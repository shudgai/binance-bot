import os
import json
import time
import asyncio
import logging
import aiohttp

logger = logging.getLogger('multi')

LOCAL_AI_BASE_URL = os.environ.get("LOCAL_AI_BASE_URL", "http://127.0.0.1:8888/v1")
LOCAL_AI_MODEL = os.environ.get("LOCAL_AI_MODEL", "llama")
AI_UPDATE_INTERVAL = 300

SYSTEM_PROMPT = """你的角色： 你是專業的加密貨幣極短線量化過濾器。
你的任務： 基於豐富的上下文數據，判斷當前標的狀態，過濾假訊號與噪音，並給出明確的動作與置信度 (0~100)。

審查重點與置信度門檻：
- < 50 (HOLD / REJECT)：盤整、假突破、逆勢。直接拒絕進場，若有持倉則轉為極度保守防守模式。
- 50 ~ 69 (WAIT)：技術面可能有訊號但雜訊高，AI 不予確認，保留觀察。
- 70 ~ 79 (BUY / SELL)：趨勢一致、成交量放大，確認進場，放寬停利區間。
- >= 80 (REVERSE)：極高置信度。只有在行情出現極端衰竭並伴隨強烈反轉結構時，才給予 REVERSE。

輸出欄位要求：
- decision: "APPROVE", "REJECT", "WAIT"
- action: "BUY", "SELL", "HOLD", "REVERSE"
- regime: "TREND_LONG", "TREND_SHORT", "CHOP"
- confidence: 0 ~ 100 (必填！決定系統防護強度的關鍵)
- reason: 必填。解釋你的判定依據。若不一致則降權。

輸出格式：
{"BTCUSDT": {"decision": "REJECT", "action": "HOLD", "regime": "CHOP", "setup_type": "None", "confidence": 40, "reason": "Low volume and conflicting HTF."}}"""

async def build_ai_context(sym, s, market_wind):
    closes = s.closes[-30:] if len(s.closes) >= 30 else s.closes
    dynamic_mvp_pct = max(1.0, (float(s.current_atr) / max(s.close_price, 1e-8) * 100 * 2.0)) if s.current_atr else 1.0
    
    # Calculate VWAP distance
    vwap_distance = ((s.close_price - s.vwap) / max(s.vwap, 1e-8) * 100) if s.vwap else 0.0
    
    # Calculate volume ratio
    volume_ratio = s.current_vol / max(s.vol_ma20, 1e-8)
    
    # Candle pattern (last 3 candles: HH, HL, LL, LH)
    candle_pattern = "UNKNOWN"
    if len(s.ohlcv) >= 3:
        p_c = s.ohlcv[-2]
        c = s.ohlcv[-1]
        if c[2] > p_c[2] and c[3] > p_c[3]:
            candle_pattern = "HIGHER_HIGH_HIGHER_LOW"
        elif c[2] < p_c[2] and c[3] < p_c[3]:
            candle_pattern = "LOWER_HIGH_LOWER_LOW"
        elif c[2] > p_c[2] and c[3] < p_c[3]:
            candle_pattern = "OUTSIDE_BAR"
        elif c[2] < p_c[2] and c[3] > p_c[3]:
            candle_pattern = "INSIDE_BAR"

    return {
        "symbol": sym,
        "price": s.close_price,
        "position_qty": s.qty,
        "profit_pct": round(float(getattr(s, "profit_pct", 0.0) or 0.0), 4),
        "htf_trend": s.htf_trend,
        "htf_4h_trend": getattr(s, "htf_4h_trend", None),
        "rsi": round(float(s.current_rsi), 2) if s.current_rsi else 50.0,
        "atr": round(float(s.current_atr), 6) if s.current_atr else 0.0,
        "atr_ma20": round(float(s.atr_ma20), 6) if s.atr_ma20 else 0.0,
        "vwap_distance_pct": round(vwap_distance, 3),
        "volume_ratio": round(volume_ratio, 2),
        "stop_loss_count": getattr(s, "stop_loss_count", 0),
        "candle_pattern": candle_pattern,
        "market_regime": market_wind.get("market_regime", "NORMAL_CHOP"),
        "fear_and_greed_index": market_wind.get("fng_value", 50),
        "btc_change_15m": round(market_wind.get("btc_change_15m", 0) * 100, 2),
    }

async def fetch_ai_signals(symbol_contexts):
    if not symbol_contexts:
        return {}
    user_content = json.dumps(symbol_contexts, ensure_ascii=False)
    payload = {
        "model": LOCAL_AI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.2,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"}
    }
    
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
            async with session.post(f"{LOCAL_AI_BASE_URL.rstrip('/')}/chat/completions", json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text = data['choices'][0]['message']['content'].strip()
                    text = text.replace("```json", "").replace("```", "").strip()
                    try:
                        results = json.loads(text)
                        processed_results = {}
                        for sym, res in results.items():
                            regime = res.get("regime", "CHOP")
                            action = res.get("action", "HOLD")
                            decision = res.get("decision", "REJECT")
                            setup_type = res.get("setup_type", "None")
                            
                            if decision == "REJECT":
                                action = "HOLD"
                                
                            if regime not in ["TREND_LONG", "TREND_SHORT", "CHOP"]:
                                regime = "CHOP"
                            if action not in ["BUY", "SELL", "HOLD", "CLOSE"]:
                                action = "HOLD"
                                
                            conf_raw = float(res.get("confidence", 0.0))
                            # 若 AI 回傳 0-100 格式，則自動縮放到 0-1.0
                            confidence = conf_raw / 100.0 if conf_raw > 1.0 else conf_raw
                                
                            processed_results[sym] = {
                                "ai_action": action,
                                "ai_regime": regime,
                                "ai_decision": decision,
                                "ai_setup_type": setup_type,
                                "ai_confidence": confidence,
                                "ai_reason": str(res.get("reason", "")),
                                "ai_updated_at": time.time()
                            }
                        return processed_results
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ [AI訊號解析失敗] 回傳非預期JSON格式: {text}")
                        return {}
                else:
                    logger.warning(f"⚠️ [AI訊號失敗] HTTP {resp.status} - {await resp.text()}")
    except Exception as e:
        import traceback
        logger.warning(f"⚠️ [AI訊號失敗] API 請求異常 ({type(e).__name__}): {e}")
        logger.warning(traceback.format_exc())
    return {}

