import logging
import asyncio
import time
import numpy as np

from core import ctx
from core.config import (PAPER_TRADING, HARD_STOP_LOSS_PCT, MIN_PROFIT_LOCK_THRESHOLD,
    PROTECTED_PROFIT_FLOOR, TREND_PERSISTENCE_WINDOW, PRICE_MOVEMENT_THRESHOLD,
    COIN_PROFILE_CONFIG, DEFAULT_REVERSAL_SETTINGS, SYMBOL_REVERSAL_SETTINGS,
    SL_ATR_MULTIPLIER, TP_ATR_MULTIPLIER)
from core.indicators import _get_atr, _macd_vals, calculate_ema, calculate_macd
from core.symbol_profile import get_effective_exit_setting, has_strong_momentum, get_dynamic_atr_multiplier
from core.calc import profit_pct as _profit_pct

logger = logging.getLogger(__name__)


def update_trailing_stop(sym, current_price, is_long):
    """
    實作非對稱移動停損 (Asymmetric Trailing Stop)
    當價格創新高/新低時，上移停損點，且加入保本緩衝區防止被雜訊洗出場。
    """
    s = ctx.STATES[sym]
    atr_val = s.get("current_atr", 0.0)
    if atr_val <= 0:
        return False, s["trailing_stop_price"]

    atr_history = s.get("atr_history", [])
    atr_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else atr_val
    safe_atr = min(atr_val, atr_avg * 3) if atr_avg > 0 else atr_val

    trailing_activation_atr = s.get("trailing_activation_atr", 0.0)
    trailing_distance_atr = s.get("trailing_distance_atr", s.get("trailing_stop_multiplier", 2.0))
    profit_lock_atr = s.get("profit_lock_atr", 0.0)

    avg_price = s["avg_price"]
    leverage = s.get("leverage", 8)
    mm_ratio = 0.004
    if is_long:
        liq_price = avg_price * (1 - 1.0 / leverage) / (1 - mm_ratio) if leverage > 0 else 0.0
    else:
        liq_price = avg_price * (1 + 1.0 / leverage) / (1 + mm_ratio) if leverage > 0 else 0.0

    profit_pct = _profit_pct(current_price, avg_price, is_long)
    s["highest_profit_pct"] = max(s.get("highest_profit_pct", 0.0), profit_pct)

    profit_atr_multiple = (current_price - avg_price) / atr_val if is_long else (avg_price - current_price) / atr_val

    if is_long:
        if current_price > s.get("trailing_highest", 0.0):
            s["trailing_highest"] = current_price

        trail_sl = s["trailing_stop_price"]

        if profit_lock_atr > 0 and profit_atr_multiple >= profit_lock_atr:
            locked_sl = avg_price * 1.001
            trail_sl = max(trail_sl, locked_sl)
        elif trailing_activation_atr > 0 and profit_atr_multiple >= trailing_activation_atr:
            dynamic_sl = s["trailing_highest"] - (atr_val * trailing_distance_atr)
            trail_sl = max(trail_sl, dynamic_sl)
        elif trailing_activation_atr == 0:
            # 獲利區間動態縮緊 Trailing Stop (Profit-Tier Dynamic Tightening)
            # 獲利越高，追蹤網越緊；獲利初期給對價格呼吸空間
            _hp_f = s["highest_profit_pct"]
            personality = s.get("personality", "balanced")
            profile_type = s.get("profile_type", "")
            if personality == "aggressive" or "High_Beta" in profile_type:
                if _hp_f > 0.05:    trailing_multiplier = 0.4
                elif _hp_f > 0.03:  trailing_multiplier = 0.6
                elif _hp_f > 0.02:  trailing_multiplier = 0.8
                else:               trailing_multiplier = 1.3
            else:
                if _hp_f > 0.05:    trailing_multiplier = 0.3
                elif _hp_f > 0.03:  trailing_multiplier = 0.45
                elif _hp_f > 0.02:  trailing_multiplier = 0.6
                else:               trailing_multiplier = 1.0
            # 最小距離防護：確保至少 0.25% 緩衝
            _min_gap_l = max(atr_val * trailing_multiplier, s["trailing_highest"] * 0.0025)
            dynamic_sl = s["trailing_highest"] - _min_gap_l

            trigger_mult = s.get("breakeven_trigger", s.get("sl_atr_multiplier", 1.5))
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = avg_price + sl_dist_atr
            if current_price >= breakeven_trigger:
                dynamic_sl = max(dynamic_sl, avg_price)

            trail_sl = max(trail_sl, dynamic_sl)

        safe_min_sl = liq_price * 1.2
        new_sl = max(trail_sl, safe_min_sl)

        if new_sl > s["trailing_stop_price"]:
            s["trailing_stop_price"] = new_sl
            logger.info(f"🛡️ [Trailing_SL] {sym} 移動止損上移至 {new_sl:.4f} (獲利倍數: {profit_atr_multiple:.1f}x ATR)")

    else:
        if current_price < s.get("trailing_lowest", float('inf')):
            s["trailing_lowest"] = current_price

        trail_sl = s["trailing_stop_price"]
        if trail_sl == 0.0:
            trail_sl = float('inf')

        if profit_lock_atr > 0 and profit_atr_multiple >= profit_lock_atr:
            locked_sl = avg_price * 0.999
            trail_sl = min(trail_sl, locked_sl)
        elif trailing_activation_atr > 0 and profit_atr_multiple >= trailing_activation_atr:
            dynamic_sl = s["trailing_lowest"] + (atr_val * trailing_distance_atr)
            trail_sl = min(trail_sl, dynamic_sl)
        elif trailing_activation_atr == 0:
            # 獲利區間動態縮緊 Trailing Stop (空單)
            _hp_fs = s["highest_profit_pct"]
            personality = s.get("personality", "balanced")
            profile_type = s.get("profile_type", "")
            if personality == "aggressive" or "High_Beta" in profile_type:
                if _hp_fs > 0.05:   trailing_multiplier = 0.4
                elif _hp_fs > 0.03: trailing_multiplier = 0.6
                elif _hp_fs > 0.02: trailing_multiplier = 0.8
                else:               trailing_multiplier = 1.3
            else:
                if _hp_fs > 0.05:   trailing_multiplier = 0.3
                elif _hp_fs > 0.03: trailing_multiplier = 0.45
                elif _hp_fs > 0.02: trailing_multiplier = 0.6
                else:               trailing_multiplier = 1.0
            # 最小距離防護
            _min_gap_s = max(atr_val * trailing_multiplier, s["trailing_lowest"] * 0.0025)
            dynamic_sl = s["trailing_lowest"] + _min_gap_s

            trigger_mult = s.get("breakeven_trigger", s.get("sl_atr_multiplier", 1.5))
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = avg_price - sl_dist_atr
            if current_price <= breakeven_trigger:
                dynamic_sl = min(dynamic_sl, avg_price)

            trail_sl = min(trail_sl, dynamic_sl)

        safe_max_sl = liq_price * 0.8
        new_sl = min(trail_sl, safe_max_sl)

        if s["trailing_stop_price"] == 0.0 or new_sl < s["trailing_stop_price"]:
            s["trailing_stop_price"] = new_sl
            logger.info(f"🛡️ [Trailing_SL] {sym} 移動止損下移至 {new_sl:.4f} (獲利倍數: {profit_atr_multiple:.1f}x ATR)")

    return False, s["trailing_stop_price"]


def detect_market_regime(sym, current_price, avg_price, is_long):
    s = ctx.STATES[sym]
    if len(s["ohlcv"]) < 20 or avg_price <= 0:
        return "HOLD", "資料不足"

    recent_candles = s["ohlcv"][-20:]
    highs = np.array([x[2] for x in recent_candles])
    lows = np.array([x[3] for x in recent_candles])
    closes = np.array([x[4] for x in recent_candles])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / recent_low if recent_low > 0 else 0

    atr_val = _get_atr(s, current_price)
    atr_pct = atr_val / current_price if current_price > 0 else 0

    reversal_settings = DEFAULT_REVERSAL_SETTINGS.copy()
    reversal_settings.update(SYMBOL_REVERSAL_SETTINGS.get(sym, {}))
    trade_signal = s.get("trade_signal_strength", 0.0)
    reversal_threshold = reversal_settings["trade_signal_threshold"]
    prev_close = s.get("prev_close")
    if trade_signal >= reversal_threshold and prev_close:
        price_move_pct = (current_price - prev_close) / max(prev_close, 1e-8)
        if (is_long and price_move_pct < -max(reversal_settings["min_reverse_pct"], atr_pct * 1.2)) or \
           (not is_long and price_move_pct > max(reversal_settings["min_reverse_pct"], atr_pct * 1.2)):
            return "BREAKOUT_REVERSAL", f"即時大額成交異常 {s['trade_signal_reason']}"

    volume_surge = s["current_vol"] > s["vol_ma20"] * reversal_settings["volume_multiplier"]
    if prev_close:
        price_jump = (prev_close - current_price) / max(prev_close, 1e-8) > max(reversal_settings["price_jump_pct"], atr_pct * 1.2) if is_long else \
                     (current_price - prev_close) / max(prev_close, 1e-8) > max(reversal_settings["price_jump_pct"], atr_pct * 1.2)
    else:
        price_jump = False
    if volume_surge and price_jump:
        return "BREAKOUT_REVERSAL", "放量突發且價格急速變動"

    is_ranging = range_width_pct < 0.025 and atr_pct < 0.015
    if is_ranging:
        profit_pct = _profit_pct(current_price, avg_price, is_long)
        if profit_pct >= 0.010:
            return "RANGE_PROFIT_TAKE", f"盤整區間內已獲利 {profit_pct * 100:.2f}%"

    return "HOLD", "未達出場條件"


def check_trend_persistence(sym):
    s = ctx.STATES[sym]
    if not s.get("ohlcv") or len(s["ohlcv"]) < 2:
        return True
    return True


async def check_exits(sym):
    from core.orders import close_position, execute_order
    s = ctx.STATES[sym]
    if s.get("adjusted_this_tick", False):
        return
    if abs(s["qty"]) < 0.000001 or s["avg_price"] <= 0:
        return

    if s.get("current_atr", 0.0) <= 0:
        return
    hold_sec = time.time() - s["open_time"] if s["open_time"] > 0 else 0
    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)
    cooldown_limit = 20.0 if (current_atr > atr_24h_avg and atr_24h_avg > 0) else 60.0
    if hold_sec < cooldown_limit:
        current_vol = s.get("current_vol", 0.0)
        vol_ma20 = s.get("vol_ma20", 1.0)
        vol_ratio = current_vol / vol_ma20 if vol_ma20 > 0 else 1.0

        if vol_ratio > 2.5:
            logger.info(f"⚠️ [防插針豁免] {sym} 瞬時爆發量 (Ratio: {vol_ratio:.2f}x)，視為真崩盤，取消盲區保護！")
        else:
            return

    p = s["close_price"]
    avg = s["avg_price"]
    is_long = s["qty"] > 0
    profit_pct = (p - avg) / avg if is_long else (avg - p) / avg

    # ══ 峰值更新（最優先，必須在所有出場機制之前執行）══
    # 含 K 線盤中尖峰（HIGH/LOW），讓 1 秒內的暴漲/暴跌也能被保本/PeakLock 捕捉
    # ⚠️ 舊版本此更新在 update_trailing_stop(line~642) 才跑，保本/PeakLock 全讀舊值
    _ohlcv_early = s.get("ohlcv", [])
    _intra_peak_early = 0.0
    if _ohlcv_early and avg > 0:
        _lc = _ohlcv_early[-1]
        _intra_peak_early = (_lc[2] - avg) / avg if is_long else (avg - _lc[3]) / avg
    s["highest_profit_pct"] = max(
        s.get("highest_profit_pct", 0.0),
        profit_pct,
        max(0.0, _intra_peak_early)
    )

    _entry_atr = s.get("entry_atr", s.get("current_atr", avg * 0.003))
    _sl_mult   = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long)
    _rr_thresh = get_effective_exit_setting(sym, "rr_threshold", 1.3, is_long)
    _hard_sl   = get_effective_exit_setting(sym, "hard_stop_loss_pct", s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT), is_long)
    _atr_sl_pct = (_sl_mult * _entry_atr / avg) if avg > 0 else 0.006
    expected_loss_pct = max(_hard_sl, _atr_sl_pct, 0.005)
    min_tp_pct = expected_loss_pct * _rr_thresh

    bb_upper = s.get('bb_up', 0)
    bb_lower = s.get('bb_low', 0)
    vol_ma20 = s.get('vol_ma20', 0)
    current_vol = s.get('current_vol', 0)

    if not s.get("debug_start_time"):
        s["debug_start_time"] = time.time()

    if time.time() - s["debug_start_time"] < 600:
        if time.time() - s.get('last_debug_pressure_time', 0) > 60:
            logger.info(f"🔍 [DEBUG_PRESSURE] {sym}: Upper={bb_upper:.4f}, Lower={bb_lower:.4f}, Vol_MA={vol_ma20:.2f}")
            s['last_debug_pressure_time'] = time.time()

    is_breakout_up = (not is_long and bb_upper > 0 and p > bb_upper and current_vol > (vol_ma20 * 1.5))
    is_breakout_down = (is_long and bb_lower > 0 and p < bb_lower and current_vol > (vol_ma20 * 1.5))

    if is_breakout_up or is_breakout_down:
        last_reverse = s.get('last_reverse_time', 0)
        hold_sec = time.time() - s.get("open_time", time.time())
        if (time.time() - last_reverse > 1800 and hold_sec > 300
                and not s.get("pending_reverse_trigger")):
            new_direction = "buy" if is_breakout_up else "sell"
            s["pending_reverse_trigger"] = {
                "side": new_direction,
                "time": s["ohlcv"][-1][0] if s["ohlcv"] else 0,
                "strength": 18.0,
                "source": "BB_Breakout",
            }
            logger.info(f"⚠️ [REVERSE_PENDING] {sym} BB 突破偵測 → 等待下一根 K 收盤確認再反手 ({new_direction})")

    atr_val = _get_atr(s, p)
    profit_atr_mult = (p - avg) / atr_val if is_long else (avg - p) / atr_val

    if profit_atr_mult > 6.0:
        macd_hist = s.get("macd_hist", 0.0)
        prev_macd_hist = s.get("prev_macd_hist", 0.0)
        rsi = s.get("current_rsi", 50.0)
        prev_rsi = s.get("prev_rsi", rsi)

        momentum_failing = False
        if is_long:
            if macd_hist < prev_macd_hist or rsi <= prev_rsi:
                momentum_failing = True
        else:
            if macd_hist > prev_macd_hist or rsi >= prev_rsi:
                momentum_failing = True

        if momentum_failing:
            logger.info(f"✅ [Momentum_Exit] {sym} 獲利達標 (6.0 ATR) 且動能衰竭，早期獲利平倉！")
            cs = "sell" if is_long else "buy"
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Momentum_Exit]")
            return

    _hard_sl = COIN_PROFILE_CONFIG.get(sym, {}).get("hard_sl_pct", 0.0)
    if _hard_sl > 0 and profit_pct <= -_hard_sl:
        cs = 'sell' if is_long else 'buy'
        logger.info(f"🚨 [Hard_SL] {sym} 虧損達 {profit_pct*100:.2f}% (限制 {_hard_sl*100:.1f}%)，強制硬止損出場！")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Hard_SL]", is_stop_loss=True)
        if abs(profit_pct) > 0.015 and _check_reversal_allowed(sym, s):
            if s.get("consecutive_losses", 0) >= 2:
                s["reversal_ban_until"] = time.time() + 14400
            last_reverse = s.get("last_reverse_time", 0)
            if time.time() - last_reverse > 1800:
                s["pending_reverse"] = "buy" if not is_long else "sell"
                s["pending_reverse_time"] = time.time()
                s["last_reverse_time"] = time.time()
                logger.info(f"🔄 [Hard_SL_Reverse] {sym} 硬止損後設置反手信號 → {s['pending_reverse']}")
        return

    # --- 進場後觀察期快速撤退 (Post-Entry Observation Exit) ---
    # 首次進場後 1-5 分鐘：檢測三種「開錯方向」情境
    # 1. 虧損 > 0.2% 且 MACD 或 EMA20 任一確認反方向
    # 2. 進場後 2 分鐘價格完全沒往預期方向走（停滯）
    # 3. 進場後 3 分鐘內虧損 > 0.5%（強烈反轉）不等確認直接撤
    # 目的：不等 SL 觸發，主動剪掉「沒力氣的訊號」
    if s.get("entry_count", 0) == 1 and abs(s.get("qty", 0.0)) > 0.000001:
        _obs_time = time.time() - s.get("open_time", time.time())
        _entry_price = s.get("first_entry_price", avg)
        _wrong_dir = False
        _reason = ""

        # 根據幣種屬性動態調整觀察期閾值，防止高波動幣種被正常噪聲秒割
        _profile_type = s.get("profile_type", COIN_PROFILE_CONFIG.get(sym, {}).get("profile_type", ""))
        is_volatile_coin = _profile_type in ["Speculative_Risk", "High_Beta_Momentum"]
        _strong_rev_limit = -0.010 if is_volatile_coin else -0.005
        _vol_reversal_limit = -0.0050 if is_volatile_coin else -0.0025

        # 快速強烈反轉檢查（3 分鐘內虧 > 0.5% 或投機幣 > 1.0%，價格直接往反方向噴）
        if _obs_time < 180 and profit_pct < _strong_rev_limit:
            _wrong_dir = True
            _reason = f"快速強烈反轉 ({_obs_time:.0f}s 虧 {profit_pct*100:.2f}%)"

        # [NEW] 爆量反噬秒砍機制 (Instant Cut on Momentum Shift)
        # 如果進場後 5 分鐘內，遭遇爆量反向實體 K 線吞噬，直接在指定限額（普通 -0.25%/投機 -0.5%）停損，不硬扛
        if not _wrong_dir and _obs_time < 300 and profit_pct <= _vol_reversal_limit:
            _vol_now = s.get("current_vol", 0.0)
            _vol_ma = s.get("vol_ma20", 1e-8)
            if _vol_now > _vol_ma * 1.5:  # 爆量 1.5 倍
                if len(s.get("ohlcv", [])) >= 2:
                    _c_now = s["ohlcv"][-1]
                    # 判斷是否為反向大實體 K 線 (跌幅/漲幅 > 0.2%)
                    if is_long and _c_now[4] < _c_now[1] and (_c_now[1] - _c_now[4]) / _c_now[1] > 0.002:
                        _wrong_dir = True
                        _reason = f"爆量反噬秒砍 (量:{_vol_now/_vol_ma:.1f}x, 虧:{profit_pct*100:.2f}%)"
                    elif not is_long and _c_now[4] > _c_now[1] and (_c_now[4] - _c_now[1]) / _c_now[1] > 0.002:
                        _wrong_dir = True
                        _reason = f"爆量反噬秒砍 (量:{_vol_now/_vol_ma:.1f}x, 虧:{profit_pct*100:.2f}%)"

        # 峰值反轉：曾觸及有意義的利潤(0.4-1.0%)後反轉跌回虧損才撤
        # 峰值反轉（ATR 相對門檻）：峰值須達 1.2 倍 ATR（最少 0.5%，最多 1.5%）才算有意義反轉
        # ORDI ATR~1% → 門檻 1.2%；NEAR ATR~0.4% → 門檻 0.5%（固定地板）
        _peak_now = s.get("highest_profit_pct", 0.0)
        _entry_atr = s.get("entry_atr", 0.0)
        _atr_pct = (_entry_atr / avg) if (avg > 0 and _entry_atr > 0) else 0.005
        _min_peak_t2 = min(max(0.005, _atr_pct * 1.2), 0.015)
        if (not _wrong_dir and
                _obs_time < 600 and
                _min_peak_t2 <= _peak_now < _min_peak_t2 * 2.5 and
                profit_pct < -0.002):
            _wrong_dir = True
            _reason = f"峰值反轉 (峰: {_peak_now*100:.2f}%≥{_min_peak_t2*100:.1f}%ATR門 → 現: {profit_pct*100:.2f}%)"

        # 方向錯誤 + 動能確認檢查（5-15 分鐘，虧 > 0.5%，MACD AND EMA20 同時確認）
        # 給足 5 分鐘讓市場噪音平息，0.5% 才算真正逆向
        if not _wrong_dir and 300 < _obs_time < 900 and profit_pct < -0.005:
            _macd_obs = s.get("macd_line", 0.0) - s.get("macd_signal", 0.0)
            _prev_macd_obs = s.get("prev_macd_line", 0.0) - s.get("prev_macd_signal", 0.0)
            _ema20_obs = s.get("ema20", 0.0)
            _macd_bearish = (_macd_obs < 0 and _macd_obs < _prev_macd_obs) if is_long else (_macd_obs > 0 and _macd_obs > _prev_macd_obs)
            _ema20_wrong = (_ema20_obs > 0 and p < _ema20_obs) if is_long else (_ema20_obs > 0 and p > _ema20_obs)
            if _macd_bearish and _ema20_wrong:  # 需雙重確認（原本 OR → 改 AND）
                _wrong_dir = True
                _reason = f"方向錯誤+雙重確認 (MACD:{_macd_bearish} EMA20:{_ema20_wrong})"

        # 價格停滯檢查：移除（0~-0.1% 是震盪市場正常雜訊，2 分鐘停滯就砍導致大量手續費損耗）
        _stagnation = False

        if _wrong_dir:
            logger.info(f"🚨 [Post_Entry_Early_Exit] {sym} {_reason}，快速撤退！")
            cs = "sell" if is_long else "buy"
            s["wrong_dir_time"] = time.time()
            s["wrong_dir_side"] = s.get("last_entry_direction", cs)
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Post_Entry_Early_Exit]", is_stop_loss=True)

            # 快速強烈反轉 / 方向錯誤有動能確認 → 價格明顯往反方向走，順勢反手
            # 價格停滯不算（沒方向訊號）
            if not _stagnation and _check_reversal_allowed(sym, s):
                rev_side = "buy" if not is_long else "sell"
                logger.info(f"🔄 [Early_Exit_Reverse] {sym} 方向錯誤確認，順勢反手 {rev_side}")
                if s.get("consecutive_losses", 0) >= 2:
                    s["reversal_ban_until"] = time.time() + 14400
                s["pending_reverse"] = rev_side
                s["pending_reverse_time"] = time.time()
                s["last_reverse_time"] = time.time()
            return

    loss_limit = get_effective_exit_setting(sym, "risk_threshold_pct", 0.0025, is_long)
    _disable_dca = COIN_PROFILE_CONFIG.get(sym, {}).get("disable_rescue_dca", False)
    if not _disable_dca and profit_pct <= -loss_limit and s.get("entry_count", 0) == 1:
        logger.info(f"⚠️ [Rescue_DCA_Triggered] {sym} 虧損突破 {loss_limit*100:.4f}%，啟動緊急救援加碼！")
        cs = "buy" if is_long else "sell"
        await execute_order(sym, cs, p, allocation_pct=0.33, is_rescue_dca=True)
        return
    elif _disable_dca and profit_pct <= -loss_limit and s.get("entry_count", 0) == 1:
        logger.info(f"ℹ️ [DCA_Disabled] {sym} 虧損 {profit_pct*100:.2f}% 但此幣種已停用 Rescue DCA，等待 ATR-SL 出場")

    if s.get("entry_count", 0) > 0:
        time_since_last_entry = time.time() - s.get("last_entry_time", 0.0)

        # 第二道防線：動態逾時 = 基礎逾時 × (當前ATR ÷ 平均ATR)，波動大時給更多空間
        base_timeout_min = get_effective_exit_setting(sym, "rescue_timeout_min", 10, is_long)
        _atr_hist_r = s.get("atr_history", [])
        _atr_ma20_r = float(np.mean(_atr_hist_r)) if len(_atr_hist_r) > 0 else atr_val
        if _atr_ma20_r > 0:
            dynamic_timeout = base_timeout_min * (atr_val / _atr_ma20_r)
        else:
            dynamic_timeout = base_timeout_min
        dynamic_timeout = max(5.0, min(dynamic_timeout, 20.0))

        if time_since_last_entry > dynamic_timeout * 60:
            logger.info(f"⚠️ [RESCUE_TIMEOUT] {sym} 救援動態逾時 {dynamic_timeout:.1f}min (Base:{base_timeout_min}min)，強制平倉！")
            cs = "sell" if is_long else "buy"
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Rescue_Timeout]", is_stop_loss=True)
            return

        # 第三道防線：救援期間動能衰減提早止損
        # 超過3分鐘且仍虧損，且 MACD 反向擴張或價格破 EMA20 → 立即出場，不等逾時
        if time_since_last_entry > 180 and profit_pct < -0.001:
            macd_hist_r = s.get("macd_line", 0.0) - s.get("macd_signal", 0.0)
            prev_macd_hist_r = s.get("prev_macd_line", 0.0) - s.get("prev_macd_signal", 0.0)
            ema20_r = s.get("ema20", 0.0)
            is_momentum_dead = False
            if is_long:
                if (macd_hist_r < 0 and macd_hist_r < prev_macd_hist_r) or (ema20_r > 0 and p < ema20_r):
                    is_momentum_dead = True
            else:
                if (macd_hist_r > 0 and macd_hist_r > prev_macd_hist_r) or (ema20_r > 0 and p > ema20_r):
                    is_momentum_dead = True
            if is_momentum_dead:
                logger.info(f"🚨 [RESCUE_MOMENTUM_DECAY] {sym} 救援期間動能已死(MACD反向或破EMA20)，提早止損！(套牢:{profit_pct*100:.2f}%)")
                cs = "sell" if is_long else "buy"
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Rescue_Momentum_Decay]", is_stop_loss=True)
                return

        rescue_floor = get_effective_exit_setting(sym, "rescue_tp_floor_pct", 0.005, is_long)
        rescue_trail_atr = get_effective_exit_setting(sym, "rescue_trailing_atr", 1.5, is_long)

        if profit_pct >= rescue_floor:
            if is_long:
                s["rescue_highest"] = max(s.get("rescue_highest", 0.0), p)
                trail_sl = s["rescue_highest"] - (atr_val * rescue_trail_atr)
                if p <= trail_sl:
                    logger.info(f"✅ [RESCUE_TRAIL] {sym} 救援模式動態追蹤觸發！(獲利 {profit_pct*100:.2f}%)，獲利入袋！")
                    await close_position(sym, "sell", abs(s["qty"]), p, avg, reason="[Rescue_Trailing_Stop]")
                    return
            else:
                s["rescue_lowest"] = min(s.get("rescue_lowest", float('inf')), p) if s.get("rescue_lowest", 0) > 0 else p
                trail_sl = s["rescue_lowest"] + (atr_val * rescue_trail_atr)
                if p >= trail_sl:
                    logger.info(f"✅ [RESCUE_TRAIL] {sym} 救援模式動態追蹤觸發！(獲利 {profit_pct*100:.2f}%)，獲利入袋！")
                    await close_position(sym, "buy", abs(s["qty"]), p, avg, reason="[Rescue_Trailing_Stop]")
                    return

            if time.time() - s.get("last_rescue_log_time", 0) > 60:
                logger.info(f"👀 [RESCUE_RUNNER] {sym} 救援模式啟動追蹤！目前獲利 {profit_pct*100:.2f}% (目標底線: {rescue_floor*100:.2f}%)")
                s["last_rescue_log_time"] = time.time()
            return

    sl_base_raw = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long)

    sl_base = get_dynamic_atr_multiplier(sym, sl_base_raw)

    atr_val = _get_atr(s, p)
    atr_ma20 = s.get("atr_ma20", atr_val)
    is_low_vol = (atr_ma20 > 0 and atr_val < atr_ma20)
    if is_low_vol:
        sl_mult = min(sl_base, 2.0)
    else:
        sl_mult = sl_base

    btc_4h = ctx.MARKET_WIND.get("btc_trend_4h", "NEUTRAL")
    _is_counter_trend = (is_long and btc_4h == "BEAR") or (not is_long and btc_4h == "BULL")
    _sl_floor_pct = 0.004
    if _is_counter_trend:
        sl_mult *= 0.7
        _sl_floor_pct = 0.0025

    tp_base = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), is_long)
    if is_low_vol:
        tp_base = min(tp_base, 5.0)

    sl_dist = max(sl_mult * atr_val, avg * _sl_floor_pct)
    tp_dist = max(tp_base * atr_val, avg * 0.012)

    breakeven_threshold = 0.0035  # 所有單：0.35% 即啟動保本

    fee_buffer = 0.001  # 0.1% 獲利以覆蓋雙向手續費與微幅點差

    if s.get("highest_profit_pct", 0.0) >= breakeven_threshold:
        if is_long:
            breakeven_price = avg * (1 + fee_buffer)
            if breakeven_price > s.get('stop_loss', 0):
                s['stop_loss'] = breakeven_price
                if not s.get('is_breakeven_locked'):
                    s['is_breakeven_locked'] = True
                    logger.info(f"🛡️ [{sym}] 獲利達標，移動保本線已鎖定在：{breakeven_price:.4f}")
        else:
            # 空倉：保本線應在入場價下方（Universal SL 用 p >= sl，price 回升超過此點才退場）
            breakeven_price = avg * (1 - fee_buffer)
            if s.get('stop_loss', float('inf')) > breakeven_price:
                s['stop_loss'] = breakeven_price
                if not s.get('is_breakeven_locked'):
                    s['is_breakeven_locked'] = True
                    logger.info(f"🛡️ [{sym}] 獲利達標，移動保本線已鎖定在：{breakeven_price:.4f}")

    MIN_EXIT_RR = 1.3
    min_tp_dist = sl_dist * MIN_EXIT_RR
    if tp_dist < min_tp_dist:
        orig_tp_dist = tp_dist
        tp_dist = min_tp_dist
        logger.info(f"⚠️ [Exit RR Fix] {sym} 停利距離 {orig_tp_dist/avg*100:.2f}% < 停損 {sl_dist/avg*100:.2f}%×{MIN_EXIT_RR}，已強制拉至 {tp_dist/avg*100:.2f}%")

    tp = avg + tp_dist if is_long else avg - tp_dist

    if not s.get("is_breakeven_locked"):
        s["stop_loss"] = avg - sl_dist if is_long else avg + sl_dist

    # ── 階梯式收網與峰值比例鎖利 (Tiered Peak Profit Lock) ──
    # 取代原本單一的鎖利邏輯，改為更敏銳的階梯式保護
    _peak_lock = s.get("highest_profit_pct", 0.0)
    _locked_gain = 0.0
    _lock_desc = ""
    
    if _peak_lock >= 0.015:
        _locked_gain = max(0.012, _peak_lock - 0.002)
        _lock_desc = f"動態高點鎖利 ({_locked_gain*100:.2f}%)"
    elif _peak_lock >= 0.008:
        _locked_gain = max(0.006, _peak_lock - 0.002)
        _lock_desc = f"半路鎖利 ({_locked_gain*100:.2f}%)"
    elif _peak_lock >= 0.004:
        _locked_gain = max(0.002, _peak_lock - 0.002)
        _lock_desc = f"保本鎖利 ({_locked_gain*100:.2f}%)"

    if _locked_gain > 0:
        if is_long:
            _peak_sl = avg * (1 + _locked_gain)
            if _peak_sl > s.get("stop_loss", 0):
                _prev_sl = s.get("stop_loss", 0)
                s["stop_loss"] = _peak_sl
                if not s.get("_peak_lock_logged") or abs(_peak_sl - _prev_sl) > avg * 0.0005:
                    s["_peak_lock_logged"] = True
                    logger.info(f"🔒 [PeakLock階梯] {sym} 峰值 {_peak_lock*100:.2f}%，SL 推至 {_peak_sl:.4f}（{_lock_desc}）")
        else:
            _peak_sl = avg * (1 - _locked_gain)
            if _peak_sl < s.get("stop_loss", float('inf')):
                _prev_sl = s.get("stop_loss", float('inf'))
                s["stop_loss"] = _peak_sl
                if not s.get("_peak_lock_logged") or abs(_peak_sl - _prev_sl) > avg * 0.0005:
                    s["_peak_lock_logged"] = True
                    logger.info(f"🔒 [PeakLock階梯] {sym} 峰值 {_peak_lock*100:.2f}%，SL 推至 {_peak_sl:.4f}（{_lock_desc}）")

    sl = s.get("stop_loss", avg)

    if s.get("entry_count", 0) >= 2:
        first_entry = s.get("first_entry_price", avg)
        if first_entry <= 0:
            first_entry = avg
        atr_half = s.get("current_atr", atr_val) * 0.5

        if is_long:
            sl_floor = first_entry - atr_half + avg * 0.001
            sl_floor = min(sl_floor, avg)
            sl = max(sl, sl_floor)
        else:
            sl_floor = first_entry + atr_half - avg * 0.001
            sl_floor = max(sl_floor, avg)
            sl = min(sl, sl_floor)

    hard_sl_pct = get_effective_exit_setting(sym, "hard_stop_loss_pct", s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT), is_long)

    if is_long:
        hard_sl_limit = avg * (1 - hard_sl_pct)
        if sl < hard_sl_limit:
            sl = hard_sl_limit
        if "highest_sl" in s and sl < s["highest_sl"]:
            sl = s["highest_sl"]
        s["highest_sl"] = sl
    else:
        hard_sl_limit = avg * (1 + hard_sl_pct)
        if sl > hard_sl_limit:
            sl = hard_sl_limit
        if "lowest_sl" in s and sl > s["lowest_sl"]:
            sl = s["lowest_sl"]
        s["lowest_sl"] = sl

    s["stop_loss"] = sl

    is_bear_market = not ctx.MARKET_WIND.get("allow_long", True)
    is_bull_market = not ctx.MARKET_WIND.get("allow_short", True)
    if hold_sec > 1800:
        if (is_long and is_bear_market) or (not is_long and is_bull_market):
            shrink_ratio = 0.5
            new_sl_dist = atr_val * sl_base_raw * shrink_ratio
            if is_long:
                new_sl = avg - new_sl_dist
                if new_sl > sl:
                    sl = new_sl
                    logger.info(f"⚠️ [事件觸發防護] {sym} 持倉>30分且大盤逆風，強制縮短停損至 {sl_base_raw*shrink_ratio:.2f} ATR (新停損價: {sl:.4f})")
            else:
                new_sl = avg + new_sl_dist
                if new_sl < sl:
                    sl = new_sl
                    logger.info(f"⚠️ [事件觸發防護] {sym} 持倉>30分且大盤逆風，強制縮短停損至 {sl_base_raw*shrink_ratio:.2f} ATR (新停損價: {sl:.4f})")

    if profit_pct > s.get("highest_profit_pct", 0.0):
        s["highest_profit_pct"] = profit_pct
        s["peak_time"] = time.time()   # 記錄每次創新高的時間
    # 純收盤價峰值（不含盤中尖峰），供三層 ATR 鎖利使用
    # highest_profit_pct 包含 intra-candle HIGH，會被高 ATR 幣的噪音誤觸鎖利
    if profit_pct > s.get("highest_close_pct", 0.0):
        s["highest_close_pct"] = profit_pct
    if profit_pct < 0:
        s["has_been_negative"] = True

    _pt_peak = s.get("highest_close_pct", 0.0)

    # ── 三層 ATR 比例鎖利 (Tiered ATR Profit Lock) ──
    # 依照幣種 ATR 動態縮放退出閾值，取代固定 0.2% PeakTrail
    # Tier1 ≥ 1.5ATR：利潤跌至 0.5ATR 以下 → 保本出場
    # Tier2 ≥ 2.5ATR：利潤跌至峰值 50%（趨勢 OK）或 30%（趨勢差）以下 → 出場
    # Tier3 ≥ 4.0ATR：利潤跌至峰值 60%（趨勢 OK）或 40%（趨勢差）以下 → 出場
    _atr_pct = (s.get("entry_atr", atr_val) / avg) if (avg > 0 and atr_val > 0) else 0.002
    _is_trend_ok = (is_long and s["macd_line"] > s["macd_signal"]) or \
                   (not is_long and s["macd_line"] < s["macd_signal"])
    _tier3 = max(_atr_pct * 4.0, 0.012)
    _tier2 = max(_atr_pct * 2.5, 0.006)
    _tier1 = max(_atr_pct * 1.5, 0.0035)

    if _pt_peak >= _tier3 and profit_pct < _pt_peak * (0.6 if _is_trend_ok else 0.4):
        cs = "sell" if is_long else "buy"
        logger.info(
            f"🛡️ [大行情鎖利] {sym} 峰值 {_pt_peak*100:.2f}%(≥4ATR={_tier3*100:.2f}%) "
            f"回落至 {profit_pct*100:.2f}%，大行情保護出場"
        )
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Tier3_Lock]")
        return
    elif _pt_peak >= _tier2 and profit_pct < _pt_peak * (0.5 if _is_trend_ok else 0.3):
        cs = "sell" if is_long else "buy"
        logger.info(
            f"🛡️ [中利鎖利] {sym} 峰值 {_pt_peak*100:.2f}%(≥2.5ATR={_tier2*100:.2f}%) "
            f"回落至 {profit_pct*100:.2f}%，中利保護出場"
        )
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Tier2_Lock]")
        return
    elif _pt_peak >= _tier1 and profit_pct < max(_atr_pct * 0.5, 0.0015):
        cs = "sell" if is_long else "buy"
        logger.info(
            f"🛡️ [基本鎖利] {sym} 峰值 {_pt_peak*100:.2f}%(≥1.5ATR={_tier1*100:.2f}%) "
            f"現利 {profit_pct*100:.2f}% 跌至低位，基本保護出場"
        )
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Tier1_Lock]")
        return

    # ── 全週期移動停損 (update_trailing_stop on each tick) ──
    update_trailing_stop(sym, p, is_long)
    if s.get("trailing_stop_price", 0.0) > 0:
        if is_long:
            sl = max(sl, s["trailing_stop_price"])
        else:
            sl = min(sl, s["trailing_stop_price"])
        s["stop_loss"] = sl

    # ── 時間停滯出場 (Time-based Stagnation Exit) ──
    # 如果進場超過 15 分鐘（900秒），利潤卻小於 0.3%，且沒有觸發保本，視為動能耗盡
    if hold_sec >= 900 and -0.005 < profit_pct < 0.003 and s.get("highest_profit_pct", 0.0) < 0.004:
        cs = "sell" if is_long else "buy"
        logger.info(f"⏳ [時間停滯出場] {sym} 進場已達 {hold_sec/60:.0f} 分鐘，利潤僅 {profit_pct*100:.2f}%，動能枯竭，提前退場")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Time_Stagnation]")
        return

    # ── 入袋為安 (SafePocket_Exit) ──
    # 情境：持倉已有段時間，曾有獲利，但現在利潤縮水且方向朝SL → 先落袋不等被SL掃出
    _peak = s.get("highest_profit_pct", 0.0)
    _SAFE_POCKET_MIN_PEAK = 0.04  # 4% 預期獲利門檻
    _SAFE_POCKET_MIN_HOLD_SEC = 900  # 持倉至少 15 分鐘
    if (hold_sec >= _SAFE_POCKET_MIN_HOLD_SEC and           # 持倉至少 15 分鐘
        _peak >= _SAFE_POCKET_MIN_PEAK and                  # 曾觸及 4% 峰值獲利
        0.003 < profit_pct < min_tp_pct and                 # 仍有微利，但未達 TP 目標
        not s.get("is_breakeven_locked", False)):            # 保本線未鎖（鎖了SL已在保本，不需此邏輯）

        _drawdown_from_peak = (_peak - profit_pct) / _peak if _peak > 0 else 0
        _sl_dist_atr = abs(p - sl) / atr_val if atr_val > 0 else 99

        _trending_to_sl = False
        if len(s.get("ohlcv", [])) >= 3:
            _c_last = s["ohlcv"][-2][4]
            _c_prev = s["ohlcv"][-3][4]
            _trending_to_sl = (_c_last < _c_prev) if is_long else (_c_last > _c_prev)

        if _drawdown_from_peak >= 0.3 and profit_pct >= 0.004 and _sl_dist_atr < 1.5 and _trending_to_sl:
            _is_profit_locked = s.get("highest_profit_pct", 0) >= MIN_PROFIT_LOCK_THRESHOLD
            if _is_profit_locked and profit_pct > PROTECTED_PROFIT_FLOOR:
                logger.info(f"🛡️ [SafePocket保護] {sym} 獲利鎖定 (峰值:{_peak*100:.2f}% 現:{profit_pct*100:.2f}%>{PROTECTED_PROFIT_FLOOR*100:.2f}%)，維持持倉")
            else:
                cs = 'sell' if is_long else 'buy'
                logger.info(f"💰 [入袋為安] {sym} 峰值 {_peak*100:.2f}%→現 {profit_pct*100:.2f}%，距SL {_sl_dist_atr:.1f}x ATR，方向向損，先落袋")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[SafePocket_Exit]")
                s["highest_profit_pct"] = 0.0
                return

    # ── 量能高潮偵測 (Volume Climax Exit) ──
    _vc_vol = s.get("current_vol", 0)
    _vc_vol_ma = s.get("vol_ma20", 1)
    _vc_prev_close = s.get("prev_close", p)
    if _vc_vol > _vc_vol_ma * 2.0 and profit_pct >= 0.015 and p < _vc_prev_close:
        _trail_ext = s.get("trailing_highest", 0) if is_long else s.get("trailing_lowest", float('inf'))
        _at_new_extreme = (p >= _trail_ext * 0.999) if is_long else (p <= _trail_ext * 1.001)
        if not _at_new_extreme:
            _macd_h, _prev_macd_h = _macd_vals(s)
            _rsi_hist = s.get("rsi_history", [])
            _prev_rsi = _rsi_hist[-2] if len(_rsi_hist) >= 2 else s.get("current_rsi", 50.0)
            _curr_rsi = s.get("current_rsi", 50.0)
            _macd_decay = (_macd_h < _prev_macd_h) if is_long else (_macd_h > _prev_macd_h)
            _rsi_decay = (_curr_rsi < _prev_rsi) if is_long else (_curr_rsi > _prev_rsi)
            if _macd_decay or _rsi_decay:
                cs = 'sell' if is_long else 'buy'
                close_qty = abs(s["qty"]) * 0.5
                logger.info(f"🚀 [量能高潮-減半] {sym} 爆量 {_vc_vol/_vc_vol_ma:.1f}x 均量+收盤轉弱+動能衰竭(MACD:{_macd_decay},RSI:{_rsi_decay})，獲利 {profit_pct*100:.2f}%，減半倉鎖利留一半續跑")
                await close_position(sym, cs, close_qty, p, avg, reason="[Volume_Climax_Half]")
                s["has_partial_closed"] = True
                return

    # ── 量能衰竭偵測 (Vol_Decay_Exit) ──
    # 爆量後量縮 + 停止創新高 → 動能耗盡主動落袋
    if profit_pct >= 0.015 and len(s.get("ohlcv", [])) >= 3:
        _vd_vols = [x[5] for x in s["ohlcv"][-3:]]
        _vd_vol_ma = s.get("vol_ma20", 1)
        _vd_was_high = _vd_vols[-2] > _vd_vol_ma * 1.5          # 前根量偏高（1.5x均量）
        # 量能縮減門檻：本根量萎縮 30% 以上
        _vd_decaying = _vd_vols[-1] < _vd_vols[-2] * 0.70
        _vd_not_new_ext = (p < s.get("trailing_highest", p) * 0.999) if is_long else \
                          (p > s.get("trailing_lowest", p) * 1.001)
        if _vd_was_high and _vd_decaying and _vd_not_new_ext:
            # 加入「趨勢持續性」 MACD + EMA20 檢查
            _vd_macd_h, _vd_prev_macd_h = _macd_vals(s)
            _vd_ema20 = s.get("ema20", 0.0)
            _vd_macd_expanding = (_vd_macd_h > _vd_prev_macd_h) if is_long else (_vd_macd_h < _vd_prev_macd_h)
            _vd_price_above_ema = (p > _vd_ema20) if (is_long and _vd_ema20 > 0) else \
                                  (p < _vd_ema20) if (not is_long and _vd_ema20 > 0) else False
            _vd_trend_intact = _vd_macd_expanding and _vd_price_above_ema

            if _vd_trend_intact:
                logger.info(f"⚡ [Vol_Decay_Vetoed] {sym} 量縮但 MACD 擴張且價格在 EMA20 {'上' if is_long else '下'}方，趨勢中場休息，抑制 Vol_Decay_Exit")
            else:
                # 強勢趨勢要求利潤進度達 85% 才出場，防止大行情中途被量縮誤退
                _vd_progress = profit_pct / min_tp_pct if min_tp_pct > 0 else 0.0
                is_strong = s.get("current_strength", 0.0) >= 15.0 or s.get("pending_route", "") == "a"
                vd_threshold = 0.85 if is_strong else 0.70
                if _vd_progress >= vd_threshold:
                    cs = 'sell' if is_long else 'buy'
                    close_qty = abs(s["qty"]) * 0.5
                    logger.info(f"📉 [Vol_Decay_Harvest-減半] {sym} 量能衰竭，獲利進度 {_vd_progress*100:.0f}% >= {vd_threshold*100:.0f}%，減半倉落袋 {profit_pct*100:.2f}% 留一半續跑")
                    await close_position(sym, cs, close_qty, p, avg, reason="[Vol_Decay_Half]")
                    s["has_partial_closed"] = True
                    return
                else:
                    logger.info(f"[Vol_Decay_Held] {sym} 量能衰竭但獲利進度 {_vd_progress*100:.0f}% < {vd_threshold*100:.0f}%，繼續持倉")

    # ── 低流動性防禦 (Low Liquidity Defense) ──
    # 進場後量能持續萎縮 → 死水區，價格無力達TP，提前鎖利
    if profit_pct >= 0.010 and len(s.get("ohlcv", [])) >= 3:
        _ll_vols = [x[5] for x in s["ohlcv"][-3:]]
        _ll_vol_ma = s.get("vol_ma20", 1)
        _ll_drying = all(v < _ll_vol_ma * 0.7 for v in _ll_vols)  # 連續3根量均低於均量70%
        if _ll_drying:
            _ll_stagnant = (p < s.get("trailing_highest", p) * 0.999) if is_long else \
                           (p > s.get("trailing_lowest", p) * 1.001)  # 價格停止創新極值
            if _ll_stagnant:
                cs = 'sell' if is_long else 'buy'
                close_qty = abs(s["qty"]) * 0.5
                logger.info(f"📉 [Low_Liquidity-減半] {sym} 量能持續枯竭（連3根 < 均量70%）且動能停滯，減半鎖住 {profit_pct*100:.2f}% 利潤留一半")
                await close_position(sym, cs, close_qty, p, avg, reason="[Low_Liquidity_Half]")
                s["has_partial_closed"] = True
                return

    # ── Trailing TP：槓桿自適應高點停利 ──
    # 啟動門檻 = max(2.0%÷槓桿, 0.3x ATR)；ATR 分層動態縮緊
    atr_pct = atr_val / avg if avg > 0 else 0.005
    _lev = s.get("leverage", 4)
    _hp = s.get("highest_profit_pct", 0.0)
    ts_activation_pct = max(0.030 / _lev, atr_pct * 0.5)
    # 動態追蹤距離：利潤越高給越大空間讓行情繼續跑，避免 5%+ 大行情被雜訊洗出場
    if _hp >= 0.05:     ts_retracement_pct = atr_pct * 1.5   # > 5%：保留充足空間繼續跑（原 0.8 太緊）
    elif _hp >= 0.02:   ts_retracement_pct = atr_pct * 1.2   # 2-5%：適中空間
    elif _hp >= 0.008:  ts_retracement_pct = atr_pct * 1.5   # 0.8-2%：早期利潤也留呼吸空間
    else:               ts_retracement_pct = atr_pct * 1.5   # < 0.8%：剛啟動
    ts_retracement_pct = max(ts_retracement_pct, 0.0015)      # 絕對下限 0.15%（原 0.08% 太小）
    if s["highest_profit_pct"] >= ts_activation_pct:
        if is_long:
            peak_price = s.get("trailing_highest", avg)
            trail_sl_price = peak_price * (1 - ts_retracement_pct)
            if trail_sl_price > s.get("stop_loss", 0):
                s["stop_loss"] = trail_sl_price
            if p <= trail_sl_price:
                cs = 'sell'
                lock_pnl = (peak_price - avg) / avg * 100
                _exit_p = max(p, trail_sl_price)
                logger.info(f"📉 [高點鎖利] {sym} 多單從高點 {peak_price:.4f} 回落至 {p:.4f}，鎖利 (峰值獲利:{lock_pnl:.2f}%)，出場 @ {_exit_p:.4f}")
                await close_position(sym, cs, abs(s["qty"]), _exit_p, avg, reason="[TrailTP_Peak]")
                s["highest_profit_pct"] = 0.0
                return
        else:
            trough_price = s.get("trailing_lowest", avg)
            trail_sl_price = trough_price * (1 + ts_retracement_pct)
            if s.get("stop_loss", float('inf')) > trail_sl_price:
                s["stop_loss"] = trail_sl_price
            if p >= trail_sl_price:
                cs = 'buy'
                lock_pnl = (avg - trough_price) / avg * 100
                _exit_p = min(p, trail_sl_price)
                logger.info(f"📉 [低點鎖利] {sym} 空單從低點 {trough_price:.4f} 反彈至 {p:.4f}，鎖利 (峰值獲利:{lock_pnl:.2f}%)，出場 @ {_exit_p:.4f}")
                await close_position(sym, cs, abs(s["qty"]), _exit_p, avg, reason="[TrailTP_Peak]")
                s["highest_profit_pct"] = 0.0
                return

    regime_decision, regime_reason = detect_market_regime(sym, p, avg, is_long)
    if regime_decision == "BREAKOUT_REVERSAL":
        cs = 'sell' if is_long else 'buy'
        logger.info(f"🚨 [市場 regime] {sym} {regime_reason}，立即平倉並考慮反手")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Breakout_Fail]", is_stop_loss=True)
        s["highest_profit_pct"] = 0.0

        if _check_reversal_allowed(sym, s):
            if s.get("consecutive_losses", 0) >= 2:
                s["reversal_ban_until"] = time.time() + 14400
            last_reverse = s.get("last_reverse_time", 0)
            if time.time() - last_reverse > 1800:
                s["pending_reverse"] = "sell" if is_long else "buy"
                s["pending_reverse_time"] = time.time()
                s["last_reverse_time"] = time.time()
            else:
                logger.info(f"⏳ [反手冷卻] {sym} 距離上次反手不到 30 分鐘，為了防禦假震盪，本次放棄反手。")
        return

    if regime_decision == "RANGE_PROFIT_TAKE":
        cs = 'sell' if is_long else 'buy'
        logger.info(f"📈 [盤整獲利] {sym} {regime_reason}，提前獲利了結")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
        s["highest_profit_pct"] = 0.0
        return

    s["pnl_history"].append(profit_pct * 100)
    if len(s["pnl_history"]) > 8:
        s["pnl_history"].pop(0)

    if profit_pct > min_tp_pct and s["highest_profit_pct"] > min_tp_pct:
        drawdown = (s["highest_profit_pct"] - profit_pct) / s["highest_profit_pct"]
        if drawdown >= 0.25:
            macd_hist_expanding = False
            try:
                closes = np.array([x[4] for x in s["ohlcv"]])
                _, _, m_hist, p_line, p_sig = calculate_macd(closes)
                p_hist = p_line - p_sig
                macd_hist_expanding = abs(m_hist) > abs(p_hist)
            except:
                pass

            if not macd_hist_expanding:
                cs = 'sell' if is_long else 'buy'
                logger.info(f"📉 [動能衰減] {sym} 利潤從最高 {s['highest_profit_pct']*100:.2f}% 回落 25% (現為 {profit_pct*100:.2f}%) 且 MACD 衰退，提早獲利了結")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Whipsaw_Stop_top]")
                s["highest_profit_pct"] = 0.0
                return
    if p > s["trailing_highest"]:
        s["trailing_highest"] = p
    if p < s["trailing_lowest"]:
        s["trailing_lowest"] = p

    macd_is_down = (s["macd_line"] < s["macd_signal"]) and (s.get("prev_macd_line", 0.0) < s.get("prev_macd_signal", 0.0))
    macd_is_up = (s["macd_line"] > s["macd_signal"]) and (s.get("prev_macd_line", 0.0) > s.get("prev_macd_signal", 0.0))
    sl_pct = s.get("hard_stop_loss_pct", 0.02)
    early_exit_limit = -(sl_pct * 0.5)
    if ((is_long and macd_is_down) or (not is_long and macd_is_up)) and (profit_pct < early_exit_limit or profit_pct > 0.015):
        cs = 'sell' if is_long else 'buy'
        is_sl = profit_pct < 0.0
        logger.info(f"📉 [反轉出場] {sym} MACD連續兩根確認反向且達門檻，立即平倉 (損益: {profit_pct*100:.2f}%)")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Trend_Follow]", is_stop_loss=is_sl)
        return

    atr_pct = (s.get("entry_atr", atr_val) / avg) if avg > 0 else 0.002

    personality = s.get("personality", "steady_trend")
    tier_mult = 1.0
    if personality == "calm_range":
        tier_mult = 0.8
    elif personality == "volatile_breakout":
        tier_mult = 1.2

    tier3_target = max(atr_pct * 4.0 * tier_mult, 0.012 * tier_mult, 0.008)
    tier2_target = max(atr_pct * 2.5 * tier_mult, 0.006 * tier_mult, 0.006)
    tier1_target = max(atr_pct * 1.5 * tier_mult, 0.003 * tier_mult, 0.003, min_tp_pct * 0.8)

    if len(s["ohlcv"]) >= 5:
        c1 = s["ohlcv"][-2]
        c2 = s["ohlcv"][-3]

        recent_vols = [x[5] for x in s["ohlcv"][-5:-1]]
        vol_ma20 = s.get("vol_ma20", 0)
        has_recent_climax = max(recent_vols) > vol_ma20 * 1.5 if vol_ma20 > 0 else True

        is_moving_progress = (p > c1[2]) if is_long else (p < c1[3])

        sma200 = s.get("sma200_15m", 0)
        bb_up = s.get("bb_up", 0)
        bb_low = s.get("bb_low", 0)

        near_resistance = (bb_up > 0 and p >= bb_up * 0.99) or (sma200 > 0 and p >= sma200 * 1.01)
        near_support = (bb_low > 0 and p <= bb_low * 1.01) or (sma200 > 0 and p <= sma200 * 0.99)
        extreme_resistance = bb_up > 0 and p >= bb_up
        extreme_support = bb_low > 0 and p <= bb_low

        is_valid_location = (is_long and (near_resistance or extreme_resistance)) or (not is_long and (near_support or extreme_support))

        is_in_consolidation = (current_atr > 0 and atr_24h_avg > 0 and current_atr < atr_24h_avg * 0.8)

        vol_threshold = 0.50 if is_in_consolidation else 0.65

        divergence_exit = False
        if has_recent_climax and not is_moving_progress and is_valid_location:
            if is_long and c1[4] > c2[4] and c1[5] < c2[5] * vol_threshold:
                divergence_exit = True
            elif not is_long and c1[4] < c2[4] and c1[5] < c2[5] * vol_threshold:
                divergence_exit = True

        if divergence_exit and profit_pct >= min_tp_pct * 0.6:
            cs = 'sell' if is_long else 'buy'
            logger.info(f"📉 [量價背離] {sym} 抵達關鍵區位且量縮停滯 (V:{c1[5]:.0f} < {vol_threshold:.2f}x)，動能竭盡提前平倉！")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Vol_Divergence]")
            s["highest_profit_pct"] = 0.0
            return

    macd_hist_now = s.get("macd_hist", 0.0)
    is_strong = (
        (is_long and s["current_rsi"] > 55 and macd_hist_now > 0) or
        (not is_long and s["current_rsi"] < 45 and macd_hist_now < 0)
    )

    if True:
        if profit_pct > 0.02 and s.get("entry_count", 0) > 0 and s.get("max_additional_entries", 0) > 0:
            logger.info(f"🎯 [強制鎖利] {sym} 獲利已達 2%，鎖定利潤，禁止繼續加倉")
            s["max_additional_entries"] = 0

        if s["highest_profit_pct"] >= tier1_target:
            atr_val = s.get("current_atr", 0)
            atr_ma20 = s.get("atr_ma20", 0)
            if is_strong:
                trail_trigger = 0.65 if atr_val > atr_ma20 else 0.70
            else:
                trail_trigger = 0.80 if atr_val > atr_ma20 else 0.85

            if len(s.get("entries", [])) > 1:
                trail_trigger -= 0.05

            if profit_pct <= s["highest_profit_pct"] * trail_trigger:
                cs = 'sell' if is_long else 'buy'
                logger.info(f"🛡️ [動態移動停利] {sym} 利潤從最高 {s['highest_profit_pct']*100:.3f}% 回吐 (觸發點 {trail_trigger:.2f})，於 {profit_pct*100:.3f}% 鎖定利潤出場")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason=f"[Trailing_Stop_{trail_trigger}]")
                s["highest_profit_pct"] = 0.0
                return

    if not is_strong:
        recent_vols = [x[5] for x in s["ohlcv"][-4:-1]] if len(s["ohlcv"]) >= 4 else []
        vol_ma20 = s.get("vol_ma20", 1)
        is_vol_stagnant = len(recent_vols) >= 3 and all(v < vol_ma20 * 0.6 for v in recent_vols)
        bb_width = s.get("bb_up", 0) - s.get("bb_low", 0)
        is_range_tight = (bb_width / p) < 0.003 if p > 0 else False

        entry_layers = len(s.get("entries", []))
        if is_strong:
            time_decay_limit = 5400 if entry_layers <= 1 else 7200
        else:
            time_decay_limit = 2400 if entry_layers <= 1 else 5400

        if hold_sec > time_decay_limit:
            cs = 'sell' if is_long else 'buy'
            if profit_pct >= min_tp_pct:
                logger.info(f"⏳ [時間衰減獲利] {sym} 持倉已達 {hold_sec//60} 分鐘，獲利 {profit_pct*100:.2f}% >= {min_tp_pct*100:.2f}%，出場！")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Time_Decay_Exit]")
                s["highest_profit_pct"] = 0.0
                return
            elif profit_pct <= -0.003:
                logger.info(f"⏳ [時間衰減停損] {sym} 持倉已達 {hold_sec//60} 分鐘但虧損 {profit_pct*100:.2f}%，切損出場！")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Time_Decay_Stop]")
                s["highest_profit_pct"] = 0.0
                return
            elif 0 < profit_pct < min_tp_pct and not s.get("is_breakeven_locked"):
                s["is_breakeven_locked"] = True
                s["stop_loss"] = avg
                logger.info(f"⏳ [時間衰減保本] {sym} 超時但利潤 {profit_pct*100:.2f}% 未達目標 {min_tp_pct*100:.2f}%，鎖定保本繼續等")

        from core.indicators import get_dynamic_stagnation_limit
        stagnation_limit = get_dynamic_stagnation_limit(s["current_atr"], s["atr_ma20"])
        if hold_sec > stagnation_limit and profit_pct >= min_tp_pct * 0.8:
            if is_vol_stagnant and is_range_tight:
                if not s["has_partial_closed"]:
                    if min_tp_pct * 0.7 <= profit_pct < min_tp_pct:
                        half = abs(s["qty"]) * 0.5
                        cs = 'sell' if is_long else 'buy'
                        logger.info(f"⏳ [量能僵局] {sym} 持倉{stagnation_limit//60}分且量縮橫盤，平50%")
                        await close_position(sym, cs, half, p, avg, reason="[Vol_Stagnation_1]")
                        s["has_partial_closed"] = True
                        return
                    else:
                        cs = 'sell' if is_long else 'buy'
                        reason = "[Vol_Stagnation_Exit]" if profit_pct >= min_tp_pct else "[Stagnation_BreakEven]"
                        logger.info(f"⏳ [量能僵局] {sym} 持倉{stagnation_limit//60}分且量縮橫盤，全平釋放資金")
                        await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason)
                        s["highest_profit_pct"] = 0.0
                        return
        if s["has_partial_closed"] and hold_sec > 480 and min_tp_pct * 0.5 <= profit_pct < min_tp_pct:
            if is_vol_stagnant and is_range_tight:
                cs = 'sell' if is_long else 'buy'
                logger.info(f"⏳ [量能僵局] {sym} 剩餘50%持倉8分仍未突破1%且量縮橫盤，全平")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Vol_Stagnation_2]")
                s["highest_profit_pct"] = 0.0
                return
            s["has_partial_closed"] = False
            return

        profile_type = COIN_PROFILE_CONFIG.get(sym, {}).get("profile_type", "")
        if s.get("personality") == "calm":
            weak_tp = 0.035
        elif profile_type == "High_Beta_Momentum":
            weak_tp = 0.045
        elif profile_type == "Speculative_Risk":
            weak_tp = 0.040
        else:
            weak_tp = 0.030
        if s["highest_profit_pct"] >= weak_tp:
            # 引入「 MACD 擴張」檢查，避免趨勢中途領界萬金出場
            _wtp_macd_h, _wtp_prev_macd_h = _macd_vals(s)
            _wtp_rsi = s.get("current_rsi", 50.0)
            _OVERBOUGHT_RSI = 78.0
            _wtp_macd_expanding = (_wtp_macd_h > _wtp_prev_macd_h) if is_long else (_wtp_macd_h < _wtp_prev_macd_h)
            _wtp_not_extreme_rsi = (_wtp_rsi < _OVERBOUGHT_RSI) if is_long else (_wtp_rsi > (100 - _OVERBOUGHT_RSI))

            if _wtp_macd_expanding and _wtp_not_extreme_rsi:
                logger.info(f"⚡ [保留動能] {sym} 弱勢已達{weak_tp*100:.1f}% 且 MACD 仍在擴張 (RSI={_wtp_rsi:.1f})，繼續跑大波段，暫不停利")
            elif not has_strong_momentum(sym, is_long):
                cs = 'sell' if is_long else 'buy'
                logger.info(f"🎯 [弱勢快速停利] {sym} 弱勢利潤達{weak_tp*100:.1f}%，動能不足則落袋")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
                s["highest_profit_pct"] = 0.0
                return
            else:
                logger.info(f"⚡ [保留動能] {sym} 弱勢已達{weak_tp*100:.1f}%但整體動能仍強，暫不停利")
    else:
        if s["highest_profit_pct"] >= 0.015:
            if s["highest_profit_pct"] >= 0.03:
                retrace_limit = 0.008
            else:
                retrace_limit = 0.005
            limit_down = 1.0 - retrace_limit
            limit_up   = 1.0 + retrace_limit

            if (is_long and p <= s["trailing_highest"] * limit_down) or (not is_long and p >= s["trailing_lowest"] * limit_up):
                cs = 'sell' if is_long else 'buy'
                locked = (s["highest_profit_pct"] - retrace_limit) * 100
                logger.info(f"🏃 [動態停利] {sym} 最高點回撤 {retrace_limit*100:.1f}%，鎖住約 {locked:.2f}% 獲利")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Trend_Follow]")
                s["highest_profit_pct"] = 0.0
                return

    # ── 強制停利 (Hard TP) ──
    # 若 MACD 仍在擴張（動能持續），跳過強制停利，讓追蹤止損跑
    if (is_long and p >= tp) or (not is_long and p <= tp):
        _tp_macd_h, _tp_prev_macd_h = _macd_vals(s)
        _tp_macd_expanding = (_tp_macd_h > _tp_prev_macd_h) if is_long else (_tp_macd_h < _tp_prev_macd_h)
        if _tp_macd_expanding:
            logger.info(f"⚡ [停利暫緩] {sym} 已達目標價但 MACD 仍在擴張，讓子彈飛，由追蹤止損守高點")
        else:
            cs = 'sell' if is_long else 'buy'
            tp_pct = abs(tp - avg) / avg * 100
            logger.info(f"🎯 [停利達成] {sym} 達到目標價 {tp:.6f} ({tp_pct:.1f}%)，獲利出場")
            await close_position(sym, cs, abs(s["qty"]), tp, avg, reason="[Take_Profit]")
            s["highest_profit_pct"] = 0.0
            return

    # ── 停損檢查 (Universal SL) ──
    if (is_long and p <= sl) or (not is_long and p >= sl):
        cs = 'sell' if is_long else 'buy'
        sl_pct = abs(sl - avg) / avg * 100
        reason_str = "[Breakeven_Stop]" if sl == avg else "[Trend_Follow]"
        logger.info(f"🛑 [{reason_str}] {sym} -{sl_pct:.1f}%")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason_str, is_stop_loss=True)
        if abs(profit_pct) > 0.015 and reason_str != "[Breakeven_Stop]" and _check_reversal_allowed(sym, s):
            if s.get("consecutive_losses", 0) >= 2:
                s["reversal_ban_until"] = time.time() + 14400
            last_reverse = s.get("last_reverse_time", 0)
            if time.time() - last_reverse > 1800:
                rev_side = "buy" if not is_long else "sell"
                s["pending_reverse"] = rev_side
                s["pending_reverse_time"] = time.time()
                s["last_reverse_time"] = time.time()
                logger.info(f"🔄 [SL_Reverse] {sym} SL 後偵測到強勢逆向突破，設置反手 → {rev_side}")
        return


def _check_reversal_allowed(sym, s):
    losses = s.get("consecutive_losses", 0)
    if losses < 2:
        return True
    ban_until = s.get("reversal_ban_until", 0)
    if time.time() >= ban_until:
        s["reversal_ban_until"] = 0
        return True
    ban_mins = int((ban_until - time.time()) / 60)
    logger.info(f"⛔ [反手禁用] {sym} 連虧 {losses} 次，反手功能禁用 (剩 {ban_mins} 分鐘)")
    return False


async def fast_exit_loop(exchange):
    """快速出場掃描 (FastExit Loop) - 每秒執行一次出場檢查"""
    while True:
        try:
            for sym in list(ctx.ALL_SYMBOLS):
                s = ctx.STATES.get(sym)
                if not s or abs(s.get("qty", 0)) < 0.000001:
                    continue
                if s.get("adjusted_this_tick", False):
                    continue
                try:
                    await check_exits(sym)
                except Exception as e:
                    logger.info(f"⚠️ [FastExit] {sym} 出場檢查異常: {e}")
        except Exception as e:
            logger.info(f"⚠️ [FastExit_Loop] 全域異常: {e}")
        await asyncio.sleep(1)
