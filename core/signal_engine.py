import logging
import time
import json
import numpy as np

from core import ctx
from core.config import (COIN_PROFILE_CONFIG, CONFIG_FILE,
    DEFAULT_REVERSAL_SETTINGS, SYMBOL_REVERSAL_SETTINGS, get_entry_strictness_profile)
from core.indicators import _get_atr, _macd_vals, calculate_macd, check_candle_strength
from core.strategy.strategy_engine import StrategyEngine

logger = logging.getLogger(__name__)

# Initialize the StrategyEngine
strategy_engine = StrategyEngine()

logger = logging.getLogger(__name__)


def compute_signal_strength(sym):
    s = ctx.STATES[sym]
    if len(s["closes"]) < 20:
        return (None, 0, None)

    # --- 新增 C：動能/成交量過濾 ---
    vol_ma10 = s.get("vol_ma10", 0.0)
    current_vol = s.get("current_vol", 0.0)
    if vol_ma10 > 0 and current_vol < vol_ma10 * 0.000015:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} [量能過濾] 當前量 {current_vol:.0f} < 均量 {vol_ma10:.0f} * 0.000015")
        return (None, 0, None)

    # --- 第三層防禦：極值檢查 (Extreme Value Defense) ---
    rsi = s.get("current_rsi", 50.0)
    rsi_extreme_low = s.get("rsi_extreme_low", 20)
    rsi_extreme_high = s.get("rsi_extreme_high", 75)

    if rsi < rsi_extreme_low:
        macd_hist_now = s.get("macd_line", 0.0) - s.get("macd_signal", 0.0)
        macd_hist_prev = s.get("prev_macd_line", 0.0) - s.get("prev_macd_signal", 0.0)
        rsi_history = s.get("rsi_history", [])
        is_hooking_up = len(rsi_history) >= 2 and rsi_history[-1] > rsi_history[-2]
        if not (is_hooking_up and macd_hist_now > macd_hist_prev):
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} [極值防禦] RSI {rsi:.1f} 尚未回勾且 MACD 未改善，拒絕接刀")
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

    profile = get_entry_strictness_profile()

    # Define parameters for dynamic RSI thresholds
    LONG_RSI_NORMAL = profile.get("rsi_long_floor", 25.0)
    SHORT_RSI_NORMAL = profile.get("rsi_short_floor", 25.0)
    LONG_RSI_HIGH_VOL = max(profile.get("rsi_long_floor", 25.0) - 5.0, 20.0)
    SHORT_RSI_HIGH_VOL = max(profile.get("rsi_short_floor", 25.0) + 10.0, 30.0)

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

    # 修改：不僅要求 MACD 方向對，還要求動能擴張，避免在動能衰竭時追高/殺低
    long_macd_ok = long_macd_cross or long_macd_hist_aligned
    short_macd_ok = short_macd_cross or short_macd_hist_aligned

    # --- 兩根 K 線加權強度評分器 ---
    # 原本的「連續性檢查」要求連續 N 根 K 線嚴格同向（close[i] > close[i-1]），太死板：
    # 只要其中一根雜訊回抽，整個訊號就被打回 0。改用 check_candle_strength()（見
    # core/indicators.py）：對最近兩根「已收盤」K 線分別判斷是否為陽線/陰線
    # （close > open / close < open），取平均分數跟門檻比較，不要求逐根比較前一根。
    # score_threshold=0.5：2 根裡有 1 根同向就算通過（基本門檻，取代舊版 CONSECUTIVE_COUNT=1）。
    # score_threshold=1.0：2 根都同向才算，用於額外加分判斷（取代舊版「連續 2 根」）。
    _is_bullish_candle = lambda candle: candle[4] > candle[1]  # close > open
    _is_bearish_candle = lambda candle: candle[4] < candle[1]  # close < open

    last_candle_long  = check_candle_strength(s["ohlcv"], _is_bullish_candle, score_threshold=0.5)
    last_candle_short = check_candle_strength(s["ohlcv"], _is_bearish_candle, score_threshold=0.5)

    # 加分判斷：兩根已收盤 K 線都同向，給予額外強度
    last_two_candles_long  = check_candle_strength(s["ohlcv"], _is_bullish_candle, score_threshold=1.0)
    last_two_candles_short = check_candle_strength(s["ohlcv"], _is_bearish_candle, score_threshold=1.0)

    # --- [新增] 分數與門檻 Debug 資訊 ---
    # 這裡的門檻可以根據需求調整，預設與之前邏輯對齊
    MIN_STRENGTH_THRESHOLD = 15.0 

    ema50 = s.get("ema50", 0.0)
    trend_confluence_long  = ema50 == 0.0 or close > ema50
    trend_confluence_short = ema50 == 0.0 or close < ema50

    sma200 = s.get("sma200_15m", 0)
    is_above_sma200 = sma200 > 0 and close > sma200 * 0.999
    is_below_sma200 = sma200 > 0 and close < sma200 * 1.001
    sma200_neutral   = sma200 == 0

    # 修改：收緊 EMA20 距離限制 (從 4% 縮小到 1.5%)，防止乖離過大時追價
    close_near_ema20_long  = ema20 <= 0 or close <= ema20 * 1.015
    close_near_ema20_short = ema20 <= 0 or close >= ema20 * 0.985
    is_in_bb_zone_long  = s.get("bb_low", 0) > 0 and close <= s["bb_low"] * 1.01
    is_in_bb_zone_short = s.get("bb_up",  0) > 0 and close >= s["bb_up"]  * 0.99
    
    # 新增：布林通道極限過濾 (防止買在上軌、空在下軌)
    bb_up = s.get("bb_up", 0)
    bb_low = s.get("bb_low", 0)
    not_overbought_bb = bb_up == 0 or close < bb_up * 0.995 # 不在布林上軌邊緣做多
    not_oversold_bb = bb_low == 0 or close > bb_low * 1.005 # 不在布林下軌邊緣做空

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

    # 極端反轉必須同時有 RSI 回勾、MACD 改善與反轉 K，不能只靠極端值猜底/猜頂。
    rsi_history = s.get("rsi_history", [])
    if rsi >= 80.0:
        rsi_hook = len(rsi_history) >= 2 and rsi_history[-1] < rsi_history[-2]
        if rsi_hook and macd_hist < prev_macd_hist and last_candle_short:
            strength = 15.0 + ((rsi - 80.0) / 2.0)
            return ("sell", strength, "Extreme_Reversal")
        logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} RSI 極端超買但反轉三確認未齊，暫不做空")

    if rsi <= 20.0:
        rsi_hook = len(rsi_history) >= 2 and rsi_history[-1] > rsi_history[-2]
        if rsi_hook and macd_hist > prev_macd_hist and last_candle_long:
            strength = 15.0 + ((20.0 - rsi) / 2.0)
            return ("buy", strength, "Extreme_Reversal")
        logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} RSI 極端超賣但反轉三確認未齊，暫不做多")

    # --- Route A/B 主要進場邏輯 ---
    # 這段原本存在，2026-07-11 被外部工具的一次提交（新增 StrategyEngine 之後）誤刪，
    # 導致上面算好的 raw_long_str/raw_short_str 只用來印 debug log，從未真正 return
    # 訊號——不管強度分數多高，實際上只有 RSI 極端反轉或 StrategyEngine 能觸發進場，
    # 這正是「強度都有 20+ 卻完全開不了倉」的根本原因。現從 commit 48cff79 復原。
    rsi_ok_long  = rsi < profile.get("rsi_long_ceiling", 75.0) and (rsi > profile.get("rsi_long_floor", 25.0) or (rsi >= max(profile.get("rsi_long_floor", 25.0) - 7.0, 20.0) and (long_macd_cross or macd_hist > 0)))
    rsi_ok_short = rsi > profile.get("rsi_short_floor", 25.0) and (rsi < profile.get("rsi_short_ceiling", 68.0) or (rsi <= profile.get("rsi_short_ceiling", 68.0) + 7.0 and (short_macd_cross or macd_hist < 0)))

    sma200_bonus_long  = 3.0 if is_above_sma200 else (-2.0 if (not sma200_neutral and is_below_sma200) else 0.0)
    sma200_bonus_short = 3.0 if is_below_sma200 else (-2.0 if (not sma200_neutral and is_above_sma200) else 0.0)

    is_relaxed = profile.get("min_signal_strength", 10.0) <= 10.0

    # Gate 0: SMA200 硬性守衛 (Hard Gate)，絕對不可豁免
    sma200_hard_gate_long  = sma200 <= 0 or close > sma200
    sma200_hard_gate_short = sma200 <= 0 or close < sma200

    # Gate 1: EMA50 方向 (寬鬆模式下僅供參考，不強制硬攔截)
    ema50_gate_long  = ema50 <= 0 or close > ema50 or is_relaxed
    ema50_gate_short = ema50 <= 0 or close < ema50 or is_relaxed

    # Gate 2: RSI 方向區間
    rsi_direction_long  = rsi > 25.0
    rsi_direction_short = rsi < 75.0

    # Gate 3: MACD 方向不僅一致，且必須擴張 (避免動能衰竭時進場)
    macd_ok_long  = long_macd_ok
    macd_ok_short = short_macd_ok

    # ── Route A: 標準順勢進場 ──────────────────────────────────────────────
    route_a_long = (
        sma200_hard_gate_long and
        macd_ok_long and
        (last_two_candles_long or is_relaxed) and
        rsi_ok_long and
        rsi_direction_long and
        ema50_gate_long and
        close_near_ema20_long and
        not_overbought_bb
    )

    route_a_short = (
        sma200_hard_gate_short and
        macd_ok_short and
        (last_two_candles_short or is_relaxed) and
        rsi_ok_short and
        rsi_direction_short and
        ema50_gate_short and
        close_near_ema20_short and
        not_oversold_bb
    )

    # ── Route B: EMA20 回測彈跳 ─────────────────────────────────────────────
    near_ema20_pullback = ema20 > 0 and abs(close - ema20) / ema20 <= 0.015
    ema20_above_ema50   = ema20 > 0 and ema50 > 0 and ema20 > ema50
    ema20_below_ema50   = ema20 > 0 and ema50 > 0 and ema20 < ema50

    route_b_long = (
        sma200_hard_gate_long and
        ema50_gate_long and
        ema20_above_ema50 and
        near_ema20_pullback and
        macd_ok_long and
        rsi_direction_long and
        rsi_ok_long and
        (last_two_candles_long or is_relaxed)
    )

    route_b_short = (
        sma200_hard_gate_short and
        ema50_gate_short and
        ema20_below_ema50 and
        near_ema20_pullback and
        macd_ok_short and
        rsi_direction_short and
        rsi_ok_short and
        (last_two_candles_short or is_relaxed)
    )

    long_base_ok  = route_a_long or route_b_long
    short_base_ok = route_a_short or route_b_short
    route_tag     = "b" if (route_b_long or route_b_short) else "a"

    if long_base_ok or short_base_ok:
        long_str = 0.0
        short_str = 0.0

        min_entry_strength = profile.get("min_entry_strength", 10.0)

        if long_base_ok:
            long_str = 12.0 + ((close - ema20) / max(ema20, 1e-8) * 100)
            if long_macd_cross:       long_str += 5.0
            if route_tag == "b":      long_str += 1.0
            if last_two_candles_long: long_str += 2.0
            long_str += l_ts + sma200_bonus_long

        if short_base_ok:
            short_str = 12.0 + ((ema20 - close) / max(ema20, 1e-8) * 100)
            if short_macd_cross:       short_str += 5.0
            if route_tag == "b":       short_str += 1.0
            if last_two_candles_short: short_str += 2.0
            short_str += s_ts + sma200_bonus_short

        if long_str >= short_str and long_base_ok:
            if long_str >= min_entry_strength:
                logger.info(f"@@COIN_DEBUG@@ ✅ {sym} Route {route_tag.upper()} 做多訊號 | Strength: {long_str:.1f}")
                return ("buy", long_str, route_tag)
        elif short_base_ok:
            if short_str >= min_entry_strength:
                logger.info(f"@@COIN_DEBUG@@ ✅ {sym} Route {route_tag.upper()} 做空訊號 | Strength: {short_str:.1f}")
                return ("sell", short_str, route_tag)

    # --- Route C: 量能衰竭進場策略 (Exhaustion Entry) ---
    if len(s["ohlcv"]) >= 50:
        c1 = s["ohlcv"][-2]
        c2 = s["ohlcv"][-3]

        current_atr = s.get("current_atr", 0.0)
        if not (atr_24h_avg > 0 and current_atr > atr_24h_avg * 2.0):
            c2_vol_low = c2[5] < s.get("vol_ma20", 1) * 0.65

            recent_low_50 = min([x[3] for x in s["ohlcv"][-50:]])
            recent_high_50 = max([x[2] for x in s["ohlcv"][-50:]])

            _exh_btc_4h = ctx.MARKET_WIND.get("btc_trend_4h", "NEUTRAL")

            # 多單：抓回檔底部
            if c2[4] < c2[1] and c2_vol_low:
                bb_low_v = s.get("bb_low", 0)
                is_near_sma = (sma200 > 0) and (abs(c1[3] - sma200) / sma200 < 0.005)
                is_near_low = (recent_low_50 > 0) and (c1[3] <= recent_low_50 * 1.005)
                support_ok = (bb_low_v > 0 and c1[3] <= bb_low_v * 1.005) or is_near_sma or is_near_low

                c2_mid = (c2[1] + c2[4]) / 2
                price_rebound = c1[4] > c2[4]
                has_lower_wick = (min(c1[1], c1[4]) - c1[3]) > abs(c1[4] - c1[1]) * 0.5
                crossed_midpoint = c1[4] > c2_mid
                pa_ok = price_rebound and has_lower_wick and crossed_midpoint
                bounce_ok = (c1[4] > c1[1]) and (c1[5] > c2[5] * 1.2) and crossed_midpoint

                trend_ok = (_exh_btc_4h != "BEAR")

                if trend_ok and support_ok and (pa_ok or bounce_ok):
                    logger.info(f"🌟 [量能衰竭] {sym} 觸發多單低接條件！(Support:{support_ok}, PA:{pa_ok}, Bounce:{bounce_ok})")
                    return ("buy", 15.0, "Exhaustion_Entry")

            # 空單：抓反彈頂部
            if c2[4] > c2[1] and c2_vol_low:
                bb_up_v = s.get("bb_up", 0)
                is_near_sma_res = (sma200 > 0) and (abs(c1[2] - sma200) / sma200 < 0.005)
                is_near_high = (recent_high_50 > 0) and (c1[2] >= recent_high_50 * 0.995)
                resistance_ok = (bb_up_v > 0 and c1[2] >= bb_up_v * 0.995) or is_near_sma_res or is_near_high

                c2_mid = (c2[1] + c2[4]) / 2
                price_rebound = c1[4] < c2[4]
                has_upper_wick = (c1[2] - max(c1[1], c1[4])) > abs(c1[4] - c1[1]) * 0.5
                crossed_midpoint = c1[4] < c2_mid
                pa_ok = price_rebound and has_upper_wick and crossed_midpoint
                bounce_ok = (c1[4] < c1[1]) and (c1[5] > c2[5] * 1.2) and crossed_midpoint

                trend_ok = (_exh_btc_4h != "BULL")

                if trend_ok and resistance_ok and (pa_ok or bounce_ok):
                    logger.info(f"🌟 [量能衰竭] {sym} 觸發空單高空條件！(Resistance:{resistance_ok}, PA:{pa_ok}, Bounce:{bounce_ok})")
                    return ("sell", 15.0, "Exhaustion_Entry")

    # --- 使用 StrategyEngine 進行多重過濾門檻 (Multi-Layer Filtering) ---
    strategy_signal = strategy_engine.check_signals(s.get("ohlcv", []))

    if strategy_signal:
        side, strength = strategy_signal
        # 轉換為小寫 side
        side_lower = side.lower()
        logger.info(f"@@COIN_DEBUG@@ 🛡️ {sym} 通過 StrategyEngine 過濾門檻 ({side_lower}) | Strength: {strength:.1f}")
        return (side_lower, strength, "StrategyEngine_Gate")

    # 最終拒絕原因日誌
    logger.debug(f"@@COIN_DEBUG@@ ❌ {sym} 無有效訊號 | LongScore:{raw_long_str:.1f} ShortScore:{raw_short_str:.1f}")
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

    # 0. SMA 200 硬性守衛：反手單也必須遵守大趨勢方向，不可繞過
    sma200 = s.get("sma200_15m", 0)
    current_price = s["close_price"]
    if sma200 > 0:
        if pending_side == "buy" and current_price < sma200:
            logger.info(f"🚫 [Reversal_SMA200_Block] {sym} 反手做多被拒：價格({current_price:.4f}) 在 SMA200({sma200:.4f}) 之下，大趨勢空頭，禁止反手做多。")
            return False
        if pending_side == "sell" and current_price > sma200:
            logger.info(f"🚫 [Reversal_SMA200_Block] {sym} 反手做空被拒：價格({current_price:.4f}) 在 SMA200({sma200:.4f}) 之上，大趨勢多頭，禁止反手做空。")
            return False

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
    # 不只看方向轉折，還要確認 MACD 柱狀圖「正在加速擴張」才算有效反手動能。
    # 例外：如果這筆是「攤平救援後很快又停損」的情況（pending_reverse_after_rescue），
    # 代表原方向的判斷已經被市場快速、明確地打臉，MACD 這種落後指標可能還來不及在
    # 同一根K線內完整反映，這時放寬成只要求「動能方向正在改善」，不用等到完全轉向
    # 又擴張——用意是這種價格已經強力反向的情況，不要因為指標太保守而錯過反手。
    macd_line = s.get("macd_line", 0.0)
    macd_signal_val = s.get("macd_signal", 0.0)
    prev_macd_line = s.get("prev_macd_line", 0.0)
    prev_macd_signal = s.get("prev_macd_signal", 0.0)

    macd_hist_now = macd_line - macd_signal_val
    macd_hist_prev = prev_macd_line - prev_macd_signal
    _after_rescue = s.get("pending_reverse_after_rescue", False)

    if pending_side == "buy":
        _ok = (macd_hist_now > macd_hist_prev) if _after_rescue else (macd_hist_now > 0 and macd_hist_now > macd_hist_prev)
        if not _ok:
            logger.info(f"📉 [Reversal_Weak_Momentum] {sym} 反手做多：MACD 動能不足 ({macd_hist_now:.6f} <= {macd_hist_prev:.6f}，攤平後放寬={_after_rescue})，放棄反手")
            return False
    elif pending_side == "sell":
        _ok = (macd_hist_now < macd_hist_prev) if _after_rescue else (macd_hist_now < 0 and macd_hist_now < macd_hist_prev)
        if not _ok:
            logger.info(f"📈 [Reversal_Weak_Momentum] {sym} 反手做空：MACD 動能不足 ({macd_hist_now:.6f} >= {macd_hist_prev:.6f}，攤平後放寬={_after_rescue})，放棄反手")
            return False

    # 4. 反手空間防護 (Space Buffer for Reverse)
    # 確保進場點不是在「剛好轉折」的最高/最低點過度追價 (限制在 0.5% 內)
    if pending_side == "buy":
        if current_price > prev_close * 1.005:
            logger.info(f"🛑 [Reversal_Chase_High] {sym} 反手做多：現價 ({current_price:.4f}) > 前收 1.005倍 ({prev_close * 1.005:.4f})，在轉折點過高處追價，拒絕")
            return False
    elif pending_side == "sell":
        if current_price < prev_close * 0.995:
            logger.info(f"🛑 [Reversal_Chase_Low] {sym} 反手做空：現價 ({current_price:.4f}) < 前收 0.995倍 ({prev_close * 0.995:.4f})，在轉折點過低處追價，拒絕")
            return False

    return True


async def is_eligible_for_reverse(sym, current_strength):
    """判斷是否允許反手：統一標準，避免多路徑衝突。"""
    s = ctx.STATES.get(sym)
    if not s or s.get("is_banned"):
        return False

    qty = float(s.get("qty", 0.0) or 0.0)
    avg = float(s.get("avg_price", 0.0) or 0.0)
    current = float(s.get("close_price", 0.0) or 0.0)
    losing = ((qty > 0 and current < avg) or (qty < 0 and current > avg)) and avg > 0 and current > 0
    reverse_threshold = 12.0 if losing else 15.0
    if current_strength < reverse_threshold:
        logger.info(f"⏳ [REVERSE_DENIED] {sym} 反手強度不足 ({current_strength:.1f} < {reverse_threshold:.1f})")
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


def _load_disabled_symbols():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {s.upper().replace(":USDT", "USDT") for s in data.get("disabled", [])}
    except Exception:
        return set()
