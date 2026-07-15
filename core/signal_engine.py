import logging
import json

from core import ctx
from core.config import CONFIG_FILE

logger = logging.getLogger(__name__)



def compute_signal_strength(sym):
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

    long_stack = ma7 > ma25 > ma99 and ma7 > prev_ma7 and ma25 >= prev_ma25
    short_stack = ma7 < ma25 < ma99 and ma7 < prev_ma7 and ma25 <= prev_ma25
    # 交叉是趨勢的起點：此時 MA25 常尚未越過 MA99。交叉路線只要求價格位於
    # MA99 正確一側與兩條短中均線斜率同向；回調/突破仍要求完整三均線排列。
    cross_long = (golden_cross and above_ma99 and ma7 > prev_ma7 and ma25 >= prev_ma25
                  and candle_close > candle_open and volume_ratio >= 0.8)
    cross_short = (death_cross and below_ma99 and ma7 < prev_ma7 and ma25 <= prev_ma25
                   and candle_close < candle_open and volume_ratio >= 0.8)
    atr = float(s.get("current_atr", 0.0) or 0.0)
    touch_tolerance = max(0.0015, min(0.008, (atr / candle_close) * 0.5 if candle_close > 0 else 0.002))
    pullback_long = (long_spreading and long_stack and above_ma99 and candle_low <= ma25 * (1 + touch_tolerance)
                     and candle_close >= ma25 and candle_close > candle_open and volume_ratio >= 0.8)
    pullback_short = (short_spreading and short_stack and below_ma99 and candle_high >= ma25 * (1 - touch_tolerance)
                      and candle_close <= ma25 and candle_close < candle_open and volume_ratio >= 0.8)

    completed = candles[:-1]
    breakout_long = breakout_short = False
    if len(completed) >= 21:
        prior = completed[-21:-1]
        prior_high = max(float(c[2]) for c in prior)
        prior_low = min(float(c[3]) for c in prior)
        breakout_long = (long_spreading and long_stack and above_ma99 and candle_close > prior_high
                         and candle_close > candle_open and volume_ratio >= 1.2)
        breakout_short = (short_spreading and short_stack and below_ma99 and candle_close < prior_low
                          and candle_close < candle_open and volume_ratio >= 1.2)

    if cross_long or cross_short:
        side, route = ("buy" if cross_long else "sell"), "MA_Cross"
    elif breakout_long or breakout_short:
        side, route = ("buy" if breakout_long else "sell"), "MA_Breakout"
    elif pullback_long or pullback_short:
        side, route = ("buy" if pullback_long else "sell"), "MA25_Pullback"
    else:
        ma_gap_pct = abs(gap) / candle_close if candle_close > 0 else 0.0
        ma7_slope = abs(ma7 - prev_ma7) / candle_close if candle_close > 0 else 0.0
        ma25_slope = abs(ma25 - prev_ma25) / candle_close if candle_close > 0 else 0.0
        if volume_ratio < 0.65:
            reason = f"量能過低（{volume_ratio:.2f}×均量），暫停交易"
        elif ma_gap_pct < 0.001 and ma7_slope < 0.0005 and ma25_slope < 0.0005:
            reason = "MA7／MA25 平走交織，屬盤整假訊號區"
        elif ma7 > ma25 and not above_ma99:
            reason = "MA7 雖高於 MA25，但價格仍在 MA99 下方，禁止逆勢做多"
        elif ma7 < ma25 and not below_ma99:
            reason = "MA7 雖低於 MA25，但價格仍在 MA99 上方，禁止逆勢做空"
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
