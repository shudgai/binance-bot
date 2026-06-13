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

SYSTEM_PROMPT = """你的角色： 你是專業的加密貨幣交易風險審查官。
你的任務： 技術指標已經初步篩選出了一組『具潛力但存在模糊性』的交易訊號。你的任務是嚴格過濾掉『低能量』與『無效』的交易，只保留高勝率與高獲利質量的訊號。

審查重點：
1. 市場狀態 (Market Regime)： 判斷目前是 TREND_LONG(強多)、TREND_SHORT(強空) 還是 CHOP(震盪區)。如果是高頻震盪區，請優先拒絕。
2. 成交量確認 (Volume-Price Confirmation)： 價格上漲時，成交量必須同步放大。如果價格在漲但成交量萎縮，這屬於『力竭噴發』或『無人問津』，請拒絕。
3. 動能評估 (Momentum Assessment)： 判斷目前的價格變動是『強勁且連續的』還是『微弱且斷斷續續的』。如果是微弱的，請拒絕。
4. 大盤連動性 (BTC Correlation)： 若標的近期漲跌與 BTC 嚴重背離，請大幅扣減信心或拒絕，除非是強勢吸血行情。
5. 大週期趨勢一致性 (HTF Trend Alignment)： 資料中包含 htf_trend (1H) 與 htf_4h_trend (4H)。
   - 若 action=BUY 但 htf_trend=short 且 htf_4h_trend=short，除非 rsi < 30（極端超賣），否則必須 REJECT。
   - 若 action=SELL 但 htf_trend=long 且 htf_4h_trend=long，除非 rsi > 70（極端超買），否則必須 REJECT。
   - 1H 與 4H 趨勢方向不一致時，信心分數上限為 60。
6. 獲利空間評估 (Profit Space Check)： 系統已經為每個幣種計算出專屬的『最低獲利門檻』(dynamic_mvp_pct)。如果預期的獲利空間低於傳入的 dynamic_mvp_pct (扣除手續費後)，請堅決拒絕 (REJECT)。
7. 持倉策略評估 (Hold Strategy)： 若判斷交易有獲利機會且空間大於 dynamic_mvp_pct，請在 reason 中明確建議機器人採用『寬鬆移動停利』並抱單。

判斷標準： 只有當『技術訊號強烈』且『市場能量充足』且『具備顯著的獲利空間(>dynamic_mvp_pct)』且『大週期趨勢一致』時，才給予 APPROVE。

輸出要求： 請僅以 JSON 格式回傳，不可有任何前言後語 (不可包含 ```json)。必須針對每個幣種回傳：
{"BTCUSDT": {"decision": "APPROVE/REJECT", "action": "BUY/SELL/HOLD/CLOSE", "regime": "TREND_LONG/TREND_SHORT/CHOP", "setup_type": "Breakout/Reversal/Trend", "confidence": 85, "reason": "Strong breakout with huge volume, profit space >1%, recommend wide trailing stop"}}"""

async def build_ai_context(sym, s, market_wind):
    closes = s.closes[-30:] if len(s.closes) >= 30 else s.closes
    # 動態獲利門檻計算
    dynamic_mvp_pct = max(1.0, (float(s.current_atr) / s.close_price * 100 * 2.0)) if s.current_atr and s.close_price > 0 else 1.0

    return {
        "symbol": sym,
        "dynamic_mvp_pct": round(dynamic_mvp_pct, 2),
        "price": s.close_price,
        "rsi": round(float(s.current_rsi), 2) if s.current_rsi else 50.0,
        "ema20": round(float(s.ema20), 6) if s.ema20 else 0.0,
        "ema50": round(float(s.ema50), 6) if s.ema50 else 0.0,
        "macd_hist": round(float(s.macd_hist), 6) if s.macd_hist else 0.0,
        "atr": round(float(s.current_atr), 6) if s.current_atr else 0.0,
        "atr_ma20": round(float(s.atr_ma20), 6) if s.atr_ma20 else 0.0,
        "bb_up": round(float(s.bb_up), 6) if s.bb_up else 0.0,
        "bb_low": round(float(s.bb_low), 6) if s.bb_low else 0.0,
        "htf_trend": s.htf_trend,
        "htf_4h_trend": getattr(s, "htf_4h_trend", None),
        "sma200_15m": round(float(s.sma200_15m), 6) if s.sma200_15m else 0.0,
        "recent_close_changes_pct": [
            round((closes[i] - closes[i-1]) / closes[i-1] * 100, 3)
            for i in range(max(1, len(closes)-10), len(closes))
        ] if len(closes) > 1 else [],
        "btc_change_15m": round(market_wind.get("btc_change_15m", 0) * 100, 2),
        "fear_and_greed_index": market_wind.get("fng_value", 50),
        "market_regime": market_wind.get("market_regime", "NORMAL_CHOP"),
        "position_qty": s.qty,
        "profit_pct": round(float(s.profit_pct), 4) if hasattr(s, "profit_pct") and s.profit_pct is not None else 0.0
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

