import logging
import json

from core import ctx
from core.config import CONFIG_FILE

logger = logging.getLogger(__name__)



from core.config import ENTRY_SURGE_THRESHOLD, MA_CROSS_MIN_GAP_PCT

def compute_signal_strength(sym, realtime_trigger=False):
    """Generate entries exclusively from completed-candle MA7/25/99 setups."""
    s = ctx.STATES[sym]
    s["entry_block_reason"] = ""
    candles = s.get("ohlcv", [])
    ma7 = float(s.get("ma7", 0.0) or 0.0)
    ma25 = float(s.get("ma25", 0.0) or 0.0)
    ma99 = float(s.get("ma99", 0.0) or 0.0)
    prev_ma7 = float(s.get("prev_ma7", 0.0) or 0.0)
    prev_ma25 = float(s.get("prev_ma25", 0.0) or 0.0)
    vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)
    adx = float(s.get("adx", 0.0) or 0.0)
    prev_adx = float(s.get("prev_adx", 0.0) or 0.0)
    if len(candles) < 22 or min(ma7, ma25, ma99, prev_ma7, prev_ma25, vol_ma20) <= 0:
        s["entry_block_reason"] = "MA7／MA25／MA99 或成交量資料尚未完成"
        return (None, 0, None)

    signal_candle = candles[-2]
    signal_ts = int(signal_candle[0])
    candle_open, candle_high, candle_low, candle_close, candle_volume = map(float, signal_candle[1:6])
    volume_ratio = candle_volume / vol_ma20
    golden_cross = prev_ma7 <= prev_ma25 and ma7 > ma25
    death_cross = prev_ma7 >= prev_ma25 and ma7 < ma25
    gap, prev_gap = ma7 - ma25, prev_ma7 - prev_ma25
    long_spreading = ma7 > ma25 and ma7 > prev_ma7 and gap > max(prev_gap, 0.0)
    short_spreading = ma7 < ma25 and ma7 < prev_ma7 and gap < min(prev_gap, 0.0)
    above_ma99, below_ma99 = candle_close > ma99, candle_close < ma99

    current_rsi = float(s.get("current_rsi", 50.0))
    vol_surge = float(s.get("vol_surge", 0.0))
    personality = s.get("personality", "calm")
    atr_pct = float(s.get("atr_pct", 0.0))

    # 動態量能閾值 — 放寬至 0.6x，只需基本流動性確認
    base_limit = 0.6
    if atr_pct > 5.0:
        base_limit = 1.0  # 波動失控時稍微收緊
    breakout_limit = base_limit * 1.5

    long_stack = ma7 > ma25 and ma7 > prev_ma7 and ma25 >= prev_ma25
    short_stack = ma7 < ma25 and ma7 < prev_ma7 and ma25 <= prev_ma25

    # 即時量只用來確認當下價格方向；MA 策略的參與度門檻必須使用訊號 K 棒的已收線量。
    # 否則新 K 棒剛開始時 vol_surge 接近 0 會誤擋有效訊號，也可能被未收線瞬時量誤放行。
    is_realtime_strong = realtime_trigger and (vol_surge >= 1.5)

    # MA7／MA25 必須在交叉後拉開最小距離；只有斜率、但兩線仍幾乎重疊時，方向
    # 尚未真正成立。0.005% 可擋 DOGE 類極薄交叉，同時保留既有成功樣本的間距。
    ma_gap_pct = abs(gap) / candle_close if candle_close > 0 else 0.0
    ma7_slope = abs(ma7 - prev_ma7) / candle_close if candle_close > 0 else 0.0
    ma25_slope = abs(ma25 - prev_ma25) / candle_close if candle_close > 0 else 0.0
    is_flat_chop = ma_gap_pct < 0.001 and ma7_slope < 0.0005 and ma25_slope < 0.0005
    cross_direction_confirmed = ma_gap_pct >= MA_CROSS_MIN_GAP_PCT

    # 交叉路線：MA7 x MA25 金叉/死叉，只需確認 K 棒方向與非極端 RSI
    # ETH 成功樣本只有 0.52x RVOL：若交叉已站在 MA99 正確方向且波動未失控，
    # 允許 0.50x～0.60x 進入候選；錯誤 MA99 方向仍維持原本 0.60x 門檻。
    cross_long_volume_ok = volume_ratio >= base_limit or (
        volume_ratio >= 0.5 and above_ma99 and atr_pct <= 5.0
    )
    cross_short_volume_ok = volume_ratio >= base_limit or (
        volume_ratio >= 0.5 and below_ma99 and atr_pct <= 5.0
    )
    cross_long = (golden_cross and ma7 > prev_ma7 and ma25 >= prev_ma25
                  and (candle_close > candle_open or is_realtime_strong)
                  and cross_long_volume_ok and current_rsi < 70
                  and cross_direction_confirmed and not is_flat_chop)
    cross_short = (death_cross and ma7 < prev_ma7 and ma25 <= prev_ma25
                   and (candle_close < candle_open or is_realtime_strong)
                   and cross_short_volume_ok and current_rsi > 30
                   and cross_direction_confirmed and not is_flat_chop)

    atr = float(s.get("current_atr", 0.0) or 0.0)
    touch_tolerance = max(0.0015, min(0.008, (atr / candle_close) * 0.5 if candle_close > 0 else 0.002))

    # 回調路線：只需量能 + K 棒方向確認，不再過濾 RSI 方向動能
    pullback_long = (long_spreading and long_stack and candle_low <= ma25 * (1 + touch_tolerance)
                     and candle_close >= ma25 and (candle_close > candle_open or is_realtime_strong)
                     and volume_ratio >= base_limit and current_rsi < 70)
    pullback_short = (short_spreading and short_stack and candle_high >= ma25 * (1 - touch_tolerance)
                      and candle_close <= ma25 and (candle_close < candle_open or is_realtime_strong)
                      and volume_ratio >= base_limit and current_rsi > 30)

    from core.config import DISABLE_MA_BREAKOUT
    completed = candles[:-1]
    breakout_long = breakout_short = False
    if len(completed) >= 21 and not DISABLE_MA_BREAKOUT:
        prior = completed[-21:-1]
        prior_high = max(float(c[2]) for c in prior)
        prior_low = min(float(c[3]) for c in prior)
        # 突破路線：放寬量能門檻，不過濾 RSI 動能方向
        breakout_long = (long_spreading and long_stack and candle_close > prior_high
                         and (candle_close > candle_open or is_realtime_strong)
                         and volume_ratio >= breakout_limit and current_rsi < 70)
        breakout_short = (short_spreading and short_stack and candle_close < prior_low
                           and (candle_close < candle_open or is_realtime_strong)
                           and volume_ratio >= breakout_limit and current_rsi > 30)

    if cross_long or cross_short:
        side, route = ("buy" if cross_long else "sell"), "MA_Cross"
    elif (breakout_long or breakout_short) and not DISABLE_MA_BREAKOUT:
        side, route = ("buy" if breakout_long else "sell"), "MA_Breakout"
    elif pullback_long or pullback_short:
        side, route = ("buy" if pullback_long else "sell"), "MA25_Pullback"
    else:
        # 既有三條路線都沒觸發時，才嘗試簡化路線 (MA7_Simple)
        #
        # 實測（peak_giveback_stats.py + trade_history.json）：MA7_Simple 40 筆只有
        # 20% 勝率，是所有路線裡最差、單一路線就吃掉全部虧損過半。原因是它唯一
        # 沒有要求 MA25 中期趨勢配合方向（其他三條路線都要求 long/short_spreading
        # 或 long/short_stack），等於允許在 MA25 明顯走跌時，只因 MA7 這條最快的
        # 均線單根蠟燭翻頭向上就做多——這種逆著中期趨勢的早期轉折，本質上更容易
        # 只是雜訊，不是真反轉。這裡補上「MA25 不能是逆勢方向」的最低限度要求，
        # 量能門檻也拉齊到跟其他路線一樣的 0.6x（原本 0.5x 比全部路線都寬鬆，
        # 等於連平均以下的量都放行）。
        prev_ma7_2 = float(s.get("prev_ma7_2", 0.0) or 0.0)
        prev_slope = prev_ma7 - prev_ma7_2
        curr_slope = ma7 - prev_ma7
        turn_up = prev_slope <= 0 and curr_slope > 0
        turn_down = prev_slope >= 0 and curr_slope < 0
        bullish_candle = candle_close > candle_open
        bearish_candle = candle_close < candle_open
        volume_ok = volume_ratio >= base_limit
        ma25_not_against_long = ma25 >= prev_ma25
        ma25_not_against_short = ma25 <= prev_ma25
        # 實測 LINKUSDT 案例：ADX 在短短 15 秒內從 7.7 暴衝到 35.4，同一輪掃描
        # 就翻出 MA7_Simple 訊號，看起來像扎實趨勢，其實只是單根尖刺行情帶動，
        # 進場後浮盈只到 +0.41% 就反轉停損。正常累積出來的趨勢，ADX 不會在
        # 相鄰兩次掃描（約 10 秒）間跳這麼多，用這個過濾掉尖刺型態的假訊號。
        ADX_SPIKE_GUARD_PCT = 15.0
        adx_not_spiking = (adx - prev_adx) <= ADX_SPIKE_GUARD_PCT

        if (turn_up and bullish_candle and volume_ok and current_rsi < 75.0
                and ma25_not_against_long and adx_not_spiking
                and not (golden_cross or death_cross)):
            side, route = "buy", "MA7_Simple"
            reason = f"MA7 谷底轉折向上 | MA7={ma7:.6f} RVOL={volume_ratio:.2f}x RSI={current_rsi:.1f}"
            s["ma_signal_candle_ts"] = signal_ts
            logger.info(f"@@COIN_DEBUG@@ ✅ {sym} [MA7_Simple] buy | {reason}")
            # USUSDT 成功樣本的 RVOL 約 0.83x。保留門檻邊緣的最低觸發能力，
            # 但讓門檻附近的弱量轉折確實降分，避免與有量轉折同為 25 分。
            volume_adjustment = max(-2.0, min((volume_ratio - 0.8) * 5.0, 5.0))
            strength = 25.0 + volume_adjustment
            return (side, strength, route)
        elif (turn_down and bearish_candle and volume_ok and current_rsi > 25.0
                and ma25_not_against_short and adx_not_spiking
                and not (golden_cross or death_cross)):
            side, route = "sell", "MA7_Simple"
            reason = f"MA7 頭部轉折向下 | MA7={ma7:.6f} RVOL={volume_ratio:.2f}x RSI={current_rsi:.1f}"
            s["ma_signal_candle_ts"] = signal_ts
            logger.info(f"@@COIN_DEBUG@@ ✅ {sym} [MA7_Simple] sell | {reason}")
            # 空單採對稱評分：弱量仍可觀察，但排序必須低於有量轉折。
            volume_adjustment = max(-2.0, min((volume_ratio - 0.8) * 5.0, 5.0))
            strength = 25.0 + volume_adjustment
            return (side, strength, route)

        if volume_ratio < base_limit:
            reason = f"量能不足（RVOL={volume_ratio:.2f}x < {base_limit:.2f}x），暫停交易"
        elif (golden_cross or death_cross) and not cross_direction_confirmed:
            reason = (f"MA7／MA25 交叉間距僅 {ma_gap_pct*100:.4f}% < "
                      f"{MA_CROSS_MIN_GAP_PCT*100:.4f}%，方向確認不足")
        elif is_flat_chop and (golden_cross or death_cross):
            reason = "MA7／MA25 平走交織，屬盤整假訊號區"
        elif current_rsi >= 70 and ma7 > ma25:
            reason = f"RSI={current_rsi:.1f} 已達極端值，防超買反轉不追多"
        elif current_rsi <= 30 and ma7 < ma25:
            reason = f"RSI={current_rsi:.1f} 已達極端值，防超賣反轉不追空"
        else:
            reason = "等待 MA7／MA25 收線交叉、MA25 回調或帶量突破"
        s["entry_block_reason"] = reason
        logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [MA_Strategy] {reason}")
        return (None, 0, None)

    strength = 25.0 + min(max(volume_ratio - 0.8, 0.0) * 5.0, 5.0)
    if route == "MA_Breakout":
        strength += 2.0
    s["ma_signal_candle_ts"] = signal_ts
    logger.info(f"@@COIN_DEBUG@@ ✅ {sym} [{route}] {side} | close={candle_close:.6f}, MA7={ma7:.6f}, MA25={ma25:.6f}, MA99={ma99:.6f}, volume={volume_ratio:.2f}x")
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
        RANGE_MIN_NET_PROFIT_PCT, TAKER_FEE_RATE,
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
    vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)

    if atr <= 0 or vol_ma20 <= 0:
        s["entry_block_reason"] = (
            f"ATR 或成交量資料不足（區間模式：ATR={atr:.8g}, VolMA20={vol_ma20:.2f}）"
        )
        return (None, 0, None)

    # 1. ADX 確認區間行情
    if adx >= RANGE_ADX_THRESHOLD:
        s["entry_block_reason"] = f"ADX={adx:.1f} ≥ {RANGE_ADX_THRESHOLD}，趨勢明顯，不開區間倉"
        logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [Range] ADX={adx:.1f} 過高，略過區間模式")
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
    min_range_width_pct = TAKER_FEE_RATE * 2 + RANGE_MIN_NET_PROFIT_PCT
    support, resistance = _find_horizontal_zones(
        completed, atr, RANGE_LOOKBACK, RANGE_TOUCH_COUNT, RANGE_TOUCH_ATR_TOLERANCE,
        current_price=signal_close, min_width_pct=min_range_width_pct,
    )

    if support is None or resistance is None or support >= resistance:
        s["entry_block_reason"] = "需要同時找到現價下方支撐與上方壓力帶"
        return (None, 0, None)

    # 3. 空間保護：區間寬度必須能覆蓋手續費 + 最低獲利空間
    round_trip_fee = TAKER_FEE_RATE * 2
    min_range_width_pct = round_trip_fee + RANGE_MIN_NET_PROFIT_PCT
    if support is not None and resistance is not None:
        range_width_pct = (resistance - support) / support if support > 0 else 0.0
        if range_width_pct < min_range_width_pct:
            s["entry_block_reason"] = (
                f"區間寬度 {range_width_pct*100:.2f}% < 最低需求 {min_range_width_pct*100:.2f}%（手續費+獲利空間）"
            )
            logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [Range] 區間過窄，略過")
            return (None, 0, None)

    # 4. 訊號 K 棒（已收盤倒數第二根）
    sig = candles[-2]
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
    bullish_rejection = candle_close > candle_open or lower_wick >= body * 1.2
    bearish_rejection = candle_close < candle_open or upper_wick >= body * 1.2

    # 5. 做多條件：低點碰支撐帶 且 收盤回彈至支撐上方 且 RSI < 55
    long_signal = (
        support is not None
        and candle_low <= support + touch_band      # 低點觸碰支撐帶
        and candle_close >= support                  # 收盤回彈至支撐上方
        and bullish_rejection                        # 收陽或足夠長下影確認拒跌
        and rsi < 55.0                               # 排除超買後的假支撐
    )

    # 6. 做空條件：高點碰壓力帶 且 收盤回落至壓力下方 且 RSI > 45
    short_signal = (
        resistance is not None
        and candle_high >= resistance - touch_band   # 高點觸碰壓力帶
        and candle_close <= resistance               # 收盤回落至壓力下方
        and bearish_rejection                        # 收陰或足夠長上影確認拒漲
        and rsi > 45.0                               # 排除超賣後的假壓力
    )

    if not long_signal and not short_signal:
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
