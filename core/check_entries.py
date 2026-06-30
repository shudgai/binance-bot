import logging
import asyncio
import time
import numpy as np

from core import ctx
from core.config import (COIN_PROFILE_CONFIG, DEFAULT_NEW_COIN_PROFILE, MAX_POSITIONS,
    DUAL_SHOT_MIN_PROFIT_ROOM, RSI_PERIOD, DAILY_LOSS_LIMIT_PCT)
from core.indicators import (_get_atr, _macd_vals, calculate_ema, calculate_macd,
    calculate_adx, calculate_bollinger_bands, _calc_sl_tp)
from core.balance import is_daily_loss_halted
import core.balance as _bal
from core.state_manager import get_open_position_count, reset_coin_state
from core.signal_engine import (compute_signal_strength, is_reversal_still_valid,
    is_eligible_for_reverse, check_pyramiding_eligibility, _load_disabled_symbols)
from core.entry_filter import is_entry_allowed

logger = logging.getLogger(__name__)


def detect_divergence(sym):
    s = ctx.STATES.get(sym)
    if not s or "rsi_history" not in s or len(s["rsi_history"]) < 3 or len(s.get("ohlcv", [])) < 3:
        return None

    closes = [x[4] for x in s["ohlcv"][-3:]]
    rsis = s["rsi_history"][-3:]

    # 價格創新低，但 RSI 沒創新低 (底背離)
    if closes[2] < closes[0] and rsis[2] > rsis[0]:
        return f"{sym} 出現底背離！價格破底 ({closes[0]:.4f}->{closes[2]:.4f}) 但 RSI 墊高 ({rsis[0]:.1f}->{rsis[2]:.1f})"
    return None


def check_all_divergence_logic():
    """自動掃描所有幣種的底背離訊號"""
    divergence_results = []
    for sym in ctx.ALL_SYMBOLS:
        res = detect_divergence(sym)
        if res:
            divergence_results.append(res)
    return divergence_results


def compute_indicators(sym):
    s = ctx.STATES[sym]
    ohlcv = s["ohlcv"]
    if len(ohlcv) < 20:
        return
    closes = np.array([x[4] for x in ohlcv])
    highs = np.array([x[2] for x in ohlcv])
    lows = np.array([x[3] for x in ohlcv])
    volumes = np.array([x[5] for x in ohlcv])
    s["closes"] = closes
    prev = s["prev_close"]
    for i in range(len(ohlcv)):
        h, l, c = ohlcv[i][2], ohlcv[i][3], ohlcv[i][4]
        if i == 0 and prev is not None:
            tr = max(h - l, abs(h - prev), abs(l - prev))
        elif i > 0:
            tr = max(h - l, abs(h - ohlcv[i-1][4]), abs(l - ohlcv[i-1][4]))
        else:
            tr = h - l
        s["tr_list"].append(tr)
    s["prev_close"] = ohlcv[-1][4]
    if len(s["tr_list"]) > 42:
        s["tr_list"] = s["tr_list"][-42:]
    if len(s["tr_list"]) >= 14:
        s["current_atr"] = float(np.mean(s["tr_list"][-14:]))
        s["atr_history"].append(s["current_atr"])
        if len(s["atr_history"]) > 1440:
            s["atr_history"] = s["atr_history"][-1440:]
        s["atr_ma20"] = float(np.mean(s["atr_history"][-20:])) if len(s["atr_history"]) >= 20 else s["current_atr"]
    if len(closes) > RSI_PERIOD:
        deltas = np.diff(closes[-(RSI_PERIOD + 1):])
        gains = deltas[deltas > 0].mean() if np.any(deltas > 0) else 1e-10
        if np.any(deltas < 0):
            losses = -deltas[deltas < 0].mean()
            rs = gains / losses
            # 正常計算，但 cap 99 避免數學上的 100 誤觸 Extreme_Reversal
            s["current_rsi"] = min(99.0, 100.0 - (100.0 / (1.0 + rs)))
        elif np.any(deltas > 0):
            s["current_rsi"] = 99.0  # 期間內全為漲K，但不等同真正超買
        else:
            s["current_rsi"] = 50.0  # 無波動
        if "rsi_history" not in s:
            s["rsi_history"] = []
        s["rsi_history"].append(s["current_rsi"])
        if len(s["rsi_history"]) > 10:
            s["rsi_history"].pop(0)
    s["vol_ma10"] = float(np.mean(volumes[-11:-1])) if len(volumes) >= 11 else float(np.mean(volumes[:-1]))
    s["vol_ma20"] = float(np.mean(volumes[-21:-1])) if len(volumes) >= 21 else float(np.mean(volumes[:-1]))
    # 使用「倒數第二根」（已完成 K 線）的量，避免當前未完成 K 線量偏低誤觸量能過濾
    s["current_vol"] = float(volumes[-2]) if len(volumes) >= 2 else float(volumes[-1])
    if len(closes) >= 20:
        s["ema20"] = calculate_ema(closes, 20)
    if len(closes) >= 50:
        s["ema50"] = calculate_ema(closes, 50)
    if len(closes) >= 26:
        m_line, m_sig, m_hist, p_line, p_sig = calculate_macd(closes)
        s["macd_line"] = m_line
        s["macd_signal"] = m_sig
        s["macd_hist"] = m_hist
        s["prev_macd_line"] = p_line
        s["prev_macd_signal"] = p_sig
    if len(closes) >= 15:
        s["adx"] = calculate_adx(highs, lows, closes, 14)
    if len(closes) >= 20:
        up, mid, low = calculate_bollinger_bands(closes)
        s["bb_up"] = up
        s["bb_mid"] = mid
        s["bb_low"] = low

    # --- Divergence Detection ---
    s["divergence"] = "none"
    if len(closes) >= 15 and len(s.get("rsi_history", [])) >= 10:
        window_closes = closes[-15:]
        window_rsi = s["rsi_history"][-10:]

        c_min = np.min(window_closes)
        c_max = np.max(window_closes)
        r_min = np.min(window_rsi)
        r_max = np.max(window_rsi)

        curr_c = closes[-1]
        curr_r = s["current_rsi"]
        prev_r = s["rsi_history"][-2] if len(s["rsi_history"]) >= 2 else curr_r

        if curr_c <= c_min and curr_r > r_min and curr_r > prev_r:
            s["divergence"] = "bullish"
        elif curr_c >= c_max and curr_r < r_max and curr_r < prev_r:
            s["divergence"] = "bearish"


async def check_entries():
    from core.orders import execute_order, close_position
    from core.market_data import load_open_positions

    disabled_syms = _load_disabled_symbols()
    for sym in ctx.ALL_SYMBOLS:
        s = ctx.STATES.get(sym)
        if s and s.get("status") == "COOLDOWN":
            continue
    # [每日熔斷] 先確認是否已觸發當日封鎖
    if is_daily_loss_halted():
        logger.info(f"[每日熔斷] 今日累計虧損已超上限 ({abs(_bal._DAILY_REALIZED_LOSS)*100:.2f}% >= {DAILY_LOSS_LIMIT_PCT*100:.1f}%)，跳過所有新進場！")
        return

    open_count = get_open_position_count()
    remaining_slots = MAX_POSITIONS - open_count

    candidates = []
    for sym in ctx.ALL_SYMBOLS:
        s = ctx.STATES[sym]

        # 幣種已被使用者停用，跳過所有進場（但不影響現有持倉的管理）
        if sym in disabled_syms:
            continue

        # --- 自動反手快速通道 ---
        pending_rev = s.get("pending_reverse")
        if pending_rev:
            if time.time() - s.get("pending_reverse_time", 0) < 300:  # 5 分鐘內有效
                if not s.get("is_ordering"):
                    logger.info(f"🔄 [自動反手執行] {sym} 偵測到反手訊號 ({pending_rev})，開始建倉！")
                    price = s["close_price"]
                    s["pending_reverse"] = None
                    s["is_ordering"] = True

                    async def _rev_task(sym, pending_rev, price):
                        try:
                            await execute_order(sym, pending_rev, price)
                        finally:
                            ctx.STATES[sym]["is_ordering"] = False
                            await load_open_positions()

                    asyncio.create_task(_rev_task(sym, pending_rev, price))
                continue
            else:
                s["pending_reverse"] = None

        if s["status"] != "ACTIVE":
            continue

        has_position = abs(s["qty"]) > 0.000001
        current_direction = "buy" if s["qty"] > 0 else "sell" if s["qty"] < 0 else None

        # 開倉數限制 (針對新開倉)
        if not has_position and open_count >= MAX_POSITIONS:
            continue

        # --- [NEW] 等待回踩 (Pullback Entry) 處理 ---
        if not has_position and s.get("waiting_pullback"):
            wp = s["waiting_pullback"]
            _wait_time = time.time() - wp["time"]
            if _wait_time > 3600:  # 1 小時沒回踩就放棄
                logger.info(f"⌛ [回踩過期] {sym} 經過 1 小時未回到支撐/壓力位，取消回踩計畫。")
                s["waiting_pullback"] = None
            else:
                p = s["close_price"]
                ema20 = s.get("ema20", 0.0)
                if ema20 > 0:
                    # 判斷是否回踩到 EMA20 (允許 0.2% 誤差)
                    if wp["side"] == "buy" and p <= ema20 * 1.002:
                        logger.info(f"🎯 [回踩進場] {sym} 成功回踩 (現價 {p:.4f} 接近 EMA20 {ema20:.4f})，準備建多單！")
                        candidates.append((sym, wp["side"], wp["strength"], wp["route"]))
                        s["waiting_pullback"] = None
                    elif wp["side"] == "sell" and p >= ema20 * 0.998:
                        logger.info(f"🎯 [回踩進場] {sym} 成功回抽 (現價 {p:.4f} 接近 EMA20 {ema20:.4f})，準備建空單！")
                        candidates.append((sym, wp["side"], wp["strength"], wp["route"]))
                        s["waiting_pullback"] = None
                continue  # 處於等回踩狀態時，跳過底下一般的新訊號判定

        current_candle_time = s["ohlcv"][-1][0] if s["ohlcv"] else 0

        # --- [新增] 自動反手訊號緩衝與 K 線收盤確認機制 ---
        if s.get("pending_reverse_trigger"):
            pending_rev_data = s["pending_reverse_trigger"]
            if current_candle_time > pending_rev_data.get("time", 0):
                logger.info(f"⏳ [{sym}] 進入新 K 線，驗證自動反手趨勢持續性...")
                if await is_reversal_still_valid(sym, pending_rev_data["side"]):
                    src = pending_rev_data.get("source", "Signal")
                    logger.info(f"⚡ [{sym}] [Reversal_Confirmed] {src} 反手確認！平倉並反手建倉 ({pending_rev_data['side']})，強度 {pending_rev_data.get('strength',0):.1f}")
                    # 1. 平倉舊倉位
                    await close_position(sym, current_direction, abs(s["qty"]), s["close_price"], s["avg_price"], reason="[AUTOMATIC_REVERSE]")
                    await asyncio.sleep(1)
                    reset_coin_state(sym)
                    # 2. 反手建倉，並記錄反手時間（冷卻 30 分鐘防連續反手）
                    s["last_reverse_time"] = time.time()
                    await execute_order(sym, pending_rev_data["side"], s["close_price"])
                else:
                    logger.info(f"❌ [{sym}] [Reversal_Cancelled] 觀察期間趨勢失效，取消反手，保留原倉位。")

                s["pending_reverse_trigger"] = None
                continue
            else:
                # 還在同一根 K 線，繼續觀察
                continue

        # --- 新增：等待收盤確認機制 ---
        if s.get("pending_side"):
            if current_candle_time <= s.get("pending_time", 0):
                continue

            # 換線了，檢查前一根(訊號K線)是否反轉
            if len(s["ohlcv"]) >= 2:
                prev_candle = s["ohlcv"][-2]
                prev_open = prev_candle[1]
                prev_close = prev_candle[4]

                is_valid = False
                if s["pending_side"] == "buy":
                    # [Layer 3] 嚴格K線：放寬容忍度至實體的 150%
                    body = prev_close - prev_open
                    upper_shadow = prev_candle[2] - prev_close
                    if body > 0 and upper_shadow < body * 2.5:
                        is_valid = True
                elif s["pending_side"] == "sell":
                    # [Layer 3] 嚴格K線：放寬容忍度至實體的 150%
                    body = prev_open - prev_close
                    lower_shadow = prev_close - prev_candle[3]
                    if body > 0 and lower_shadow < body * 2.5:
                        is_valid = True

                if is_valid:
                    # [新增] Second-Bar Confirmation
                    current_price = s["close_price"]
                    trigger_high = prev_candle[2]
                    trigger_low = prev_candle[3]

                    if s["pending_side"] == "buy" and current_price < trigger_high * 0.985:
                        logger.info(f"❌ [防二次誘騙] {sym} 第二根 K 線現價 {current_price} 未能維持在觸發 K 線高點 {trigger_high} 的 98.5% ({trigger_high*0.985:.4f}) 以上，疑似插針假突破，取消多單。")
                        is_valid = False
                    elif s["pending_side"] == "sell" and current_price > trigger_low * 1.015:
                        logger.info(f"❌ [防二次誘騙] {sym} 第二根 K 線現價 {current_price} 未能維持在觸發 K 線低點 {trigger_low} 的 101.5% ({trigger_low*1.015:.4f}) 以下，疑似插針假跌破，取消空單。")
                        is_valid = False

                    # [新增] 量能續航檢查：跟進量必須 >= 訊號量的 20%
                    if is_valid:
                        signal_vol = prev_candle[5]
                        follow_vol = s.get("current_vol", 0)
                        if signal_vol > 0 and follow_vol < signal_vol * 0.2:
                            logger.info(f"❌ [量能續航] {sym} 突破後量能萎縮 (跟進量 {follow_vol:.0f} < 訊號量 {signal_vol:.0f} × 20%)，疑似假突破，取消")
                            is_valid = False

                if not is_valid:
                    # 記錄假突破事件，同區間再次觸發時提高閾值
                    if s.get("pending_side"):
                        s["fake_breakout"] = {
                            "time": time.time(),
                            "side": s["pending_side"],
                            "level_high": prev_candle[2],
                            "level_low": prev_candle[3],
                        }

                if is_valid:
                    s["fake_breakout"] = None
                    logger.info(f"✅ [訊號確認] {sym} {s['pending_side']} 訊號已確認 (K線收盤無反轉且通過防二次誘騙)")
                    side = s["pending_side"]
                    strength = s.get("pending_strength", 5.0)
                    route = s.get("pending_route", "confirmed")
                    s["pending_side"] = None

                    p = s["close_price"]
                    atr_val, sl_dist, tp_dist, expected_rr = _calc_sl_tp(sym, side, s, p, route)
                    min_rr = s.get("min_rr", 1.0)
                    if expected_rr < min_rr:
                        logger.info(f"🛑 [Filter:RiskReward] {sym} 預期盈虧比太差 ({expected_rr:.2f} < {min_rr:.1f})，放棄進場")
                        continue

                    expected_profit_pct = tp_dist / p
                    if expected_profit_pct < DUAL_SHOT_MIN_PROFIT_ROOM:
                        logger.info(f"🛑 [Filter:MinProfit] {sym} 預期獲利空間過小 ({expected_profit_pct*100:.2f}% < {DUAL_SHOT_MIN_PROFIT_ROOM*100:.1f}%)，利潤無法覆蓋手續費與摩擦成本，拒絕進場")
                        continue

                    # 再測一次大環境 (MTF & RR)，因為換線了可能改變
                    if s.get("mtf_filter", True):
                        if strength > 15.0:
                            logger.info(f"🚀 [強勢訊號 Override] {sym} 強度 {strength:.2f} 極高，跳過 MTF 趨勢過濾直接允許進場")
                        else:
                            ema50_1h = s.get("ema50_1h", 0.0)
                            if ema50_1h > 0:
                                if side == "buy" and p <= s["ohlcv"][-2][4] and p < ema50_1h:
                                    logger.info(f"📉 [1H 過濾] {sym} 確認階段：1H 趨勢向下，捨棄訊號")
                                    continue
                                if side == "sell" and p > ema50_1h:
                                    logger.info(f"📈 [1H 過濾] {sym} 確認階段：1H 趨勢向上，捨棄訊號")
                                    continue

                    # RSI 過熱/過冷保護：趨勢型訊號確認時，禁止追高做多或追低做空
                    # 改為：進入「等待回踩 (Waiting Pullback)」狀態
                    if route not in ["Exhaustion_Entry", "Extreme_Reversal"]:
                        rsi_conf = s.get("current_rsi", 50.0)
                        if side == "buy" and rsi_conf >= 68.0:
                            logger.info(f"⏳ [等待回踩] {sym} RSI={rsi_conf:.1f}>=68，動能強但不追高，標記為等待回踩 EMA20。")
                            s["waiting_pullback"] = {"side": side, "strength": strength, "route": route, "time": time.time(), "signal_price": p}
                            s["pending_side"] = None
                            continue
                        if side == "sell" and rsi_conf <= 32.0:
                            logger.info(f"⏳ [等待回踩] {sym} RSI={rsi_conf:.1f}<=32，動能弱但不殺低，標記為等待回抽 EMA20。")
                            s["waiting_pullback"] = {"side": side, "strength": strength, "route": route, "time": time.time(), "signal_price": p}
                            s["pending_side"] = None
                            continue

                    base_rr_thresh = COIN_PROFILE_CONFIG.get(sym, {}).get("rr_threshold", 1.1)
                    rr_thresh = 0.9 if strength > 20.0 else (1.0 if strength > 15.0 else base_rr_thresh)

                    if expected_rr < rr_thresh:
                        logger.info(f"🛑 [Filter:RR_Low] {sym} 預期盈虧比 {expected_rr:.2f} < {rr_thresh}，放棄")
                        continue

                    expected_profit_pct = tp_dist / p if p > 0 else 0
                    if expected_profit_pct < 0.015:
                        logger.info(f"@@COIN_DEBUG@@ ⛔ [Block] {sym} 獲利空間過小 {expected_profit_pct*100:.2f}% < 1.5%，放棄")
                        continue

                    # [Layer 4] 動態空間過濾 (Adaptive Space Check)
                    macd_hist = s.get("macd_hist", 0.0)
                    prev_macd_hist = s.get("prev_macd_hist", 0.0)
                    rsi = s.get("current_rsi", 50.0)
                    current_atr = s.get("current_atr", 0.0)
                    atr_ma20 = s.get("atr_ma20", 0.0)
                    recent_candles = s.get("ohlcv", [])
                    if len(recent_candles) >= 20:
                        highs = np.array([x[2] for x in recent_candles])
                        lows = np.array([x[3] for x in recent_candles])
                        range_width_pct = (np.max(highs) - np.min(lows)) / np.min(lows)
                    else:
                        range_width_pct = 1.0

                    space_multiplier = 0.2

                    is_strong_trend = abs(macd_hist) > abs(prev_macd_hist) and (
                        (side == "buy" and p <= s["ohlcv"][-2][4] and rsi > 60.0) or (side == "sell" and rsi < 40.0)
                    )

                    is_consolidation = (atr_ma20 > 0 and current_atr < atr_ma20 * 0.8) and range_width_pct < 0.02

                    if is_strong_trend or route == "Automatic_Reverse":
                        space_multiplier = 0.0
                    elif is_consolidation:
                        space_multiplier = 0.5

                    if not is_strong_trend:
                        if side == "buy" and p <= s["ohlcv"][-2][4] and s.get("bb_up", 0) > 0 and p < s.get("bb_up", 0):
                            space = s["bb_up"] - p
                            if space < sl_dist * space_multiplier:
                                logger.info(f"@@COIN_DEBUG@@ ⛔ [Block] {sym} 做多空間不足 {space:.4f} < {sl_dist * space_multiplier:.4f}（布林上軌太近），放棄")
                                continue
                        if side == "sell" and s.get("bb_low", 0) > 0 and p > s.get("bb_low", 0):
                            space = p - s["bb_low"]
                            if space < sl_dist * space_multiplier:
                                logger.info(f"@@COIN_DEBUG@@ ⛔ [Block] {sym} 做空空間不足 {space:.4f} < {sl_dist * space_multiplier:.4f}（布林下軌太近），放棄")
                                continue
                    candidates.append((sym, side, strength, route))
                    continue
                else:
                    logger.info(f"❌ [訊號失效] {sym} {s['pending_side']} 訊號 K 線收盤反轉，取消開倉。")
                    s["pending_side"] = None
            else:
                s["pending_side"] = None
            continue

        # 原本的計算邏輯
        side_strength = compute_signal_strength(sym)
        if side_strength is None or side_strength[0] is None:
            continue
        side, strength, route = side_strength

        # [Layer 0] 每幣種最低信號強度門檻
        min_sig = COIN_PROFILE_CONFIG.get(sym, DEFAULT_NEW_COIN_PROFILE).get("min_signal_strength", 20.0)
        if strength < min_sig:
            continue

        # --- 2. 多重共振過濾區塊 (Multi-Confluence Entry Filter) ---
        cp = s["close_price"]
        ema50_1h = s.get("ema50_1h", 0)
        sma200_15m = s.get("sma200_15m", 0)
        rsi = s.get("current_rsi", 50)
        macd_hist = s.get("macd_hist", 0.0)
        vol_ma20 = s.get("vol_ma20", 0.0)
        volume = s["ohlcv"][-2][5] if len(s["ohlcv"]) > 1 else (s["ohlcv"][-1][5] if len(s["ohlcv"]) > 0 else 0)

        # A. 數據完整性檢查
        if sma200_15m == 0 or vol_ma20 == 0:
            continue

        # Exhaustion_Entry 與 Extreme_Reversal 是反轉策略，不受一般動能與 RSI 限制
        if route not in ["Exhaustion_Entry", "Extreme_Reversal"]:
            # C. [放寬] 動能共振過濾：RSI 多單>22；空單<78；MACD 允許剛轉向
            _macd_tiny = 1e-8
            if side == "buy":
                if rsi <= 22:
                    logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 多單 RSI 極端超賣 ({rsi:.1f} <= 22)，防接刀")
                    continue
                if macd_hist < -_macd_tiny and rsi < 35:
                    logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 多單 RSI 低 ({rsi:.1f}) 且 MACD 仍負 ({macd_hist:.6f})")
                    continue
            else:  # sell
                if rsi >= 78:
                    logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 空單 RSI 極端超買 ({rsi:.1f} >= 78)，防追高")
                    continue
                if macd_hist > _macd_tiny and rsi > 65:
                    logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 空單 RSI 高 ({rsi:.1f}) 且 MACD 仍正 ({macd_hist:.6f})")
                    continue

        # D. 真實性驗證 (Volume Confirmation) - 動態門檻
        _atr_hist_ce = s.get("atr_history", [])
        _atr_avg_ce = float(np.mean(_atr_hist_ce)) if len(_atr_hist_ce) > 0 else 0.0
        _atr_cur_ce = s.get("current_atr", 0.0)
        _is_low_vol_ce = (_atr_avg_ce > 0 and _atr_cur_ce <= _atr_avg_ce)
        _d_multiplier = 0.03 if _is_low_vol_ce else 0.04
        if route not in ("Exhaustion_Entry", "Extreme_Reversal") and volume < (vol_ma20 * _d_multiplier):
            logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 量能極度不足 (當前量 {volume:.0f} < 均量 {vol_ma20:.0f} * {_d_multiplier})")
            continue

        # E. 參與度過濾 (Participation Filter)
        if len(s["ohlcv"]) > 1:
            current_vol = volume  # 已是 ohlcv[-2]
            prev_vol = s["ohlcv"][-3][5] if len(s["ohlcv"]) > 2 else s["ohlcv"][-2][5]
            price_change = cp - s["ohlcv"][-2][1]

            _rvol_multiplier = 0.03 if _is_low_vol_ce else 0.04
            rvol_check = current_vol > (vol_ma20 * _rvol_multiplier)

            h24_quote_volume_est = vol_ma20 * cp * 288
            liquidity_check = h24_quote_volume_est > 1000000

            volume_price_sync = False
            if side == "buy" and cp <= s["ohlcv"][-2][4] and price_change > 0 and current_vol > prev_vol:
                volume_price_sync = True
            elif side == "sell" and price_change < 0 and current_vol > prev_vol:
                volume_price_sync = True

            if route != "Exhaustion_Entry":
                if not liquidity_check:
                    logger.info(f"🛑 [LOW_PARTICIPATION] {sym} 被攔截：流動性不足 (估算24H交易額: {h24_quote_volume_est:,.0f} < 1,000,000)")
                    continue
                if not rvol_check:
                    _rvol_pct = int(_rvol_multiplier * 100)
                    logger.info(f"🛑 [LOW_PARTICIPATION] {sym} 被攔截：量能爆發不足 (目前 {current_vol:.0f} 未達均量 {_rvol_pct}% | {'低波動放寬' if _is_low_vol_ce else '高波動嚴格'})")
                    continue
                if not volume_price_sync:
                    logger.info(f"⚠️ [LOW_PARTICIPATION] {sym} 量價不協同 (價格變動: {price_change:.6f}, 大於前量: {current_vol > prev_vol})，但已放寬不攔截")

        # F. 極端區域防禦 (Extreme Zone Defense)
        if route != "Exhaustion_Entry" and strength <= 15.0:
            if side == "buy" and rsi > 80:
                logger.info(f"🛑 [EXTREME_ZONE_FAIL] {sym} 被攔截：RSI {rsi:.1f} 極端超買，拒絕追高做多")
                continue
            if side == "sell" and rsi < 25:
                logger.info(f"🛑 [EXTREME_ZONE_FAIL] {sym} 被攔截：RSI {rsi:.1f} 極端超賣，拒絕殺低做空")
                continue
        elif route != "Exhaustion_Entry" and strength > 15.0:
            if side == "buy" and rsi > 88:
                logger.info(f"🛑 [EXTREME_ZONE_FAIL] {sym} 強勢訊號仍被攔截：RSI {rsi:.1f} 極端超買頂部")
                continue
            if side == "sell" and rsi < 12:
                logger.info(f"🛑 [EXTREME_ZONE_FAIL] {sym} 強勢訊號仍被攔截：RSI {rsi:.1f} 極端超賣底部")
                continue

        logger.info(f"✅ [CONFLUENCE_PASS] {sym}: {side} 四重防禦過濾皆通過！(Route: {route})")

        # --- 方向鎖定 (Direction Lock) 與 高門檻自動反手 ---
        if has_position:
            if side != current_direction:
                if await is_eligible_for_reverse(sym, strength):
                    if not s.get("pending_reverse_trigger"):
                        s["pending_reverse_trigger"] = {
                            "side": side,
                            "time": current_candle_time,
                            "strength": strength,
                            "source": "Signal",
                        }
                        logger.info(f"⚡ [{sym}] [Pending_Reversal_Detected] 反轉訊號強度 {strength:.1f}，等待下一根 K 收盤確認...")
                    continue
                else:
                    continue
            else:
                # 金字塔加倉邏輯 (順勢加碼)
                is_eligible, cooldown_mins = check_pyramiding_eligibility(s)
                if not is_eligible:
                    logger.info(f"⏳ [加碼防禦] {sym} 欲順勢加倉 {side}，但未達動態冷卻 ({cooldown_mins}m) 或已達上限，攔截加碼")
                    continue

        if not is_entry_allowed(sym, side, route, strength):
            continue

        # --- 反手冷卻時間 (min_flip_time) 過濾 ---
        last_trade_side = s.get("last_trade_side", "")
        if last_trade_side != "" and side != last_trade_side and route != "Automatic_Reverse":
            flip_elapsed = time.time() - s.get("last_trade_time", 0)
            last_exit = s.get("last_exit_reason", "")
            is_stop_loss = "Stop" in last_exit or "Loss" in last_exit or "Trailing" in last_exit or "Momentum_Fade" in last_exit

            if is_stop_loss:
                min_flip = 60
            else:
                min_flip = 1800

            if flip_elapsed < min_flip:
                logger.info(f"⏳ [Filter:Cooldown] [獲利防反手] {sym} 欲 {side}，但距離上次做 {last_trade_side} 僅 {flip_elapsed:.0f}s (獲利後需冷卻 {min_flip}s)，保護利潤不接刀！")
                continue

        # --- 同價位防雙巴鎖 (Price Zone Lock) ---
        p = s["close_price"]
        last_entry_price = s.get("last_entry_price", 0.0)
        last_entry_dir = s.get("last_entry_direction", "")
        if last_entry_price > 0 and last_entry_dir != "" and route != "Automatic_Reverse":
            price_diff_pct = abs(p - last_entry_price) / last_entry_price
            if price_diff_pct < 0.003 and side != last_entry_dir:
                logger.info(f"🛑 [Filter:Choppiness] {sym} 欲 {side}，但現價 {p:.4f} 距離上次進場價 {last_entry_price:.4f} 誤差小於 0.3%，陷入原地盤整，拒絕雙巴被洗！")
                continue

        # --- 動能背離過濾 (Divergence Filter) ---
        divergence_type = s.get("divergence", "none")
        if route == "Automatic_Reverse":
            if (side == "buy" and cp <= s["ohlcv"][-2][4] and divergence_type == "bullish") or (side == "sell" and divergence_type == "bearish"):
                strength *= 1.5
                logger.info(f"🌟 [Divergence_Boost] {sym} 偵測到強烈背離，權重提升至 {strength:.2f}")
            else:
                strength *= 0.9
        else:
            if divergence_type == "bearish" and side == "buy":
                logger.info(f"@@COIN_DEBUG@@ 🛑 [Divergence_Block] {sym} 頂背離阻擋做多 → 訊號取消")
                continue
            if divergence_type == "bullish" and side == "sell":
                logger.info(f"@@COIN_DEBUG@@ 🛑 [Divergence_Block] {sym} 底背離阻擋做空 → 訊號取消")
                continue

        # --- 1H 多重時間週期 (Multi-Timeframe) 過濾 ---
        if s.get("mtf_filter", True):
            if strength > 15.0 or route == "Automatic_Reverse":
                logger.info(f"🚀 [強勢訊號 Override] {sym} 強度 {strength:.2f} 極高或來自反手，跳過 MTF 趨勢過濾直接允許進場")
            else:
                ema50_1h = s.get("ema50_1h", 0.0)
                if ema50_1h > 0:
                    if side == "buy" and cp <= s["ohlcv"][-2][4] and p < ema50_1h:
                        logger.info(f"📉 [1H 過濾] {sym} 1H 趨勢向下 (現價 {p:.4f} < EMA50 {ema50_1h:.4f})，忽略買入訊號")
                        continue
                    if side == "sell" and p > ema50_1h:
                        logger.info(f"📈 [1H 過濾] {sym} 1H 趨勢向上 (現價 {p:.4f} > EMA50 {ema50_1h:.4f})，忽略賣出訊號")
                        continue

        # --- R:R 盈虧比過濾 (Risk:Reward Filter) ---
        atr_val, sl_dist, tp_dist, expected_rr = _calc_sl_tp(sym, side, s, p, route)
        base_rr_thresh = s.get("min_rr", 1.3)

        rr_thresh = 1.1 if strength > 20.0 else (1.2 if strength > 15.0 else base_rr_thresh)
        if base_rr_thresh >= 2.0:
            rr_thresh = base_rr_thresh

        if route != "Automatic_Reverse" and expected_rr < rr_thresh:
            logger.info(f"🛑 [Filter:RR_Low] {sym} 預期盈虧比 {expected_rr:.2f} < {rr_thresh}，放棄暫存")
            continue

        expected_profit_pct = tp_dist / p if p > 0 else 0
        if expected_profit_pct < DUAL_SHOT_MIN_PROFIT_ROOM:
            logger.info(f"⚠️ [獲利空間過濾] {sym} 預期潛在利潤過小 ({expected_profit_pct*100:.2f}% < {DUAL_SHOT_MIN_PROFIT_ROOM*100:.1f}%)，無法覆蓋手續費與滑點，放棄暫存")
            continue

        # 絕對獲利空間硬門檻 1.5% (MinProfit Hard Gate)
        # 防止在極低波動（ATR 極小）時進場
        _HARD_MIN_PROFIT_PCT = 0.015  # 1.5% 硬門檻
        if expected_profit_pct < _HARD_MIN_PROFIT_PCT:
            logger.info(f"🛑 [Filter:MinProfit_Hard] {sym} 預期獲利僅 {expected_profit_pct*100:.2f}%，遠低於 {_HARD_MIN_PROFIT_PCT*100:.1f}% 硬門檻，拒絕進場")
            continue

        # --- Flip Buffer: 防止快速反手 ---
        last_entry_time = s.get("last_entry_time", 0.0)
        if route != "Automatic_Reverse" and last_entry_time > 0 and (time.time() - last_entry_time) < 300:
            logger.info(f"⏳ [Flip Buffer] {sym} 訊號 {side} 被攔截 (距離上次開倉僅 {time.time() - last_entry_time:.0f}s)")
            continue

        # --- 錯誤方向禁止再進 (Wrong Direction Ban) ---
        _wd_time = s.get("wrong_dir_time", 0.0)
        _wd_side = s.get("wrong_dir_side", "")
        if _wd_side == side and time.time() - _wd_time < 1800:
            logger.info(f"⏳ [Wrong Dir Ban] {sym} 同方向 {side} 剛在 {time.time()-_wd_time:.0f}s 前開錯方向，冷卻中 (30min)")
            continue

        # --- 假突破記憶檢查 (Fake Breakout Memory) ---
        _fb = s.get("fake_breakout")
        if _fb and time.time() - _fb["time"] < 14400 and route not in ("Extreme_Reversal", "Automatic_Reverse"):
            _fb_level = _fb["level_high"] if _fb["side"] == "buy" else _fb["level_low"]
            _current_dist = abs(p - _fb_level) / max(_fb_level, 1e-8)
            _atr_fb = s.get("current_atr", 0)
            if _current_dist < (_atr_fb * 2 / max(p, 1e-8)) and side == _fb["side"]:
                _boost_needed = 5.0
                _effective_min = min_sig + _boost_needed
                if strength < _effective_min:
                    logger.info(f"⏳ [假突破記憶] {sym} 距上次同向假突破不到 2 ATR ({_current_dist*100:.3f}%)，強度 {strength:.1f} < {_effective_min:.1f}，暫停進場")
                    continue
                logger.info(f"⚠️ [假突破記憶] {sym} 距上次同向假突破不到 2 ATR，但強度 {strength:.1f} >= {_effective_min:.1f}，允許進場")
                strength *= 0.85

        # 通過 Flip Buffer，進入 pending 狀態等待下一根 K 線確認
        s["pending_side"] = side
        s["pending_time"] = current_candle_time
        s["pending_strength"] = strength
        s["pending_route"] = route
        s["entry_reason"] = route  # 保留到平倉記錄，避免 trade_history 全部 UNKNOWN

        logger.info(f"⏳ [等待確認] {sym} 產生 {side} 訊號 ({route})，等待目前 K 線收盤確認...")

    if not candidates:
        return

    candidates.sort(key=lambda x: -x[2])
    logger.info(f"📊 [訊號排行] {' | '.join(f'{sym}:{side}({strength:.2f})' for sym, side, strength, _ in candidates[:3])}")

    total_weight = sum(strength for _, _, strength, _ in candidates)

    for sym, side, strength, route in candidates:
        s = ctx.STATES[sym]
        has_pos = abs(s["qty"]) > 0.000001

        if not has_pos:
            if remaining_slots <= 0:
                continue
            remaining_slots -= 1
            logger.info(f"⚡ [即時開倉] {sym} 觸發訊號 ({route} 路線)，即刻首倉進場！")
        else:
            logger.info(f"⚡ [順勢加倉] {sym} 觸發加倉訊號 ({route} 路線)，準備執行加碼！")

        if not s.get("is_ordering"):
            s["is_ordering"] = True

            # --- 動態權重分配 (Dynamic Position Sizing) ---
            raw_ratio = strength / total_weight if total_weight > 0 else 1.0
            allocation_pct = min(raw_ratio, 0.6)  # 最高封頂 60%

            weight_label = f"{allocation_pct*100:.1f}%"
            logger.info(f"⚖️ [Allocation_Ratio] {sym} 強度 {strength:.1f} (原始佔比 {raw_ratio*100:.1f}%)，實際分配資金封頂為: {weight_label}")

            async def _entry_task(sym, side, price, alloc_pct):
                try:
                    await execute_order(sym, side, price, alloc_pct)
                finally:
                    ctx.STATES[sym]["is_ordering"] = False

            asyncio.create_task(_entry_task(sym, side, s["close_price"], allocation_pct))

        s["pending_side"] = None
        s["pending_confirm_high"] = 0
        s["pending_confirm_low"] = 0
