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
    # 每輪重算，避免介面沿用上一輪已失效的阻擋原因。
    s["entry_block_reason"] = ""
    if len(s["closes"]) < 20:
        s["entry_block_reason"] = "K 線資料不足（至少需要 20 根）"
        return (None, 0, None)

    # --- 新增 C：動能/成交量過濾 ---
    vol_ma10 = s.get("vol_ma10", 0.0)
    current_vol = s.get("current_vol", 0.0)
    if vol_ma10 > 0 and current_vol < vol_ma10 * 0.000015:
        s["entry_block_reason"] = "即時量能低於最低資料可信門檻"
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
            s["entry_block_reason"] = "RSI 極端超賣，但尚未回勾且 MACD 未改善"
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
    ma7 = s.get("ma7", 0.0)
    ma25 = s.get("ma25", 0.0)
    ma99 = s.get("ma99", 0.0)
    prev_ma99 = s.get("prev_ma99", 0.0)

    trend_long = ma7 > 0 and ma25 > 0 and ma7 > ma25
    trend_short = ma7 > 0 and ma25 > 0 and ma7 < ma25

    # 紫線 (MA99) 作為趨勢濾網：若紫線整體呈現向下，則短線只做空、不買入做多；若整體呈現向上，則只做多、不買入做空。
    ma99_trend_long = ma99 <= 0 or prev_ma99 <= 0 or ma99 >= prev_ma99
    ma99_trend_short = ma99 <= 0 or prev_ma99 <= 0 or ma99 <= prev_ma99

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

    # --- [新增] 分數與門檻 Debug 資訊 ---
    # 這裡的門檻可以根據需求調整，預設與之前邏輯對齊
    MIN_STRENGTH_THRESHOLD = 15.0 

    # For MA7 & MA25 trend confluence
    trend_confluence_long  = trend_long
    trend_confluence_short = trend_short

    ma99_direction_up = ma99 > 0 and prev_ma99 > 0 and ma99 > prev_ma99
    ma99_direction_down = ma99 > 0 and prev_ma99 > 0 and ma99 < prev_ma99
    ma99_neutral = ma99 == 0

    # 修改：收緊 MA7 距離限制 (從 4% 縮小到 1.5%)，防止乖離過大時追價，MA7 作為即時支撐與壓力
    close_near_ma7_long  = ma7 <= 0 or close <= ma7 * 1.015
    close_near_ma7_short = ma7 <= 0 or close >= ma7 * 0.985
    is_in_bb_zone_long  = s.get("bb_low", 0) > 0 and close <= s["bb_low"] * 1.01
    is_in_bb_zone_short = s.get("bb_up",  0) > 0 and close >= s["bb_up"]  * 0.99
    
    # 新增：布林通道極限過濾 (防止買在上軌、空在下軌)
    bb_up = s.get("bb_up", 0)
    bb_low = s.get("bb_low", 0)
    not_overbought_bb = bb_up == 0 or close < bb_up * 0.995 # 不在布林上軌邊緣做多
    not_oversold_bb = bb_low == 0 or close > bb_low * 1.005 # 不在布林下軌邊緣做空

    # 預先計算供 Log 顯示的預估強度
    l_ts = 0; s_ts = 0
    if ma99_direction_up: l_ts += 4; s_ts -= 3
    elif ma99_direction_down: l_ts -= 3; s_ts += 4
    if trend_confluence_long and (long_macd_cross or macd_hist > 0): l_ts += 5
    if trend_confluence_short and (short_macd_cross or macd_hist < 0): s_ts += 5
    if trend_confluence_short and (long_macd_cross or macd_hist > 0): l_ts -= 5
    if trend_confluence_long and (short_macd_cross or macd_hist < 0): s_ts -= 5

    raw_long_str = 12.0 + ((close - ma7) / max(ma7, 1e-8) * 100) + l_ts + (5.0 if long_macd_cross else 0.0)
    raw_short_str = 12.0 + ((ma7 - close) / max(ma7, 1e-8) * 100) + s_ts + (5.0 if short_macd_cross else 0.0)
    _reversal_rsi_low = 30.0
    _reversal_rsi_high = 70.0
    if rsi >= _reversal_rsi_high: raw_short_str = 15.0 + ((rsi - _reversal_rsi_high) / 2.0)
    if rsi <= _reversal_rsi_low: raw_long_str = 15.0 + ((_reversal_rsi_low - rsi) / 2.0)

    logger.info(f"@@COIN_DEBUG@@ 🔍 {sym} 條件檢測 | 原始評分(非有效訊號)(L/S): {raw_long_str:.1f}/{raw_short_str:.1f} | RSI動能(L>48/S<52): {rsi > 48.0}/{rsi < 52.0} | MA99趨勢(L/S): {ma99_direction_up}/{ma99_direction_down} | MACD多頭/空頭: {macd_hist > 0}/{macd_hist < 0} | 收盤價確認(L/S): {last_candle_long}/{last_candle_short} | MA7距離(L/S): {close_near_ma7_long}/{close_near_ma7_short} | BB區(L/S): {is_in_bb_zone_long}/{is_in_bb_zone_short} | MA25確認(L/S): {trend_confluence_long}/{trend_confluence_short}")

    # 極端反轉必須同時有 RSI 回勾、MACD 改善與反轉 K，不能只靠極端值猜底/猜頂。
    rsi_history = s.get("rsi_history", [])
    if rsi >= _reversal_rsi_high:
        rsi_hook = len(rsi_history) >= 2 and rsi_history[-1] < rsi_history[-2]
        if rsi_hook and macd_hist < prev_macd_hist and last_candle_short:
            if ma99_trend_short:
                strength = 15.0 + ((rsi - _reversal_rsi_high) / 2.0)
                return ("sell", strength, "Extreme_Reversal")
            else:
                logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} RSI {rsi:.1f} 進入反轉觀察區，但與 MA99 趨勢逆勢，拒絕做空")
        else:
            logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} RSI {rsi:.1f} 進入反轉觀察區，但回勾/MACD/K線三確認未齊，暫不做空")

    if rsi <= _reversal_rsi_low:
        rsi_hook = len(rsi_history) >= 2 and rsi_history[-1] > rsi_history[-2]
        if rsi_hook and macd_hist > prev_macd_hist and last_candle_long:
            if ma99_trend_long:
                strength = 15.0 + ((_reversal_rsi_low - rsi) / 2.0)
                return ("buy", strength, "Extreme_Reversal")
            else:
                logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} RSI {rsi:.1f} 進入止跌觀察區，但與 MA99 趨勢逆勢，拒絕做多")
        else:
            logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} RSI {rsi:.1f} 進入止跌觀察區，但回勾/MACD/K線三確認未齊，暫不做多")

    # --- Route A/B 主要進場邏輯 ---
    rsi_ok_long  = rsi < profile.get("rsi_long_ceiling", 75.0) and (rsi > profile.get("rsi_long_floor", 25.0) or (rsi >= max(profile.get("rsi_long_floor", 25.0) - 7.0, 20.0) and (long_macd_cross or macd_hist > 0)))
    rsi_ok_short = rsi > profile.get("rsi_short_floor", 25.0) and (rsi < profile.get("rsi_short_ceiling", 68.0) or (rsi <= profile.get("rsi_short_ceiling", 68.0) + 7.0 and (short_macd_cross or macd_hist < 0)))

    # MA99 趨勢方向加/減分：MA99 向上加多單分、MA99 向下加空單分
    ma99_bonus_long  = 3.0 if ma99_direction_up else (-2.0 if (not ma99_neutral and ma99_direction_down) else 0.0)
    ma99_bonus_short = 3.0 if ma99_direction_down else (-2.0 if (not ma99_neutral and ma99_direction_up) else 0.0)

    is_relaxed = profile.get("min_signal_strength", 10.0) <= 10.0

    # Gate 2: RSI 方向區間
    rsi_direction_long  = rsi > 25.0
    rsi_direction_short = rsi < 75.0

    # Gate 3: MACD 方向不僅一致，且必須擴張 (避免動能衰竭時進場)
    macd_ok_long  = long_macd_ok
    macd_ok_short = short_macd_ok

    # 加強確認：實測發現 Route A/B 共用的 macd_ok_*/rsi_ok_* 太鬆——只要 RSI 沒有逆勢、
    # MACD 柱狀圖剛好翻過零軸一點點就算數，趨勢其實還沒真的轉向，很快又被打回原方向
    # （ADA/SUI 實測案例：做空進場時 RSI 才 55~58、MACD 柱狀圖只有 -0.0002，幾乎貼零，
    # 進場沒多久 RSI 衝上 62.8、MACD 直接翻多頭）。MACD 連續兩讀數同向且擴張這道確認
    # 兩條路線都要加（Route A 跟 Route B 共用同一組 macd_ok_short，只改 Route B 會被
    # Route A 放行、等於沒真的擋掉問題）。但 RSI 要真的偏高/偏低這道確認只適用在
    # Route B：Route B 是「回測彈跳」，本質是等一個過熱/超賣的反彈點才進場，RSI 極端
    # 才有意義；Route A 是「標準順勢進場」，抓的是趨勢延續（例如 EMA20/50/RSI/MACD
    # 全部同向的健康拉回續漲），這種情況 RSI 本來就該落在 50~65 附近，不會是超賣區，
    # 硬性要求 RSI<=40 才能做多會直接擋掉這種教科書等級的順勢單（BCH 實測案例：RSI
    # 48+、站上 SMA200/EMA50、MACD 多頭，卻因為 RSI 沒到 40 以下被 Route A 拒絕）。
    _rsi_extreme_long  = rsi <= 40.0
    _rsi_extreme_short = rsi >= 60.0
    # Route A 只在「剛轉向」或「既有方向重新擴張」時進場。單純仍在零軸同側、但
    # 柱狀圖正在收斂，不再追單（UNI 3.509 空單即屬此類）。新交叉另要求 RSI 已站到
    # 新方向一側，避免 RSI 仍偏多時只因極小的 bearish cross 就過早做空，反向亦同。
    _macd_direction_long  = macd_hist > 0 and prev_macd_hist > 0
    _macd_direction_short = macd_hist < 0 and prev_macd_hist < 0
    _macd_confirmed_long  = _macd_direction_long and macd_hist > prev_macd_hist
    _macd_confirmed_short = _macd_direction_short and macd_hist < prev_macd_hist
    # 當前是否為低波動模式
    atr_24h_avg = s.get("atr_24h_avg", 0.0)
    current_atr = s.get("current_atr", 0.0)
    is_low_vol_signal = (atr_24h_avg > 0 and current_atr <= atr_24h_avg)
    _macd_stability_threshold = 0.70 if is_low_vol_signal else 0.85

    _macd_stable_long = (
        _macd_direction_long and (last_candle_long or is_low_vol_signal)
        and macd_hist >= prev_macd_hist * _macd_stability_threshold
    )
    _macd_stable_short = (
        _macd_direction_short and (last_candle_short or is_low_vol_signal)
        and abs(macd_hist) >= abs(prev_macd_hist) * _macd_stability_threshold
    )
    _macd_turn_long = long_macd_cross and rsi >= 48.0
    _macd_turn_short = short_macd_cross and rsi <= 52.0
    _route_a_macd_long = _macd_confirmed_long or _macd_stable_long or _macd_turn_long
    _route_a_macd_short = _macd_confirmed_short or _macd_stable_short or _macd_turn_short

    # ── Route A: 標準順勢進場 ──────────────────────────────────────────────
    # BTC 4H 大盤過濾已移至 entry_filter.py 的 MACRO_BLOCK（含豁免條件）統一處理。
    # signal_engine 只評估幣種自身技術面，避免雙重過濾導致訊號無法生成。
    route_a_long = (
        _route_a_macd_long and
        rsi_ok_long and
        rsi_direction_long and
        trend_long and
        close_near_ma7_long and
        ma99_trend_long
    )

    route_a_short = (
        _route_a_macd_short and
        rsi_ok_short and
        rsi_direction_short and
        trend_short and
        close_near_ma7_short and
        ma99_trend_short
    )

    # BTC 方向僅保留用於 debug/log，不直接 gate 訊號
    _btc_trend = ctx.MARKET_WIND.get("btc_trend_4h", "NEUTRAL")
    _long_btc_ok  = _btc_trend != "BEAR"
    _short_btc_ok = _btc_trend != "BULL"

    # ── Route B: MA7 回測彈跳 ─────────────────────────────────────────────
    near_ma7_pullback = ma7 > 0 and abs(close - ma7) / ma7 <= 0.015
    ma7_above_ma25   = ma7 > 0 and ma25 > 0 and ma7 > ma25
    ma7_below_ma25   = ma7 > 0 and ma25 > 0 and ma7 < ma25

    route_b_long = (
        trend_long and
        ma7_above_ma25 and
        near_ma7_pullback and
        _macd_confirmed_long and
        _rsi_extreme_long and
        rsi_direction_long and
        rsi_ok_long and
        ma99_trend_long
        # BTC 大盤過濾由 entry_filter 統一處理
    )

    route_b_short = (
        trend_short and
        ma7_below_ma25 and
        near_ma7_pullback and
        _macd_confirmed_short and
        _rsi_extreme_short and
        rsi_direction_short and
        rsi_ok_short and
        ma99_trend_short
    )

    long_base_ok  = route_a_long or route_b_long
    short_base_ok = route_a_short or route_b_short
    route_tag     = "b" if (route_b_long or route_b_short) else "a"

    # 原始分數只是加權觀察值；真正候選仍須通過 Route A/B 的全部硬條件。
    # 把主要方向缺少的關卡寫進 state，讓狀態頁不再只顯示籠統的「暫無有效訊號」。
    _long_route_a_gates = (
        ("MACD多頭擴張", _route_a_macd_long),
        ("多方收盤K", last_candle_long),
        ("RSI多方區間", rsi_ok_long and rsi_direction_long),
        ("MA25多頭(MA7>MA25)", trend_long),
        ("MA7距離", close_near_ma7_long),
    )
    _short_route_a_gates = (
        ("MACD空頭擴張", _route_a_macd_short),
        ("空方收盤K", last_candle_short or is_relaxed),
        ("RSI空方區間", rsi_ok_short and rsi_direction_short),
        ("MA25空頭(MA7<MA25)", trend_short),
        ("MA7距離", close_near_ma7_short),
    )
    _preferred_side = "多單" if raw_long_str >= raw_short_str else "空單"
    _preferred_gates = _long_route_a_gates if _preferred_side == "多單" else _short_route_a_gates
    _missing_preferred_gates = [name for name, passed in _preferred_gates if not passed]

    if long_base_ok or short_base_ok:
        long_str = 0.0
        short_str = 0.0

        min_entry_strength = profile.get("min_entry_strength", 10.0)

        if long_base_ok:
            long_str = 12.0 + ((close - ma7) / max(ma7, 1e-8) * 100)
            if long_macd_cross:       long_str += 5.0
            if route_tag == "b":      long_str += 1.0
            long_str += l_ts + ma99_bonus_long

        if short_base_ok:
            short_str = 12.0 + ((ma7 - close) / max(ma7, 1e-8) * 100)
            if short_macd_cross:       short_str += 5.0
            if route_tag == "b":       short_str += 1.0
            short_str += s_ts + ma99_bonus_short

        if long_str >= short_str and long_base_ok:
            if long_str >= min_entry_strength:
                logger.info(f"@@COIN_DEBUG@@ ✅ {sym} Route {route_tag.upper()} 做多訊號 | Strength: {long_str:.1f}")
                s["entry_block_reason"] = ""
                return ("buy", long_str, route_tag)
        elif short_base_ok:
            if short_str >= min_entry_strength:
                logger.info(f"@@COIN_DEBUG@@ ✅ {sym} Route {route_tag.upper()} 做空訊號 | Strength: {short_str:.1f}")
                s["entry_block_reason"] = ""
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
                is_near_low = (recent_low_50 > 0) and (c1[3] <= recent_low_50 * 1.005)
                support_ok = (bb_low_v > 0 and c1[3] <= bb_low_v * 1.005) or is_near_low

                c2_mid = (c2[1] + c2[4]) / 2
                price_rebound = c1[4] > c2[4]
                has_lower_wick = (min(c1[1], c1[4]) - c1[3]) > abs(c1[4] - c1[1]) * 0.5
                crossed_midpoint = c1[4] > c2_mid
                pa_ok = price_rebound and has_lower_wick and crossed_midpoint
                bounce_ok = (c1[4] > c1[1]) and (c1[5] > c2[5] * 1.2) and crossed_midpoint

                trend_ok = True

                if trend_ok and support_ok and (pa_ok or bounce_ok) and ma99_trend_long:
                    logger.info(f"🌟 [量能衰竭] {sym} 觸發多單低接條件！(Support:{support_ok}, PA:{pa_ok}, Bounce:{bounce_ok})")
                    return ("buy", 15.0, "Exhaustion_Entry")

            # 空單：抓反彈頂部 - 要求更嚴格的確認，避免開錯方向
            if c2[4] > c2[1] and c2_vol_low:
                bb_up_v = s.get("bb_up", 0)
                is_near_high = (recent_high_50 > 0) and (c1[2] >= recent_high_50 * 0.995)
                resistance_ok = (bb_up_v > 0 and c1[2] >= bb_up_v * 0.995) or is_near_high

                c2_mid = (c2[1] + c2[4]) / 2
                price_rebound = c1[4] < c2[4]
                has_upper_wick = (c1[2] - max(c1[1], c1[4])) > abs(c1[4] - c1[1]) * 0.5
                crossed_midpoint = c1[4] < c2_mid
                # PA 確認必要：需要有上影線插針且已跌穿前根中點
                pa_ok = price_rebound and has_upper_wick and crossed_midpoint
                bounce_ok = (c1[4] < c1[1]) and (c1[5] > c2[5] * 1.2) and crossed_midpoint

                # 嚴格版：BTC 4H 必須明確偏空或至少中性 (已註銷：短線以 1m 為主，不看 4H 宏觀面)
                trend_ok = True

                # 新增：MACD 確認（柱狀圖必須已翻負或 RSI 偏高）
                _exh_rsi = s.get("current_rsi", 50.0)
                _exh_macd = s.get("macd_hist", 0.0)
                _exh_macd_confirm = _exh_macd < 0 or _exh_rsi >= 60.0

                # 嚴格版：只有 PA 確認（插針反轉）才夠資格，單純量縮陰線不夠
                if trend_ok and resistance_ok and _exh_macd_confirm and pa_ok and ma99_trend_short:
                    logger.info(f"🌟 [量能衰竭] {sym} 觸發空單高空條件！(Resistance:{resistance_ok}, PA:{pa_ok}, MACD_Confirm:{_exh_macd_confirm})")
                    return ("sell", 15.0, "Exhaustion_Entry")

    # --- 使用 StrategyEngine 進行多重過濾門檻 (Multi-Layer Filtering) ---
    strategy_signal = strategy_engine.check_signals(s.get("ohlcv", []))

    if strategy_signal:
        side, strength = strategy_signal
        # 轉換為小寫 side
        side_lower = side.lower()
        logger.info(f"@@COIN_DEBUG@@ 🛡️ {sym} 通過 StrategyEngine 過濾門檻 ({side_lower}) | Strength: {strength:.1f}")
        s["entry_block_reason"] = ""
        return (side_lower, strength, "StrategyEngine_Gate")

    # 最終拒絕原因日誌
    _missing_text = "、".join(_missing_preferred_gates) if _missing_preferred_gates else "替代路徑條件"
    s["entry_block_reason"] = (
        f"{_preferred_side}原始評分 {max(raw_long_str, raw_short_str):.1f}，"
        f"但硬條件未齊：{_missing_text}"
    )
    logger.info(
        f"@@COIN_DEBUG@@ ⛔ {sym} 未形成有效進場 | {s['entry_block_reason']} "
        f"| L/S原始評分:{raw_long_str:.1f}/{raw_short_str:.1f}"
    )
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

    current_price = s["close_price"]

    # 1. 大盤方向過濾 (已註銷：短線以 1m 為主，不看 4H 宏觀面)


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
