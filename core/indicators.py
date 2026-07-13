import logging
import numpy as np

logger = logging.getLogger(__name__)


def check_candle_strength(ohlcv, condition_func, score_threshold=0.5):
    """用權重型「訊號強度」取代死板的「連續性」判斷。

    原本 core/signal_engine.py 的 get_consecutive_count() 要求連續 N 根 K 線嚴格同向
    （close[i] > close[i-1]），太死板：只要其中一根雜訊回抽，整個訊號就被打回 0。
    改成對最近兩根「已收盤」K 線分別套用 condition_func，取平均分數跟門檻比較——
    預設 score_threshold=0.5 時，2 根裡符合 1 根就算通過（分數 0.5），2 根都符合分數
    才是 1.0，用來額外加權。

    ohlcv: [[timestamp, open, high, low, close, volume], ...]，最新一筆在最後面。
    condition_func: 傳入單根 K 線（同樣是 [timestamp, open, high, low, close, volume]
                    格式），回傳布林值或數值分數。
    score_threshold: 平均分數門檻，預設 0.5。

    注意：ohlcv[-1] 通常是「當前尚未收盤」的那一根（交易所還在即時更新），只有
    ohlcv[-2]、ohlcv[-3] 才是「已經收盤確認」的資料，所以取樣範圍是 ohlcv[-3:-1]
    （不含 -1），不是天真地取最後兩筆。
    """
    target_candles = ohlcv[-3:-1]
    if not target_candles:
        return False

    scores = []
    for candle in target_candles:
        res = condition_func(candle)
        score_val = float(res) if isinstance(res, (bool, int, float)) else 0.0
        scores.append(score_val)

    avg_score = sum(scores) / len(scores)
    return avg_score >= score_threshold


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

    def get_ema_series(data, period):
        if len(data) < period:
            return np.zeros(len(data))
        multiplier = 2.0 / (period + 1)
        series = np.zeros(len(data))
        series[:period] = np.mean(data[:period])
        for i in range(period, len(data)):
            series[i] = (data[i] - series[i-1]) * multiplier + series[i-1]
        return series

    ema_fast_series = get_ema_series(prices, fast)
    ema_slow_series = get_ema_series(prices, slow)

    macd_line_series = ema_fast_series - ema_slow_series

    macd_line = macd_line_series[-1]
    prev_macd_line = macd_line_series[-2] if len(macd_line_series) >= 2 else macd_line

    # Signal line is an EMA of the MACD line
    macd_vals_for_signal = macd_line_series[-(signal*2):]
    signal_series = get_ema_series(macd_vals_for_signal, signal)

    macd_signal = signal_series[-1] if len(signal_series) > 0 else 0
    prev_macd_signal = signal_series[-2] if len(signal_series) >= 2 else macd_signal

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


def _calc_sl_tp(sym, side, s, p, route="a"):
    """計算 ATR、SL 距離、TP 距離、預期盈虧比。"""
    from core.symbol_profile import get_effective_exit_setting, get_dynamic_atr_multiplier
    from core.config import SL_ATR_MULTIPLIER, TP_ATR_MULTIPLIER, HARD_STOP_LOSS_PCT, EXIT_RR_MULTIPLIER
    atr_val = _get_atr(s, p)
    sl_raw = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), side == "buy")
    tp_mult = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), side == "buy")
    sl_mult = get_dynamic_atr_multiplier(sym, sl_raw)

    # Layer-S: 動態反手止損 (Dynamic Reverse SL)
    if route == "Automatic_Reverse":
        old_sl_mult = sl_mult
        sl_mult *= 1.25
        logger.info(f"@@COIN_DEBUG@@ 🛡️ {sym} 反手進場，擴大止損空間 (sl_mult: {old_sl_mult:.2f} -> {sl_mult:.2f})")

    # Layer-A: Low-Volatility Mode Switch
    _atr_hist_sl = s.get("atr_history", [])
    _atr_24h_avg_sl = float(np.mean(_atr_hist_sl)) if len(_atr_hist_sl) > 0 else 0.0
    _is_low_vol_mode = (_atr_24h_avg_sl > 0 and atr_val < _atr_24h_avg_sl * 0.8)

    if _is_low_vol_mode:
        sl_dist = p * 0.010
        tp_dist = p * 0.015
        logger.info(f"[LowVol_Mode] {sym} ATR low({atr_val:.5f} < avg{_atr_24h_avg_sl:.5f}x0.8), using fixed% SL=1.0% TP=1.5%")
    else:
        sl_dist = max(atr_val * sl_mult, p * 0.004)
        sl_dist += p * 0.0005  # 0.05% 執行滑點緩衝
        tp_dist = max(atr_val * tp_mult, p * 0.015)

    # Layer-B: Absolute Distance Floor
    _SL_FLOOR_PCT = 0.008
    _TP_FLOOR_PCT = 0.005
    sl_dist = max(sl_dist, p * _SL_FLOOR_PCT)
    tp_dist = max(tp_dist, p * _TP_FLOOR_PCT)

    hard_sl_pct = get_effective_exit_setting(
        sym,
        "hard_stop_loss_pct",
        s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT),
        side == "buy",
    )
    risk_dist = max(sl_dist, p * hard_sl_pct)

    # Layer-C: Forced R:R Floor
    min_tp_dist = risk_dist * EXIT_RR_MULTIPLIER
    if tp_dist < min_tp_dist:
        logger.info(f"⚠️ [R:R_Adjustment] {sym} 原本停利距離 {tp_dist:.4f} 太近 (< 風險×{EXIT_RR_MULTIPLIER})，已強制拉開至 {min_tp_dist:.4f} (保證 R:R >= {EXIT_RR_MULTIPLIER})")
        tp_dist = min_tp_dist

    expected_rr = tp_dist / risk_dist if risk_dist > 0 else 0
    return atr_val, sl_dist, tp_dist, expected_rr
