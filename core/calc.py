"""Pure calculation functions — no ctx dependency, no side effects.
   AI: if you need to compute a price/level/score, put it here."""
from __future__ import annotations
import numpy as np


def stop_loss_price(avg_price: float, sl_mult: float, atr: float, is_long: bool, hard_sl_pct: float = 0.015) -> float:
    if atr <= 0 or avg_price <= 0:
        return avg_price * (1 - hard_sl_pct) if is_long else avg_price * (1 + hard_sl_pct)
    atr_sl = sl_mult * atr
    sl = avg_price - atr_sl if is_long else avg_price + atr_sl
    hard = avg_price * (1 - hard_sl_pct) if is_long else avg_price * (1 + hard_sl_pct)
    return sl if is_long == (sl > hard) else hard


def take_profit_price(avg_price: float, tp_mult: float, atr: float, is_long: bool) -> float:
    return avg_price + tp_mult * atr if is_long else avg_price - tp_mult * atr


def trailing_stop_price(current_price: float, highest: float, lowest: float,
                        activation_atr: float, distance_atr: float,
                        atr: float, is_long: bool, giveback_ratio: float = 0.2) -> float:
    if is_long:
        if current_price <= highest:
            return highest - (highest - current_price) * giveback_ratio
        return current_price - activation_atr * atr
    else:
        if current_price >= lowest:
            return lowest + (current_price - lowest) * giveback_ratio
        return current_price + activation_atr * atr


def breakeven_price(avg_price: float, atr: float, is_long: bool, buffer_pct: float = 0.002) -> float:
    threshold = atr * 0.8  # profit must exceed this before breakeven locks
    be = avg_price * (1 + buffer_pct) if is_long else avg_price * (1 - buffer_pct)
    if is_long:
        return be if (be - avg_price) > threshold else avg_price + threshold
    else:
        return be if (avg_price - be) > threshold else avg_price - threshold


def profit_pct(current_price: float, avg_price: float, is_long: bool) -> float:
    if avg_price <= 0:
        return 0.0
    return (current_price - avg_price) / avg_price if is_long else (avg_price - current_price) / avg_price


def rsi_from_closes(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-period - 1:])
    gains = deltas[deltas > 0].sum()
    losses = -deltas[deltas < 0].sum()
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def atr_from_ohlcv(ohlcv: list) -> float:
    if len(ohlcv) < 15:
        return 0.0
    tr_list = []
    for i in range(1, len(ohlcv)):
        h, l, pc = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    return float(np.mean(tr_list[-14:]))


def signal_strength(rsi: float, macd_hist: float, prev_macd_hist: float,
                    volume_ratio: float, adx: float, atr_ratio: float) -> float:
    score = 0.0
    if rsi > 70 or rsi < 30:
        score += 15
    if macd_hist > 0 and macd_hist > prev_macd_hist:
        score += 20
    elif macd_hist < 0 and macd_hist < prev_macd_hist:
        score += 20
    if volume_ratio > 1.5:
        score += 15 * min(volume_ratio / 3, 2)
    if adx > 25:
        score += 10
    return score
