import logging
import time
import json
import numpy as np

from core import ctx
from core.config import (COIN_PROFILE_CONFIG, CONFIG_FILE,
    DEFAULT_REVERSAL_SETTINGS, SYMBOL_REVERSAL_SETTINGS)
from core.indicators import _get_atr, _macd_vals, calculate_macd

logger = logging.getLogger(__name__)


def compute_signal_strength(sym):
    s = ctx.STATES[sym]
    if len(s["closes"]) < 20:
        return (None, 0, None)

    # --- 新增 C：動能/成交量過濾 ---
    vol_ma10 = s.get("vol_ma10", 0.0)
    current_vol = s.get("current_vol", 0.0)
    if vol_ma10 > 0 and current_vol < vol_ma10 * 0.000015:
        return (None, 0, None)

    # --- 第三層防禦：極值檢查 (Extreme Value Defense) ---
    rsi = s.get("current_rsi", 50.0)
    rsi_extreme_low = s.get("rsi_extreme_low", 20)
    rsi_extreme_high = s.get("rsi_extreme_high", 75)

    if rsi < rsi_extreme_low:
        macd_line_v = s.get("macd_line", 0.0)
        macd_sig_v = s.get("macd_signal", 0.0)
        macd_trending_down = (macd_line_v - macd_sig_v) < 0
        rsi_history = s.get("rsi_history", [])
        is_hooking_up = len(rsi_history) >= 2 and rsi_history[-1] > rsi_history[-2]
        if not macd_trending_down and not is_hooking_up:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [極值防禦] RSI ({rsi:.1f}) < {rsi_extreme_low} 且未見轉折向上，拒絕進場防接刀")
            return (None, 0, None)

    if rsi > rsi_extreme_high:
        s["is_extreme_high_rsi"] = True
    else:
        s["is_extreme_high_rsi"] = False

    rsi = s["current_rsi"]
    close = s["close_price"]
    prev_close = s["prev_close"] if s["prev_close"] is not None else close
    ema20 = s.get("ema20", 0.0)
    ema50 = s.get("ema50", 0.0)

    trend_long = ema20 > 0 and close > ema20
    trend_short = ema20 > 0 and close < ema20

    # Define parameters for dynamic RSI thresholds
    LONG_RSI_NORMAL = 45.0
    SHORT_RSI_NORMAL = 55.0
    LONG_RSI_HIGH_VOL = 30.0
    SHORT_RSI_HIGH_VOL = 70.0

    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)

    if current_atr > atr_24h_avg and atr_24h_avg > 0:
        long_rsi_threshold = LONG_RSI_HIGH_VOL
        short_rsi_threshold = SHORT_RSI_HIGH_VOL
        vol_mode = "高波動模式 (High Vol)"
    else:
        long_rsi_threshold = LONG_RSI_NORMAL
        short_rsi_threshold = SHORT_RSI_NORMAL
        vol_mode = "低波動模式 (Low Vol)"

    logger.info(f"@@COIN_DEBUG@@ 🔍 {sym} | RSI: {rsi:.1f} | Price: {close:.4f} (BB: {s.get('bb_low', 0):.4f} - {s.get('bb_up', 0):.4f}) | MACD: {s.get('macd_line', 0):.4f}/{s.get('macd_signal', 0):.4f} | Trend (L/S): {trend_long}/{trend_short} | VolMode: {vol_mode} (ATR: {current_atr:.5f} / 24h Avg: {atr_24h_avg:.5f})")

    is_in_bb_zone_long = close <= s.get("bb_low", 0) * 1.005
    is_in_bb_zone_short = close >= s.get("bb_up", 0) * 0.995

    macd_line = s.get("macd_line", 0.0)
    macd_signal = s.get("macd_signal", 0.0)
    prev_macd_line = s.get("prev_macd_line", 0.0)
    prev_macd_signal = s.get("prev_macd_signal", 0.0)

    macd_hist = macd_line - macd_signal
    prev_macd_hist = prev_macd_line - prev_macd_signal

    long_macd_cross = prev_macd_line <= prev_macd_signal and macd_line > macd_signal
    short_macd_cross = prev_macd_line >= prev_macd_signal and macd_line < macd_signal

    long_macd_hist_aligned  = macd_hist > 0 and macd_hist > prev_macd_hist
    short_macd_hist_aligned = macd_hist < 0 and macd_hist < prev_macd_hist

    long_macd_ok = long_macd_cross or long_macd_hist_aligned
    short_macd_ok = short_macd_cross or short_macd_hist_aligned

    # --- 放寬：只需最後 1 根 K 線方向一致即可 ---
    last_candle_long  = len(s["ohlcv"]) >= 2 and s["ohlcv"][-1][4] > s["ohlcv"][-2][4]
    last_candle_short = len(s["ohlcv"]) >= 2 and s["ohlcv"][-1][4] < s["ohlcv"][-2][4]
    # 保留原連2根判斷供加分使用
    last_two_candles_long  = len(s["ohlcv"]) >= 3 and s["ohlcv"][-1][4] > s["ohlcv"][-2][4] and s["ohlcv"][-2][4] > s["ohlcv"][-3][4]
    last_two_candles_short = len(s["ohlcv"]) >= 3 and s["ohlcv"][-1][4] < s["ohlcv"][-2][4] and s["ohlcv"][-2][4] < s["ohlcv"][-3][4]

    ema50 = s.get("ema50", 0.0)
    trend_confluence_long  = ema50 == 0.0 or close > ema50
    trend_confluence_short = ema50 == 0.0 or close < ema50

    sma200 = s.get("sma200_15m", 0)
    is_above_sma200 = sma200 > 0 and close > sma200 * 0.999
    is_below_sma200 = sma200 > 0 and close < sma200 * 1.001
    sma200_neutral   = sma200 == 0

    close_near_ema20_long  = ema20 <= 0 or close <= ema20 * 1.08
    close_near_ema20_short = ema20 <= 0 or close >= ema20 * 0.92
    is_in_bb_zone_long  = s.get("bb_low", 0) > 0 and close <= s["bb_low"] * 1.01
    is_in_bb_zone_short = s.get("bb_up",  0) > 0 and close >= s["bb_up"]  * 0.99

    # 預先計算供 Log 顯示的預估強度
    l_ts = 0; s_ts = 0
    if is_above_sma200: l_ts += 4; s_ts -= 3
    elif is_below_sma200 and not sma200_neutral: l_ts -= 3; s_ts += 4
    if trend_confluence_long and (long_macd_cross or macd_hist > 0): l_ts += 5
    if trend_confluence_short and (short_macd_cross or macd_hist < 0): s_ts += 5
    if trend_confluence_short and (long_macd_cross or macd_hist > 0): l_ts -= 5
    if trend_confluence_long and (short_macd_cross or macd_hist < 0): s_ts -= 5
    if last_two_candles_long: l_ts += 3
    if last_two_candles_short: s_ts += 3

    raw_long_str = 12.0 + ((close - ema20) / max(ema20, 1e-8) * 100) + l_ts + (5.0 if long_macd_cross else 0.0)
    raw_short_str = 12.0 + ((ema20 - close) / max(ema20, 1e-8) * 100) + s_ts + (5.0 if short_macd_cross else 0.0)
    if rsi >= 80.0: raw_short_str = 15.0 + ((rsi - 80.0) / 2.0)
    if rsi <= 20.0: raw_long_str = 15.0 + ((20.0 - rsi) / 2.0)

    logger.info(f"@@COIN_DEBUG@@ 🔍 {sym} 條件檢測 | 預估強度(L/S): {raw_long_str:.1f}/{raw_short_str:.1f} | RSI動能(L>48/S<52): {rsi > 48.0}/{rsi < 52.0} | SMA200長線(L/S): {is_above_sma200}/{is_below_sma200} | MACD多頭/空頭: {macd_hist > 0}/{macd_hist < 0} | 收盤價確認(L/S): {last_candle_long}/{last_candle_short} | 連2根(L/S): {last_two_candles_long}/{last_two_candles_short} | EMA20距離(L/S): {close_near_ema20_long}/{close_near_ema20_short} | BB區(L/S): {is_in_bb_zone_long}/{is_in_bb_zone_short} | EMA50確認(L/S): {trend_confluence_long}/{trend_confluence_short}")

    # 💥 極端反轉路線 (Extreme Reversal)
    if rsi >= 80.0:
        strength = 15.0 + ((rsi - 80.0) / 2.0)
        return ("sell", strength, "Extreme_Reversal")

    if rsi <= 20.0:
        strength = 15.0 + ((20.0 - rsi) / 2.0)
        return ("buy", strength, "Extreme_Reversal")

    rsi_ok_long  = rsi < 75.0 and (rsi > 32.0 or (rsi >= 25.0 and (long_macd_cross  or macd_hist > 0)))
    rsi_ok_short = rsi > 25.0 and (rsi < 68.0 or (rsi <= 75.0 and (short_macd_cross or macd_hist < 0)))

    # --- 加分機制 ---
    long_trend_score = 0
    short_trend_score = 0

    if is_above_sma200:
        long_trend_score += 4
        short_trend_score -= 3
    elif is_below_sma200 and not sma200_neutral:
        long_trend_score -= 3
        short_trend_score += 4

    if trend_confluence_long and (long_macd_cross or macd_hist > 0):
        long_trend_score += 5
    if trend_confluence_short and (short_macd_cross or macd_hist < 0):
        short_trend_score += 5

    if trend_confluence_short and (long_macd_cross or macd_hist > 0):
        long_trend_score -= 5
    if trend_confluence_long and (short_macd_cross or macd_hist < 0):
        short_trend_score -= 5

    if last_two_candles_long:
        long_trend_score += 3
    if last_two_candles_short:
        short_trend_score += 3

    # Gate 1: EMA50 方向
    ema50_gate_long  = ema50 <= 0 or close > ema50
    ema50_gate_short = ema50 <= 0 or close < ema50

    # Gate 2: RSI 方向區間（25-75，填補 Extreme_Reversal ≤20 和正常多頭 >35 之間的 20-35 死區）
    rsi_direction_long  = rsi > 25.0
    rsi_direction_short = rsi < 75.0

    # Gate 3: MACD 方向一致即可
    macd_ok_long  = long_macd_cross  or macd_hist > 0
    macd_ok_short = short_macd_cross or macd_hist < 0

    # SMA200 純加分
    sma200_bonus_long  = 3.0 if is_above_sma200 else (-2.0 if (not sma200_neutral and is_below_sma200) else 0.0)
    sma200_bonus_short = 3.0 if is_below_sma200 else (-2.0 if (not sma200_neutral and is_above_sma200) else 0.0)

    # ── Route A: 標準順勢進場 ──────────────────────────────────────────────
    # last_candle 改為加分條件（+2），不再是硬性前提
    # 原因：小幅拉回時當前K線短暫反向，導致所有訊號同時失效，機器人完全靜默
    route_a_long = (
        macd_ok_long and
        rsi_ok_long and
        rsi_direction_long and
        ema50_gate_long and
        close_near_ema20_long
    )

    route_a_short = (
        macd_ok_short and
        rsi_ok_short and
        rsi_direction_short and
        ema50_gate_short and
        close_near_ema20_short
    )

    # ── Route B: EMA20 回測彈跳 ─────────────────────────────────────────────
    near_ema20_pullback = ema20 > 0 and abs(close - ema20) / ema20 <= 0.015
    ema20_above_ema50   = ema20 > 0 and ema50 > 0 and ema20 > ema50
    ema20_below_ema50   = ema20 > 0 and ema50 > 0 and ema20 < ema50

    route_b_long = (
        ema50_gate_long and
        ema20_above_ema50 and
        near_ema20_pullback and
        macd_ok_long and
        rsi_direction_long and
        rsi_ok_long
    )

    route_b_short = (
        ema50_gate_short and
        ema20_below_ema50 and
        near_ema20_pullback and
        macd_ok_short and
        rsi_direction_short and
        rsi_ok_short
    )

    long_base_ok  = route_a_long or route_b_long
    short_base_ok = route_a_short or route_b_short
    route_tag     = "b" if (route_b_long or route_b_short) else "a"

    if long_base_ok or short_base_ok:
        long_str = 0.0
        short_str = 0.0

        if long_base_ok:
            long_str = 12.0 + ((close - ema20) / max(ema20, 1e-8) * 100)
            if long_macd_cross:    long_str += 5.0
            if route_tag == "b":   long_str += 2.0
            if last_candle_long:   long_str += 2.0   # K線方向確認加分（非硬性）
            long_str += long_trend_score + sma200_bonus_long

        if short_base_ok:
            short_str = 12.0 + ((ema20 - close) / max(ema20, 1e-8) * 100)
            if short_macd_cross:   short_str += 5.0
            if route_tag == "b":   short_str += 2.0
            if last_candle_short:  short_str += 2.0  # K線方向確認加分（非硬性）
            short_str += short_trend_score + sma200_bonus_short

        # 多空同時成立時比強度取勝，不再永遠偏向多單
        if long_str >= short_str and long_base_ok:
            return ("buy",  long_str  if long_str  >= 10.0 else 0.0, route_tag)
        elif short_base_ok:
            return ("sell", short_str if short_str >= 10.0 else 0.0, route_tag)

    # --- Route C: 量能衰竭進場策略 (Exhaustion Entry) ---
    if len(s["ohlcv"]) >= 50:
        c1 = s["ohlcv"][-2]
        c2 = s["ohlcv"][-3]

        current_atr = s.get("current_atr", 0.0)
        atr_history = s.get("atr_history", [])
        atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
        if atr_24h_avg > 0 and current_atr > atr_24h_avg * 2.0:
            return (None, 0, None)

        c2_vol_low = c2[5] < s.get("vol_ma20", 1) * 0.65

        recent_low_50 = min([x[3] for x in s["ohlcv"][-50:]])
        recent_high_50 = max([x[2] for x in s["ohlcv"][-50:]])
        sma200 = s.get("sma200_15m", 0)

        _exh_btc_4h = ctx.MARKET_WIND.get("btc_trend_4h", "NEUTRAL")

        # 多單：抓回檔底部
        if c2[4] < c2[1] and c2_vol_low:
            bb_low = s.get("bb_low", 0)
            is_near_sma = (sma200 > 0) and (abs(c1[3] - sma200) / sma200 < 0.005)
            is_near_low = (recent_low_50 > 0) and (c1[3] <= recent_low_50 * 1.005)
            support_ok = (bb_low > 0 and c1[3] <= bb_low * 1.005) or is_near_sma or is_near_low

            c2_mid = (c2[1] + c2[4]) / 2
            price_rebound = c1[4] > c2[4]
            has_lower_wick = (min(c1[1], c1[4]) - c1[3]) > abs(c1[4] - c1[1]) * 0.5
            crossed_midpoint = c1[4] > c2_mid
            pa_ok = price_rebound and has_lower_wick and crossed_midpoint
            bounce_ok = (c1[4] > c1[1]) and (c1[5] > c2[5] * 1.2) and crossed_midpoint

            trend_ok = (_exh_btc_4h != "BEAR")  # 宏觀雙熊不做多底接

            if trend_ok and support_ok and (pa_ok or bounce_ok):
                logger.info(f"🌟 [量能衰竭] {sym} 觸發多單低接條件！(Support:{support_ok}, PA:{pa_ok}, Bounce:{bounce_ok})")
                return ("buy", 15.0, "Exhaustion_Entry")

        # 空單：抓反彈頂部
        if c2[4] > c2[1] and c2_vol_low:
            bb_up = s.get("bb_up", 0)
            is_near_sma_res = (sma200 > 0) and (abs(c1[2] - sma200) / sma200 < 0.005)
            is_near_high = (recent_high_50 > 0) and (c1[2] >= recent_high_50 * 0.995)
            resistance_ok = (bb_up > 0 and c1[2] >= bb_up * 0.995) or is_near_sma_res or is_near_high

            c2_mid = (c2[1] + c2[4]) / 2
            price_rebound = c1[4] < c2[4]
            has_upper_wick = (c1[2] - max(c1[1], c1[4])) > abs(c1[4] - c1[1]) * 0.5
            crossed_midpoint = c1[4] < c2_mid
            pa_ok = price_rebound and has_upper_wick and crossed_midpoint
            bounce_ok = (c1[4] < c1[1]) and (c1[5] > c2[5] * 1.2) and crossed_midpoint

            trend_ok = (_exh_btc_4h != "BULL")  # 宏觀多頭不做空高空

            if trend_ok and resistance_ok and (pa_ok or bounce_ok):
                logger.info(f"🌟 [量能衰竭] {sym} 觸發空單高空條件！(Resistance:{resistance_ok}, PA:{pa_ok}, Bounce:{bounce_ok})")
                return ("sell", 15.0, "Exhaustion_Entry")

    return (None, 0, None)


async def is_reversal_still_valid(sym, pending_side):
    """
    反手確認：在 K 線收盤後驗證反轉訊號仍然有效。
    同時檢查大盤方向、MACD、價格位置。
    """
    s = ctx.STATES.get(sym)
    if not s or not s.get("ohlcv") or len(s["ohlcv"]) < 2:
        return False

    current_price = s["close_price"]
    prev_candle = s["ohlcv"][-2]
    prev_close = prev_candle[4]

    # 1. 大盤方向過濾：BTC 雙熊不允許做多反手；BTC 4H 多頭不允許做空反手
    btc_4h = ctx.MARKET_WIND.get("btc_trend_4h")
    btc_1h = ctx.MARKET_WIND.get("btc_trend_1h")
    rsi = s.get("current_rsi", 50.0)
    if pending_side == "buy" and btc_4h == "BEAR" and btc_1h == "BEAR":
        if rsi >= 32:
            logger.info(f"🔴 [Reversal_MacroBlock] {sym} BTC 雙熊，做多反手需 RSI<32，目前 {rsi:.1f}")
            return False
    if pending_side == "sell" and btc_4h == "BULL":
        if rsi <= 73.0:
            logger.info(f"🔵 [Reversal_BullBlock] {sym} BTC 4H 多頭，做空反手需 RSI>73，目前 {rsi:.1f}")
            return False

    # 2. 價格位置確認（防接刀 / 防地板空）
    if pending_side == "buy":
        if current_price < prev_close * 0.995:
            logger.info(f"📉 [Reversal_Invalid] {sym} 反手做多：現價已跌超 0.5%，放棄")
            return False
    elif pending_side == "sell":
        if current_price > prev_close * 1.005:
            logger.info(f"📈 [Reversal_Invalid] {sym} 反手做空：現價已漲超 0.5%，放棄")
            return False

    # 3. MACD 動能擴張確認 (Momentum Expansion)
    # 不只看方向轉折，還要確認 MACD 柱狀圖「正在加速擴張」才算有效反手動能
    macd_line = s.get("macd_line", 0.0)
    macd_signal_val = s.get("macd_signal", 0.0)
    prev_macd_line = s.get("prev_macd_line", 0.0)
    prev_macd_signal = s.get("prev_macd_signal", 0.0)

    macd_hist_now = macd_line - macd_signal_val
    macd_hist_prev = prev_macd_line - prev_macd_signal

    if pending_side == "buy":
        if not (macd_hist_now > 0 and macd_hist_now > macd_hist_prev):
            logger.info(f"📉 [Reversal_Weak_Momentum] {sym} 反手做多：MACD 雖轉正但未擴張 ({macd_hist_now:.6f} <= {macd_hist_prev:.6f})，放棄反手")
            return False
    elif pending_side == "sell":
        if not (macd_hist_now < 0 and macd_hist_now < macd_hist_prev):
            logger.info(f"📈 [Reversal_Weak_Momentum] {sym} 反手做空：MACD 雖轉負但未擴張 ({macd_hist_now:.6f} >= {macd_hist_prev:.6f})，放棄反手")
            return False

    # 4. 反手空間防護 (Space Buffer for Reverse)
    # 確保進場點不是在「剛好轉折」的最高/最低點追價
    if pending_side == "buy":
        if current_price > prev_close:
            logger.info(f"🛑 [Reversal_Chase_High] {sym} 反手做多：現價 ({current_price:.4f}) > 前收 ({prev_close:.4f})，在轉折點過高處追價，拒絕")
            return False
    elif pending_side == "sell":
        if current_price < prev_close:
            logger.info(f"🛑 [Reversal_Chase_Low] {sym} 反手做空：現價 ({current_price:.4f}) < 前收 ({prev_close:.4f})，在轉折點過低處追價，拒絕")
            return False

    return True


async def is_eligible_for_reverse(sym, current_strength):
    """判斷是否允許反手：統一標準，避免多路徑衝突。"""
    s = ctx.STATES.get(sym)
    if not s or s.get("is_banned"):
        return False

    # 1. 反手強度門檻 ≥ 15
    if current_strength < 15.0:
        logger.info(f"⏳ [REVERSE_DENIED] {sym} 反手強度不足 ({current_strength:.1f} < 15.0)")
        return False

    # 2. 距上次反手至少 30 分鐘
    last_reverse = s.get("last_reverse_time", 0)
    if (time.time() - last_reverse) < 1800:
        logger.info(f"⏳ [REVERSE_DENIED] {sym} 距上次反手不足 30 分鐘")
        return False

    # 3. 最少持倉 5 分鐘才允許反手
    open_time = s.get("open_time", time.time())
    hold_sec = time.time() - open_time
    if hold_sec < 300:
        logger.info(f"⏳ [REVERSE_DENIED] {sym} 持倉未達 5 分鐘 ({hold_sec:.0f}s)，防雜訊反手")
        return False

    # 4. 目前不能已有另一個反手在等待
    if s.get("pending_reverse_trigger"):
        return False

    return True


def get_dynamic_cooldown(current_atr, avg_atr, adx_value, base_cooldown=15):
    volatility_ratio = current_atr / avg_atr if avg_atr > 0 else 1.0
    vol_factor = 1.0 + (max(0, volatility_ratio - 1.0) * 0.5)

    if adx_value > 30:
        trend_factor = 0.8
    elif adx_value < 20:
        trend_factor = 1.5
    else:
        trend_factor = 1.0

    dynamic_cooldown = base_cooldown * vol_factor * trend_factor
    return max(5, min(60, round(dynamic_cooldown)))


def check_pyramiding_eligibility(s):
    if not s.get('entries'):
        return False, 0

    last_entry = s['entries'][-1]
    last_entry_time = last_entry['time']

    current_atr = s.get('current_atr', 0.0)
    avg_atr = s.get('atr_ma20', current_atr)
    adx_value = s.get('adx', 25.0)

    dynamic_cooldown_mins = get_dynamic_cooldown(current_atr, avg_atr, adx_value)

    current_time = time.time()
    seconds_passed = current_time - last_entry_time
    minutes_passed = seconds_passed / 60

    is_cooldown_over = minutes_passed >= dynamic_cooldown_mins
    is_under_max_layers = len(s['entries']) < 3

    if is_cooldown_over and is_under_max_layers:
        price_gap = abs(s['close_price'] - s.get('avg_price', s['close_price'])) / s.get('avg_price', s['close_price'])
        if price_gap < 0.05:
            return True, dynamic_cooldown_mins

    return False, dynamic_cooldown_mins


def _load_disabled_symbols():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {s.upper().replace(":USDT", "USDT") for s in data.get("disabled", [])}
    except Exception:
        return set()
