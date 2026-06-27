import numpy as np


def calculate_ema(prices, period):
    if len(prices) < period:
        return np.mean(prices)
    multiplier = 2.0 / (period + 1)
    ema = np.mean(prices[:period])
    for p in prices[period:]:
        ema = (p - ema) * multiplier + ema
    return ema


def calculate_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal:
        return 0, 0, 0, 0, 0
    ema_fast = np.array([calculate_ema(prices[:i+1], fast) for i in range(fast-1, len(prices))])
    ema_slow = np.array([calculate_ema(prices[:i+1], slow) for i in range(slow-1, len(prices))])
    macd_line = ema_fast[-1] - ema_slow[-1]
    prev_macd_line = ema_fast[-2] - ema_slow[-2] if len(ema_fast) >= 2 and len(ema_slow) >= 2 else macd_line
    macd_vals = ema_fast[-signal*2:] - ema_slow[-signal*2:]
    signal_vals = np.array([calculate_ema(macd_vals[:i+1], signal) for i in range(signal-1, len(macd_vals))])
    macd_signal = signal_vals[-1] if len(signal_vals) > 0 else 0
    prev_macd_signal = signal_vals[-2] if len(signal_vals) >= 2 else macd_signal
    macd_hist = macd_line - macd_signal
    return macd_line, macd_signal, macd_hist, prev_macd_line, prev_macd_signal


def calculate_bollinger_bands(prices, period=20, std_dev=2.0):
    if len(prices) < period:
        return 0, 0, 0
    sma = np.mean(prices[-period:])
    std = np.std(prices[-period:])
    return sma + std_dev * std, sma, sma - std_dev * std


def calculate_adx(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        return 0
    tr_list, plus_dm_list, minus_dm_list = [], [], []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0
        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
    if len(tr_list) < period:
        return 0
    atr = np.mean(tr_list[-period:])
    if atr < 1e-10:
        return 0
    plus_di = 100 * np.mean(plus_dm_list[-period:]) / atr
    minus_di = 100 * np.mean(minus_dm_list[-period:]) / atr
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 1e-10 else 0
    return dx


def get_dynamic_stagnation_limit(current_atr, atr_ma20):
    if current_atr < atr_ma20 * 0.5:
        return 180
    elif current_atr < atr_ma20:
        return 300
    return 480


def _get_atr(s, p):
    """安全取得 ATR 值；若為零則以價格 1% 代替。"""
    atr = s.get("current_atr", 0.0)
    return atr if atr > 0 else (p * 0.01)


def _macd_vals(s):
    """從 state 取出 macd_hist 與 prev_macd_hist。"""
    macd_hist = s.get("macd_line", 0.0) - s.get("macd_signal", 0.0)
    prev_macd_hist = s.get("prev_macd_line", 0.0) - s.get("prev_macd_signal", 0.0)
    return macd_hist, prev_macd_hist


def _calc_sl_tp(sym, side, s, p):
    """計算 ATR、SL 距離、TP 距離、預期盈虧比。"""
    from core.symbol_profile import get_effective_exit_setting, get_dynamic_atr_multiplier
    from core.config import SL_ATR_MULTIPLIER, TP_ATR_MULTIPLIER
    atr_val = _get_atr(s, p)
    sl_raw = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), side == "buy")
    tp_mult = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), side == "buy")
    sl_mult = get_dynamic_atr_multiplier(sym, sl_raw)
    sl_dist = max(atr_val * sl_mult, p * 0.004)
    tp_dist = max(atr_val * tp_mult, p * 0.015)

    # 強制 R:R 保底 (Forced R:R Floor)
    # 停利距離必須 >= 停損距離的 1.5 倍，防止「停損大於停利」
    MIN_RR_FLOOR = 1.5
    min_tp_dist = sl_dist * MIN_RR_FLOOR
    if tp_dist < min_tp_dist:
        print(f"⚠️ [R:R_Adjustment] {sym} 原本停利距離 {tp_dist:.4f} 太近 (< SL×{MIN_RR_FLOOR})，已強制拉開至 {min_tp_dist:.4f} (保證 R:R >= {MIN_RR_FLOOR})")
        tp_dist = min_tp_dist

    expected_rr = tp_dist / sl_dist if sl_dist > 0 else 0
    return atr_val, sl_dist, tp_dist, expected_rr
