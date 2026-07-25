import logging
import json

import numpy as np

from core import ctx
from core.config import CONFIG_FILE
from core.indicators import calculate_keltner_channels, calculate_supertrend, bars_since_supertrend_flip

logger = logging.getLogger(__name__)


def compute_signal_strength(sym, realtime_trigger=False):
    """Keltner Channel 突破 + SuperTrend 轉向為進場依據，搭配動態波段過濾。

    [2026-07-25] 使用者指示：完全取代 MA_Cross/MA_Breakout/MA25_Pullback/MA7_Simple
    四條路線，目標高頻高勝率（一天 15~30 筆）：
      1. 5 分鐘 K 線（TIMEFRAME 本來就是 5m，一天 288 根，開倉機會足夠）
      2. 大趨勢過濾改用「動態波段」EMA20/EMA50（取代僵硬的 EMA200），
         抓中短線趨勢，不會把短線高勝率波段突破濾掉
      3. 動態 RSI 超買超賣防守，避免插針/低流動性雜訊誤觸發

    多單：即時價格突破 Keltner 通道上軌 + SuperTrend 多頭 + EMA20>=EMA50 + RSI>=45
    空單：即時價格跌破 Keltner 通道下軌 + SuperTrend 空頭 + EMA20<=EMA50 + RSI<=55
    通道與 SuperTrend 用已收盤 K 棒計算（避免每個 tick 通道本身跟著即時價格抖動）；
    突破判斷則對照最新一筆即時更新中的 close，讓訊號能在掃描週期內立即反應。

    另外疊加 4 道品質濾網（使用者要求，皆與舊版 MA7/MA25/MA99 邏輯無關）：
      1. 防插針：突破比對用 SpikeFilter_L2 修正後的成交中位數價，而非未濾波的即時
         成交價，避免 Testnet 稀薄流動性造成的單根插針雜訊直接誤觸發
      2. 突破幅度緩衝：價格需超出通道邊界一定比例（通道寬度 x margin），拒絕貼線
         即回的邊緣訊號
      3. 量能確認：當下量須達 20 期均量一定比例，拒絕無量假突破
      4. SuperTrend 新鮮度：目前方向須在最近 N 根已收盤K棒內才剛轉向，避免追一個
         已經走了很久、隨時可能回頭的老趨勢

    既有出場與風控機制（硬停損、移動停利、停滯超時、相關性折扣、方向集中度等）
    完全不受影響，這裡只取代「要不要開倉」的判斷本身。
    """
    from core.config import (
        KELTNER_EMA_PERIOD, KELTNER_ATR_PERIOD, KELTNER_ATR_MULTIPLIER,
        SUPERTREND_ATR_PERIOD, SUPERTREND_MULTIPLIER,
        KELTNER_BREAKOUT_MARGIN_PCT, KELTNER_MIN_VOLUME_RATIO, SUPERTREND_MAX_FLIP_AGE_BARS,
    )
    s = ctx.STATES[sym]
    s["entry_block_reason"] = ""
    candles = s.get("ohlcv", [])
    min_len = max(KELTNER_EMA_PERIOD, SUPERTREND_ATR_PERIOD, 50) + 2
    if len(candles) < min_len:
        s["entry_block_reason"] = "K 線資料尚未足夠計算 Keltner/SuperTrend/EMA"
        return (None, 0, None)

    ema20 = float(s.get("ema20", 0.0) or 0.0)
    ema50 = float(s.get("ema50", 0.0) or 0.0)
    current_rsi = float(s.get("current_rsi", 50.0) or 50.0)
    if ema20 <= 0 or ema50 <= 0:
        s["entry_block_reason"] = "EMA20/EMA50 尚未計算完成"
        return (None, 0, None)

    completed = candles[:-1]
    closes = np.array([float(c[4]) for c in completed])
    highs = np.array([float(c[2]) for c in completed])
    lows = np.array([float(c[3]) for c in completed])

    kc_upper, kc_mid, kc_lower = calculate_keltner_channels(
        closes, highs, lows, KELTNER_EMA_PERIOD, KELTNER_ATR_PERIOD, KELTNER_ATR_MULTIPLIER
    )
    if kc_upper <= 0 or kc_lower <= 0:
        s["entry_block_reason"] = "Keltner 通道尚未計算完成"
        return (None, 0, None)

    st_values, st_direction = calculate_supertrend(
        highs, lows, closes, SUPERTREND_ATR_PERIOD, SUPERTREND_MULTIPLIER
    )
    if len(st_direction) == 0:
        s["entry_block_reason"] = "SuperTrend 尚未計算完成"
        return (None, 0, None)

    current_dir = int(st_direction[-1])
    flip_age = bars_since_supertrend_flip(st_direction)
    # 防插針：優先採用 SpikeFilter_L2 已修正過的價格；沒有這個欄位（例如尚未跑過
    # 即時 tick 處理）時退回原始 close，避免因缺欄位而整條路線失效。
    live_price = float(s.get("close_price_spike_filtered", 0.0) or candles[-1][4])

    s["keltner_upper"] = kc_upper
    s["keltner_mid"] = kc_mid
    s["keltner_lower"] = kc_lower
    s["supertrend_direction"] = current_dir

    kc_width = max(kc_upper - kc_lower, 1e-8)
    margin = kc_width * KELTNER_BREAKOUT_MARGIN_PCT
    current_vol = float(s.get("current_vol", 0.0) or 0.0)
    vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)
    volume_ratio = (current_vol / vol_ma20) if vol_ma20 > 0 else 0.0
    volume_ok = volume_ratio >= KELTNER_MIN_VOLUME_RATIO
    flip_fresh = flip_age <= SUPERTREND_MAX_FLIP_AGE_BARS

    trend_ok_long = ema20 >= ema50
    trend_ok_short = ema20 <= ema50
    rsi_ok_long = current_rsi >= 45.0
    rsi_ok_short = current_rsi <= 55.0

    long_breakout = (live_price > kc_upper + margin and current_dir == 1
                      and trend_ok_long and rsi_ok_long and volume_ok and flip_fresh)
    short_breakout = (live_price < kc_lower - margin and current_dir == -1
                       and trend_ok_short and rsi_ok_short and volume_ok and flip_fresh)

    if not long_breakout and not short_breakout:
        s["entry_block_reason"] = (
            f"等待 Keltner 突破(含緩衝) + SuperTrend 同向新鮮 + EMA20/50 波段同向 + "
            f"RSI 動能 + 量能確認 "
            f"(價={live_price:.6f}, KC上={kc_upper:.6f}, KC下={kc_lower:.6f}, "
            f"ST方向={'多' if current_dir == 1 else '空'}(第{flip_age}根), "
            f"EMA20={ema20:.6f}, EMA50={ema50:.6f}, RSI={current_rsi:.1f}, "
            f"量能={volume_ratio:.2f}x)"
        )
        return (None, 0, None)

    side = "buy" if long_breakout else "sell"
    route = "Keltner_SuperTrend"
    breakout_pct = ((live_price - kc_upper) / kc_width) if long_breakout else ((kc_lower - live_price) / kc_width)
    strength = 25.0 + min(max(breakout_pct, 0.0) * 40.0, 10.0)

    s["ma_signal_candle_ts"] = int(completed[-1][0]) if len(completed) else 0
    logger.info(
        f"@@COIN_DEBUG@@ ✅ {sym} [{route}] {side} | 價={live_price:.6f} "
        f"KC上={kc_upper:.6f} KC中={kc_mid:.6f} KC下={kc_lower:.6f} ST方向={'多' if current_dir == 1 else '空'}(第{flip_age}根) "
        f"EMA20={ema20:.6f} EMA50={ema50:.6f} RSI={current_rsi:.1f} 量能={volume_ratio:.2f}x"
    )
    return (side, strength, route)



# Legacy RSI/MACD/BB entry routes were removed when the MA lifecycle became authoritative.

def _load_disabled_symbols():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {s.upper().replace(":USDT", "USDT") for s in data.get("disabled", [])}
    except Exception:
        return set()


def _find_horizontal_zones(candles, atr, lookback, min_touches, tolerance_atr,
                           current_price=0.0, min_width_pct=0.0):
    """在已收盤 K 棒中找水平支撐帶與壓力帶。

    演算法：
    1. 收集所有已收盤 K 棒的低點（支撐候選）和高點（壓力候選）。
    2. 以 ATR × tolerance_atr 為群聚半徑，把接近的價位合併為「水平帶」。
    3. 過濾掉觸碰次數 < min_touches 的帶（太少觸碰 = 未確認支撐/壓力）。
    4. 回傳 (最強支撐帶中心, 最強壓力帶中心)，無則回傳 None。

    Args:
        candles: 已收盤 K 棒列表（不含當前未收盤那根）
        atr: 當前 ATR
        lookback: 最多往回看幾根
        min_touches: 確認水平帶的最低觸碰次數
        tolerance_atr: 群聚半徑（ATR 倍數）

    Returns:
        (support_center, resistance_center)，找不到時為 None
    """
    if not candles or atr <= 0:
        return None, None

    recent = candles[-lookback:] if len(candles) >= lookback else candles
    lows  = [float(c[3]) for c in recent]
    highs = [float(c[2]) for c in recent]
    band  = atr * tolerance_atr

    def cluster(prices):
        """把相近價位聚合成帶，回傳 (中心價, 觸碰次數) 的列表。"""
        if not prices:
            return []
        sorted_p = sorted(prices)
        groups = []
        current = [sorted_p[0]]
        for p in sorted_p[1:]:
            if p - current[0] <= band:
                current.append(p)
            else:
                groups.append(current)
                current = [p]
        groups.append(current)
        return [(sum(g) / len(g), len(g)) for g in groups]

    support_clusters    = [(c, t) for c, t in cluster(lows)  if t >= min_touches]
    resistance_clusters = [(c, t) for c, t in cluster(highs) if t >= min_touches]

    # 區間交易要使用「現價下方最近支撐／現價上方最近壓力」。舊版只取觸碰
    # 次數最多，次數相同時會因排序順序拿到最遠價位，甚至把不同區段硬配成一個區間。
    if current_price > 0:
        supports_below = [(c, t) for c, t in support_clusters if c <= current_price]
        resistances_above = [(c, t) for c, t in resistance_clusters if c >= current_price]
        if min_width_pct > 0:
            valid_pairs = [
                (support_row, resistance_row)
                for support_row in supports_below
                for resistance_row in resistances_above
                if support_row[0] > 0
                and (resistance_row[0] - support_row[0]) / support_row[0] >= min_width_pct
            ]
            if not valid_pairs:
                return None, None
            support_row, resistance_row = min(
                valid_pairs,
                key=lambda pair: (
                    (pair[1][0] - pair[0][0]) / pair[0][0],
                    -(pair[0][1] + pair[1][1]),
                    abs(((pair[0][0] + pair[1][0]) / 2.0) - current_price),
                ),
            )
            return support_row[0], resistance_row[0]
        support = max(supports_below, key=lambda x: x[0])[0] if supports_below else None
        resistance = min(resistances_above, key=lambda x: x[0])[0] if resistances_above else None
    else:
        support = max(support_clusters, key=lambda x: x[1])[0] if support_clusters else None
        resistance = max(resistance_clusters, key=lambda x: x[1])[0] if resistance_clusters else None

    return support, resistance


def compute_range_signal(sym):
    """在 ADX 低（區間行情）時，偵測水平支撐/壓力並產生進場訊號。

    條件（全部需滿足）：
    - RANGE_MODE_ENABLED 為 True
    - ADX < RANGE_ADX_THRESHOLD（確認區間行情）
    - 找到有足夠觸碰次數的支撐帶或壓力帶
    - 區間寬度（壓力 - 支撐）> 手續費 + RANGE_MIN_NET_PROFIT_PCT（空間保護）
    - 當前收盤 K 棒確認回彈（做多）或拒絕（做空）：
        做多：低點進入支撐帶誤差帶 且 收盤 > 支撐帶中心（收陽或長下影）
        做空：高點進入壓力帶誤差帶 且 收盤 < 壓力帶中心（收陰或長上影）
    - RSI：做多 < 55，做空 > 45

    Returns:
        (side, strength, route) 或 (None, 0, None)
    """
    from core.config import (
        RANGE_MODE_ENABLED, RANGE_ADX_THRESHOLD, RANGE_LOOKBACK,
        RANGE_TOUCH_COUNT, RANGE_TOUCH_ATR_TOLERANCE,
        RANGE_MIN_NET_PROFIT_PCT, STRICT_ENTRY_SYMBOLS,
        STRICT_RANGE_MIN_NET_PROFIT_PCT, TAKER_FEE_RATE,
    )

    if not RANGE_MODE_ENABLED:
        return (None, 0, None)

    s = ctx.STATES.get(sym)
    if not s:
        return (None, 0, None)

    candles = s.get("ohlcv", [])
    if len(candles) < 22:
        s["entry_block_reason"] = "K 棒資料不足（區間模式）"
        return (None, 0, None)

    atr = float(s.get("current_atr", 0.0) or 0.0)
    adx = float(s.get("adx", 0.0) or 0.0)
    rsi = float(s.get("current_rsi", 50.0) or 50.0)
    prev_rsi = float(s.get("prev_rsi", 50.0) or 50.0)
    vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)

    if atr <= 0 or vol_ma20 <= 0:
        s["entry_block_reason"] = (
            f"ATR 或成交量資料不足（區間模式：ATR={atr:.8g}, VolMA20={vol_ma20:.2f}）"
        )
        return (None, 0, None)

    # 1. ADX 確認區間行情；高 ADX 交給 MA 趨勢路線，避免前段產生 Range 訊號、
    # 送單前又被同一個 ADX 門檻取消。
    if adx >= RANGE_ADX_THRESHOLD:
        s["entry_block_reason"] = f"ADX={adx:.1f} ≥ {RANGE_ADX_THRESHOLD}，趨勢明顯，改由 MA 策略評估"
        logger.info(f"⏳ {sym} [Range] ADX={adx:.1f} 過高，略過區間模式")
        return (None, 0, None)

    # 1.5. ATR 波動比例過濾（高於 6.0% 判定為高波動妖幣，不開區間倉以防突破止損）
    close_price = candles[-1][4] if candles else 0.0
    atr_pct = (atr / close_price) if close_price > 0 else 0.0
    if atr_pct > 0.06:
        s["entry_block_reason"] = f"ATR波動佔比={atr_pct*100:.2f}% > 6.0%，波動過大，略過區間模式"
        logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [Range] ATR波動比例 ({atr_pct*100:.2f}%) 過高，略過區間模式")
        return (None, 0, None)

    # 2. 辨識水平支撐/壓力帶（只用已收盤 K 棒，排除最後一根）
    completed = candles[:-1]
    signal_close = float(completed[-1][4])
    range_min_net_pct = (
        STRICT_RANGE_MIN_NET_PROFIT_PCT
        if sym in STRICT_ENTRY_SYMBOLS else RANGE_MIN_NET_PROFIT_PCT
    )
    min_range_width_pct = TAKER_FEE_RATE * 2 + range_min_net_pct
    support, resistance = _find_horizontal_zones(
        completed, atr, RANGE_LOOKBACK, RANGE_TOUCH_COUNT, RANGE_TOUCH_ATR_TOLERANCE,
        current_price=signal_close, min_width_pct=min_range_width_pct,
    )

    if support is None or resistance is None or support >= resistance:
        s["entry_block_reason"] = "需要同時找到現價下方支撐與上方壓力帶"
        return (None, 0, None)

    # 3. 空間保護：區間寬度必須能覆蓋手續費 + 最低獲利空間
    round_trip_fee = TAKER_FEE_RATE * 2
    min_range_width_pct = round_trip_fee + range_min_net_pct
    if support is not None and resistance is not None:
        range_width_pct = (resistance - support) / support if support > 0 else 0.0
        if range_width_pct < min_range_width_pct:
            s["entry_block_reason"] = (
                f"區間寬度 {range_width_pct*100:.2f}% < 最低需求 {min_range_width_pct*100:.2f}%（手續費+獲利空間）"
            )
            logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [Range] 區間過窄，略過")
            return (None, 0, None)

    # 4. 訊號 K 棒。ETH/XRP 曾出現只靠一根短暫拒跌 K 棒就逆著 15m
    # 趨勢做多，下一根隨即破底；嚴格幣種因此要用前一根作為觸碰／拒絕，
    # 再由最新已收盤 K 棒確認 higher-low + higher-close（做空反向）。
    # [2026-07-24 修正] 實測 23 筆非 ETH/XRP 的區間單，贏率只有 26%，且幾乎每筆
    # 峰值都很弱（0.3%~0.6%）就反轉——正是這條註解描述的「只靠一根拒跌就進場，
    # 下一根隨即破底」同一種失敗模式，只是這個二次確認機制過去只套用在 ETH/XRP
    # 身上。既然證實這是普遍問題，二次確認（第二根收線 higher-low/lower-high
    # + 15m EMA 同向）改為對所有幣種一視同仁。
    strict_reversal_confirmation = True
    if strict_reversal_confirmation and len(candles) < 23:
        s["entry_block_reason"] = "等待第二根收線確認區間反轉"
        return (None, 0, None)
    sig = candles[-3] if strict_reversal_confirmation else candles[-2]
    confirmation = candles[-2] if strict_reversal_confirmation else None
    candle_open  = float(sig[1])
    candle_high  = float(sig[2])
    candle_low   = float(sig[3])
    candle_close = float(sig[4])
    candle_vol   = float(sig[5])
    volume_ratio = candle_vol / vol_ma20

    touch_band = atr * RANGE_TOUCH_ATR_TOLERANCE
    body = max(abs(candle_close - candle_open), candle_close * 0.0001)
    lower_wick = min(candle_open, candle_close) - candle_low
    upper_wick = candle_high - max(candle_open, candle_close)
    bullish_rejection = candle_close > candle_open or lower_wick >= body * 1.5
    bearish_rejection = candle_close < candle_open or upper_wick >= body * 1.5

    strict_long_confirmation = True
    strict_short_confirmation = True
    if strict_reversal_confirmation:
        ema20_15m = float(s.get("ema20_15m", 0.0) or 0.0)
        ema50_15m = float(s.get("ema50_15m", 0.0) or 0.0)
        if min(ema20_15m, ema50_15m) <= 0:
            s["entry_block_reason"] = "等待 15m EMA20／EMA50 完成，避免把短暫反彈誤認為反轉"
            return (None, 0, None)

        confirm_open = float(confirmation[1])
        confirm_high = float(confirmation[2])
        confirm_low = float(confirmation[3])
        confirm_close = float(confirmation[4])
        strict_long_confirmation = (
            ema20_15m >= ema50_15m
            and confirm_close > confirm_open
            and confirm_close > candle_close
            and confirm_low > candle_low
        )
        strict_short_confirmation = (
            ema20_15m <= ema50_15m
            and confirm_close < confirm_open
            and confirm_close < candle_close
            and confirm_high < candle_high
        )

    # 實測 ADAUSDT 案例：支撐反彈訊號觸發時 RSI=50.0，看似正常，但短短不到
    # 一分鐘內連續幾輪掃描 RSI 一路殺到 33.3、29.4，代表當下賣壓根本還沒停，
    # 進場後價格直接跌破支撐、從未反彈（峰值 0%）。原本只檢查 RSI 是否低於
    # 55/高於 45 這種靜態門檻，抓不到「動能還在惡化中」的情況。這裡改成同時
    # 要求 RSI 沒有在最近一幾輪掃描間快速朝反方向惡化。
    RANGE_RSI_MOMENTUM_GUARD_PCT = 8.0
    rsi_not_still_falling = (prev_rsi - rsi) <= RANGE_RSI_MOMENTUM_GUARD_PCT
    rsi_not_still_rising = (rsi - prev_rsi) <= RANGE_RSI_MOMENTUM_GUARD_PCT

    # 5. 做多條件：低點碰支撐帶 且 收盤回彈至支撐上方 且 RSI < 50 (原55，收緊防追高)
    long_signal = (
        support is not None
        and candle_low <= support + touch_band      # 低點觸碰支撐帶
        and candle_close >= support                  # 收盤回彈至支撐上方
        and bullish_rejection                        # 收陽或更長下影確認拒跌
        and rsi < 50.0                               # 排除偏高位時的假支撐
        and rsi_not_still_falling                     # 排除賣壓還在惡化中的假支撐
        and strict_long_confirmation                  # ETH/XRP：15m 同向且第二根收線確認反轉
    )

    # 6. 做空條件：高點碰壓力帶 且 收盤回落至壓力下方 且 RSI > 50 (原45，收緊防低位做空)
    short_signal = (
        resistance is not None
        and candle_high >= resistance - touch_band   # 高點觸碰壓力帶
        and candle_close <= resistance               # 收盤回落至壓力下方
        and bearish_rejection                        # 收陰或更長上影確認拒漲
        and rsi > 50.0                               # 排除偏低位時的假壓力
        and rsi_not_still_rising                      # 排除買壓還在惡化中的假壓力
        and strict_short_confirmation                 # ETH/XRP：15m 同向且第二根收線確認反轉
    )

    if not long_signal and not short_signal:
        if strict_reversal_confirmation:
            s["entry_block_reason"] = "ETH/XRP 區間反轉未確認：需 15m 趨勢同向及第二根收線形成更高低點／更低高點"
        else:
            s["entry_block_reason"] = "價格未確認觸碰支撐/壓力後回彈/拒絕（區間模式）"
        return (None, 0, None)

    # 確保兩個訊號不會同時成立（優先支撐做多，壓力做空次之）
    if long_signal and short_signal:
        # 價格更接近支撐就做多，更接近壓力就做空
        if support is not None and resistance is not None:
            mid = (support + resistance) / 2
            long_signal  = candle_close <= mid
            short_signal = not long_signal
        else:
            short_signal = False  # 只有支撐時做多

    side  = "buy"  if long_signal  else "sell"
    route = "Range_Support_Long" if long_signal else "Range_Resistance_Short"

    # 7. 計算訊號強度
    # 基礎分：18（剛好達到 RANGE_MIN_SIGNAL_STRENGTH）
    # 加分：量能比例（最多 +5）、RSI 距中線的距離（最多 +3）、ADX 越低區間越穩（最多 +2）
    base_strength = 18.0
    vol_bonus = min(max(volume_ratio - 0.5, 0.0) * 4.0, 5.0)
    rsi_dist  = abs(rsi - 50.0) / 50.0
    rsi_bonus = rsi_dist * 3.0
    adx_bonus = max(0.0, (RANGE_ADX_THRESHOLD - adx) / RANGE_ADX_THRESHOLD) * 2.0
    strength  = round(base_strength + vol_bonus + rsi_bonus + adx_bonus, 4)

    # 8. 將識別到的支撐/壓力寫入狀態，供進場過濾與出場計算使用
    s["range_support_level"]   = support    if support    is not None else 0.0
    s["range_resistance_level"] = resistance if resistance is not None else 0.0

    logger.info(
        f"@@COIN_DEBUG@@ ✅ {sym} [{route}] {side} | close={candle_close:.6f}, "
        f"support={support}, resistance={resistance}, "
        f"ADX={adx:.1f}, RSI={rsi:.1f}, vol={volume_ratio:.2f}x, strength={strength:.2f}"
    )
    return (side, strength, route)
