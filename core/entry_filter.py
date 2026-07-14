import logging
import time
import numpy as np

from core import ctx
from core.config import (PAPER_TRADING, SL_ATR_MULTIPLIER, TP_ATR_MULTIPLIER,
    COIN_PROFILE_CONFIG, USE_BTC_MACRO_FILTER, get_entry_strictness_profile)
from core.indicators import _get_atr, _macd_vals, calculate_macd
from core.symbol_profile import get_effective_exit_setting, has_strong_momentum, get_dynamic_atr_multiplier, SYMBOL_PROFILES
from core.balance import is_daily_loss_halted, get_fee_overhead
from core.state_manager import get_open_position_count, get_active_count, is_symbol_locked

logger = logging.getLogger(__name__)


def is_valid_candle(sym, side):
    """
    插針過濾器 (Wick / Pin-Bar Filter)
    在進場前判斷最新 K 線是否因影線過長而為無效訊號。
    - buy:  上影線 > 實體 * 門檻 → 拒絕（壓力太強）
    - sell: 下影線 > 實體 * 門檻 → 拒絕（支撐太強）
    Returns True 代表 K 線合格，可進場；False 代表過濾掉。
    """
    s = ctx.STATES[sym]
    if len(s["ohlcv"]) < 2:
        return True

    candle = s["ohlcv"][-1]
    prev_candle = s["ohlcv"][-2]
    open_price = float(candle[1])
    high = float(candle[2])
    low = float(candle[3])
    close_price = float(candle[4])
    prev_close = float(prev_candle[4])  # noqa: F841  (保留以備未來使用)
    body = abs(close_price - open_price)
    upper_wick = high - max(open_price, close_price)
    lower_wick = min(open_price, close_price) - low

    profile = get_entry_strictness_profile()

    # --- 【新增：量能過濾】 ---
    current_vol = s.get("current_vol", 0.0)
    vol_ma20 = s.get("vol_ma20", 0.0)
    volume_ratio = profile["volume_ratio"]
    if vol_ma20 > 0 and current_vol < vol_ma20 * volume_ratio:
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能過濾] 當前量({current_vol:.0f}) < {volume_ratio:.2f}x均量({vol_ma20*volume_ratio:.0f})，拒絕進場")
        return False
    # --------------------------

    pin_threshold = profile["pin_threshold"]
    candle_range = max(high - low, 1e-8)
    body_ratio = body / candle_range
    if body_ratio < profile["min_body_ratio"] or s.get("current_vol", 0.0) < max(80.0, s.get("vol_ma20", 0.0) * 0.4):
        pin_threshold = max(1.8, pin_threshold - 0.4)
    ema20 = s.get("ema20", 0.0)
    if ema20 > 0:
        if side == 'buy' and close_price < ema20:
            pin_threshold = max(1.5, pin_threshold - 0.5)
        if side == 'sell' and close_price > ema20:
            pin_threshold = max(1.5, pin_threshold - 0.5)

    enabled = pin_threshold < 2.0

    # [新增] MACD 動能強勁且持續放大時，放寬容錯空間
    macd_hist = s.get("macd_hist", 0.0)
    prev_macd_hist = 0.0
    try:
        if len(s.get("ohlcv", [])) >= 34:
            closes = np.array([x[4] for x in s["ohlcv"]])
            _, _, m_hist, p_line, p_sig = calculate_macd(closes)
            macd_hist = m_hist
            prev_macd_hist = p_line - p_sig
    except:
        pass

    is_strong_macd = False
    if side == 'buy' and macd_hist > 0 and macd_hist > prev_macd_hist:
        is_strong_macd = True
    elif side == 'sell' and macd_hist < 0 and macd_hist < prev_macd_hist:
        is_strong_macd = True

    if is_strong_macd:
        pin_threshold = max(pin_threshold, profile["pin_threshold"] + 0.5)
        enabled = False

    # [2026-07-14 放寬A] 插針門檻從 1.2 放寬至 1.8，減少過度攔截合法訊號。
    # 上/下影線超過實體 1.8 倍才視為插針假突破，拒絕開倉。
    pin_threshold = 1.8
    logger.info(f"@@COIN_DEBUG@@ 🔧 {sym} 插針門檻放寬至 {pin_threshold:.1f}")

    if side == 'buy':
        if body <= 0:
            return True
        if upper_wick > body * pin_threshold:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 上影線過長 (上影線 {upper_wick:.4f} > 實體 {body:.4f} * {pin_threshold:.1f})")
            return False
        return True

    if side == 'sell':
        # Doji 辨別：實體越小於 0.05% 價格時，用 ATR 絕對値替代比例判斷
        atr_val = s.get("current_atr", 0.0)
        min_body = max(atr_val * 0.05, close_price * 0.0005) if atr_val > 0 else close_price * 0.0005
        if body < min_body:
            return True
        if lower_wick > body * pin_threshold:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 下影線過長 (下影線 {lower_wick:.4f} > 實體 {body:.4f} * {pin_threshold:.1f})")
            return False
        return True

    return True  # fallback


def get_dynamic_volume_factor(states):
    total_current_vol = 0.0
    total_ma20_vol = 0.0

    for s in states.values():
        total_current_vol += s.get("current_vol", 0)
        total_ma20_vol += s.get("vol_ma20", 0)

    # 防止除以零的錯誤
    if total_ma20_vol == 0:
        return 1.2

    market_volume_state = total_current_vol / total_ma20_vol

    # 邏輯：如果整體市場量能低於平均水準，放寬門檻至 1.0
    # 否則保持 1.2 的高標準過濾
    return 1.0 if market_volume_state < 1.0 else 1.2


def is_entry_volume_confirmed(sym, side):
    s = ctx.STATES[sym]
    if len(s["ohlcv"]) < 2:
        return False
    current_vol = s["current_vol"]
    vol_ma20 = s["vol_ma20"]
    if vol_ma20 <= 0:
        return False

    # [Layer 3] 動態量能門檻 (根據幣種性格動態調整)
    base_vol_factor = s.get("volume_threshold_factor", 0.4)
    vol_factor = base_vol_factor

    # [新增] 大盤量能過濾：環境感知
    market_dynamic_factor = get_dynamic_volume_factor(ctx.STATES)
    if market_dynamic_factor == 1.0:
        vol_factor = 0.5
        logger.info(f"@@COIN_DEBUG@@ ⚡ {sym} 整體市場量縮，動態放寬量能門檻至 0.5x")
    else:
        # [新增] RSI 強勢放寬量能門檻 vs 極端狂熱提高門檻
        rsi = s.get("current_rsi", 50.0)
        if s.get("is_extreme_high_rsi", False):
            vol_factor = vol_factor * 1.2
            logger.info(f"@@COIN_DEBUG@@ ⚠️ {sym} 觸發 [極值防禦] RSI ({rsi:.1f}) 處於狂熱頂點，強制提高量能門檻至 {vol_factor:.2f}x 防追高")
        elif rsi > 70 or rsi < 30:
            vol_factor = 0.5
            logger.info(f"@@COIN_DEBUG@@ ⚡ {sym} 行情強勢 (RSI: {rsi:.1f})，動態放寬量能門檻至 0.5x")
        else:
            # [新增] 根據 ATR 高低自動動態調整倍數
            atr_24h_avg = s.get("atr_24h_avg", 0.0)
            current_atr = s.get("current_atr", 0.0)
            atr_ratio = (current_atr / atr_24h_avg) if atr_24h_avg > 0 else 1.0

            if atr_ratio >= 1.5:
                vol_factor = min(1.1, vol_factor)
                logger.info(f"@@COIN_DEBUG@@ ⚡ {sym} 波動率極高 (ATR ratio: {atr_ratio:.2f})，動態降低量能門檻至 {vol_factor}x")
            elif atr_ratio >= 1.2:
                vol_factor = max(1.0, vol_factor - 0.2)
                logger.info(f"@@COIN_DEBUG@@ ⚡ {sym} 波動率偏高 (ATR ratio: {atr_ratio:.2f})，微調量能門檻至 {vol_factor:.1f}x")

    min_volume = vol_ma20 * vol_factor
    if current_vol < min_volume:
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能過濾] 當前量 {current_vol:.2f} < 門檻 {min_volume:.2f} (均量:{vol_ma20:.2f} * {vol_factor})")
        return False

    # --- R:R (盈虧比) 過濾 + 手續費最小獲利空間 ---
    is_long = (side == 'buy')
    sl_multiplier = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long)
    tp_multiplier = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), is_long)

    current_atr = s.get("current_atr", 0.0)
    cp_rr = s.get("close_price", 0.0)
    leverage_rr = s.get("leverage", 5)

    expected_profit = tp_multiplier * current_atr
    expected_risk = sl_multiplier * current_atr

    rr_ratio = expected_profit / expected_risk if expected_risk > 0 else 0
    # 盈虧比放寬：全局最高上限設為 2.0，降低開倉門檻
    rr_threshold = min(s.get("rr_threshold", 1.3), 2.0)
    if rr_ratio < rr_threshold:
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [盈虧比過濾] 預計R:R ({rr_ratio:.2f}) < {rr_threshold} (TP: {tp_multiplier}x, SL: {sl_multiplier}x)")
        return False

    # --- 手續費最小獲利空間：預期利潤必須 > 來回手續費（以名義值比例換算）---
    if cp_rr > 0 and current_atr > 0:
        fee_overhead_pct = get_fee_overhead(float(leverage_rr))  # 保證金基準的來回費用
        expected_profit_pct = expected_profit / cp_rr            # 預期利潤佔現價的比例
        min_profit_pct = fee_overhead_pct * 2.0                  # 至少要賺到手續費的 2 倍才值得進
        if expected_profit_pct < min_profit_pct:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [手續費門檻] 預期利潤 {expected_profit_pct*100:.3f}% < 最低門檻 {min_profit_pct*100:.3f}% (ATR 太小，手續費吃掉獲利)")
            return False

    return True


def is_entry_pin_safe(sym, side):
    """
    插針過濾 (Pin-Bar Safety Check)
    檢查最新 K 線是否存在對方向不利的長影線（插針假突破）。
    - 多單：若上影線 > 實體 * pin_threshold，代表壓力強，拒絕做多。
    - 空單：若下影線 > 實體 * pin_threshold，代表支撐強，拒絕做空。
    若 K 線資料不足或實體為 0，放行（保守地允許）。
    """
    s = ctx.STATES.get(sym)
    if not s or len(s.get("ohlcv", [])) < 1:
        return True  # 資料不足，放行

    last = s["ohlcv"][-1]
    o, h, l, c = last[1], last[2], last[3], last[4]
    body = abs(c - o)
    if body < 1e-10:
        return True  # 十字星，不判定插針

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    from core.config import get_entry_strictness_profile
    profile = get_entry_strictness_profile()
    pin_threshold = profile.get("pin_threshold", 2.0)

    if side == "buy" and len(s["ohlcv"]) >= 2 and c <= s["ohlcv"][-2][4] and upper_wick > body * pin_threshold:
        return False  # 上影線過長，壓力強 → 拒絕多單
    if side == "sell" and lower_wick > body * pin_threshold:
        return False  # 下影線過長，支撐強 → 拒絕空單
    return True


BTC_MOMENTUM_OVERRIDE_STRENGTH = 18.0  # 放寬：原25，降至18，讓更多幣種自身強訊號可突破BTC MACD封鎖


def has_strong_local_momentum_override(route, strength):
    """Only a fully aligned, very strong Route A signal may ignore BTC 1H lag."""
    return route == "a" and float(strength or 0.0) >= BTC_MOMENTUM_OVERRIDE_STRENGTH


def is_last_closed_1m_aligned(state, side):
    """Use the last *closed* 1m candle; the live candle is too noisy for a hard gate."""
    candles = state.get("ohlcv", [])
    if len(candles) < 2:
        return True
    last_closed = candles[-2]
    open_price, close_price = float(last_closed[1]), float(last_closed[4])
    if side == "buy":
        return close_price >= open_price
    if side == "sell":
        return close_price <= open_price
    return False


def is_entry_allowed(sym, side, route="a", strength=0.0):
    s = ctx.STATES[sym]
    cp = s["close_price"]
    from core.config import ENTRY_STRICTNESS_MODE
    is_relaxed = (ENTRY_STRICTNESS_MODE == "relaxed")

    # 新幣沒設定檔 → 自動套用保守預設，避免 DEFAULT_LEVERAGE=5 失控
    if sym not in COIN_PROFILE_CONFIG:
        from core.config import DEFAULT_NEW_COIN_PROFILE
        COIN_PROFILE_CONFIG[sym] = DEFAULT_NEW_COIN_PROFILE.copy()
        logger.info(f"⚠️ [AUTO_PROFILE] {sym} 補套預設風控（2x 槓桿 / hard_sl 1.5%）")

    if route == "Automatic_Reverse":
        logger.info(f"@@COIN_DEBUG@@ ⚡ [反手豁免] {sym} 來自強勢反手，跳過空間/趨勢/大盤過濾")
        return True

    # ── [2026-07-14 修正B] BTC 短期衝擊過濾器 (已改為僅作參考，不攔截) ──
    btc_state = ctx.STATES.get("BTCUSDT")
    if btc_state and "ohlcv" in btc_state and len(btc_state["ohlcv"]) >= 3:
        btc_ohlcv = btc_state["ohlcv"]
        btc_closes = [float(x[4]) for x in btc_ohlcv[-3:]]
        
        # 檢測大盤是否在急跌 (跌幅 > 0.45%)。
        btc_drop_pct = (btc_closes[-1] - btc_closes[-3]) / btc_closes[-3]
        btc_dumping = btc_drop_pct < -0.0045
        
        # 檢測大盤是否在急拉 (漲幅 > 0.45%)。
        btc_pump_pct = (btc_closes[-1] - btc_closes[-3]) / btc_closes[-3]
        btc_pumping = btc_pump_pct > 0.0045
        
        # 取得大盤 1H MACD 與歷史動能
        btc_macd_hist = btc_state.get("macd_hist", 0.0)
        btc_prev_macd_hist = btc_state.get("prev_macd_hist", 0.0)
        btc_climbing = btc_macd_hist > btc_prev_macd_hist # 大盤動能向上
        
        if side == "buy":
            if btc_dumping:
                logger.info(f"⚠️ [BTC 衝擊參考] 大盤急跌中 (跌幅: {btc_drop_pct*100:.2f}%)，但允許小幣獨立做多")
            if btc_macd_hist < 0 and not btc_climbing:
                logger.info(f"⚠️ [大盤共振參考] BTC 1H MACD 處於空頭動能擴張期，但允許小幣獨立做多")
            
        if side == "sell":
            if btc_pumping:
                logger.info(f"⚠️ [BTC 衝擊參考] 大盤急拉中 (漲幅: {btc_pump_pct*100:.2f}%)，但允許小幣獨立做空")
            if btc_macd_hist > 0 and btc_climbing:
                logger.info(f"⚠️ [大盤共振參考] BTC 1H MACD 處於多頭動能擴張期，但允許小幣獨立做空")

    # ── [2026-07-14 修正] 1m 短線順向確認 ──
    # 只讀倒數第二根已收線 K 棒，避免當前 K 棒在同一分鐘內翻紅/翻黑而反覆否決訊號。
    # 單根反色只能攔截一般訊號；完整共振且 >=25 分的 Route A 不應被這個單一雜訊否決。
    # 豁免反轉與反手路由，因為這些策略本質就是摸底/接針/變盤反手。
    if route not in ("Exhaustion_Entry", "Extreme_Reversal"):
        if not is_last_closed_1m_aligned(s, side):
            # 放寬：豁免強度從 25 降至 18（與 BTC_MOMENTUM_OVERRIDE_STRENGTH 對齊）
            if has_strong_local_momentum_override(route, strength) or strength >= 18.0:
                logger.info(
                    f"⚡ [SHORT_TERM_OVERRIDE] {sym} Route A {side} 強度 {strength:.1f} >= 18，"
                    "略過單根已收線 1m 反色 K 棒"
                )
            else:
                candle_color = "陰線" if side == "buy" else "陽線"
                logger.info(
                    f"🛑 [短線順向過濾] {sym} 上一根已收線 1m K 線為{candle_color}，拒絕逆向"
                    f"{'做多' if side == 'buy' else '做空'}"
                )
                return False

    # 若幣種被標記為完全禁入場，直接拒絕（管理員策略）
    if COIN_PROFILE_CONFIG.get(sym, {}).get("disable_entry", False):
        logger.info(f"🛑 [DISABLED_ENTRY] {sym} 已被設定為禁入場，拒絕所有進場信號")
        return False

    # ── 驟跌/驟漲保護：暫停新進場（不接落下的刀子）──
    # 小幣種有時會短時間劇烈崩跌或暴衝，跟出場端「急速逆勢」用同一套 2.0倍ATR
    # 標準判斷，但這裡要判斷方向：只有「逆著進場方向」的劇烈變動才算危險
    # （買單遇到剛崩跌、空單遇到剛暴衝），不能用不分方向的高低區間，否則會把
    # 正常的強勢單邊行情（訊號本來就該追的趨勢）也誤判成崩盤而擋掉。
    # 偵測到就暫停這個幣種的新進場 15 分鐘；冷卻期間再次偵測到會持續延長，
    # 直到波動真的緩和下來才恢復——不是接刀子進場，而是等塵埃落定。
    # 反手類路線（Automatic_Reverse 已在上面提早 return True、Extreme_Reversal/
    # Exhaustion_Entry）不受此限制，因為這些本來就是設計來承接劇烈變盤的。
    if route not in ("Extreme_Reversal", "Exhaustion_Entry"):
        _ohlcv = s.get("ohlcv", [])
        _crash_atr = s.get("current_atr", 0.0)
        if len(_ohlcv) >= 3 and _crash_atr > 0:
            _ref_close = _ohlcv[-3][4]
            _cur_close = s.get("close_price", _ref_close)
            _net_move = _cur_close - _ref_close
            _adverse_mult = (-_net_move / _crash_atr) if side == "buy" else (_net_move / _crash_atr)
            if _adverse_mult >= 2.0:
                s["crash_cooldown_until"] = time.time() + 900
                logger.info(f"⚡ [驟跌保護] {sym} 近期價格逆勢達 {_adverse_mult:.2f}倍ATR，暫停{'多' if side=='buy' else '空'}單進場15分鐘")

        if s.get("crash_cooldown_until", 0) > time.time():
            remaining = s["crash_cooldown_until"] - time.time()
            logger.info(f"🛑 [驟跌保護] {sym} 仍在劇烈逆勢冷卻期，暫停進場（剩餘 {remaining:.0f} 秒）")
            return False

    # =========================================================================
    # ✨ NEW STAGE 0.5: SUPPORT/RESISTANCE ZONE CONFIRMATION (支撑/阻力位確認)
    # 只在有明確支撑/阻力的位置入場，避免買在相對高點
    # =========================================================================
    bb_lower = s.get("bb_low", 0.0)
    bb_upper = s.get("bb_up", 0.0)
    bb_middle = (bb_lower + bb_upper) / 2 if (bb_lower > 0 and bb_upper > 0) else 0.0
    
    # 階梯式放寬位置門檻：
    # - 極強訊號 (>=22) 放寬至 55% (容許窄幅行情下的突破)
    # - 強訊號 (>=18) 放寬至 45% 
    # - 普通訊號維持 25% (極嚴格防禦)
    if strength >= 22.0:
        zone_ratio = 0.55
    elif strength >= 18.0:
        zone_ratio = 0.45
    else:
        zone_ratio = 0.25

    if side == "buy" and bb_lower > 0:
        coin_cfg = COIN_PROFILE_CONFIG.get(sym, {})
        tol = coin_cfg.get("support_zone_tolerance_pct", 0.015)
        strength_threshold = coin_cfg.get("support_zone_strength_threshold", 12.0)

        _band_width = bb_upper - bb_lower if bb_upper > bb_lower else 0.0
        support_zone_upper = bb_lower + _band_width * zone_ratio if _band_width > 0 else bb_lower * (1 + tol)
        is_in_support_zone = cp <= support_zone_upper
        if strength > 40:
            is_in_support_zone = True

        if not is_in_support_zone:
            _band_position = (cp - bb_lower) / _band_width if _band_width > 0 else 1.0
            _route_a_trend_override = (
                route == "a" and strength >= 17.0
                and _band_position <= 0.65
                and s.get("current_rsi", 50.0) <= 55.0
                and s.get("macd_line", 0.0) > s.get("macd_signal", 0.0)
            )
            _route_b_momentum_override = (
                route == "b" and strength >= 30.0
                and s.get("current_rsi", 50.0) <= 55.0
            )
            if _route_a_trend_override:
                logger.info(f"⚡ [SUPPORT_ZONE_TREND_OVERRIDE] {sym} Route A 強勢做多 ({strength:.1f})，位於布林帶 {_band_position*100:.0f}% 且 MACD 已確認，允許順勢進場")
            elif _route_b_momentum_override:
                logger.info(f"⚡ [SUPPORT_ZONE_OVERRIDE] {sym} Route B 強勢做多 ({strength:.1f}) 且 RSI {s.get('current_rsi', 50.0):.1f} 未過熱，允許突破進場")
            elif strength >= 18.0:
                # ───【限價掛單機制：多單】───
                # 當強訊號現價高於支撐區時，轉化為 pullback 限價掛單。
                # 低波動時掛在支撐區的中間點，普通波動掛在支撐上限。
                s["force_pullback_entry"] = True
                atr_history_v = s.get("atr_history", [])
                atr_24h_avg_v = float(np.mean(atr_history_v)) if len(atr_history_v) > 0 else 0.0
                current_atr_v = s.get("current_atr", 0.0)
                is_low_vol = (atr_24h_avg_v > 0 and current_atr_v <= atr_24h_avg_v)
                support_zone_middle = bb_lower + _band_width * (zone_ratio / 2.0) if _band_width > 0 else bb_lower * (1 + tol / 2.0)
                s["close_price"] = support_zone_middle if (is_low_vol and bb_lower > 0) else support_zone_upper
                logger.info(f"🧲 [SUPPORT_ZONE_LIMIT_CONVERT] {sym} 強度 {strength:.1f}，現價 {cp:.6f}，限價單掛在 {s['close_price']:.6f} ({'支撐區中間點' if is_low_vol else '支撐上限'})")
                is_in_support_zone = True
            else:
                distance_to_support = (cp - bb_lower) / bb_lower if bb_lower > 0 else 0
                logger.info(f"🛑 [SUPPORT_ZONE] {sym} 買入價 {cp:.6f} 不在布林帶下方{zone_ratio*100:.0f}%支撐區 (需 <= {support_zone_upper:.6f}；距下軌 {distance_to_support*100:.2f}%)，拒絕趨勢多單")
                return False

        if is_in_support_zone:
            logger.info(f"✅ [SUPPORT_ZONE] {sym} 買入價在支撑區 [{bb_lower:.6f} ~ {support_zone_upper:.6f}]，有支撑，允許進場")
    
    if side == "sell" and bb_upper > 0:
        coin_cfg = COIN_PROFILE_CONFIG.get(sym, {})
        tol = coin_cfg.get("support_zone_tolerance_pct", 0.015)
        strength_threshold = coin_cfg.get("support_zone_strength_threshold", 12.0)

        _band_width = bb_upper - bb_lower if bb_upper > bb_lower else 0.0
        resistance_zone_lower = bb_upper - _band_width * zone_ratio if _band_width > 0 else bb_upper * (1 - tol)
        is_in_resistance_zone = cp >= resistance_zone_lower
        if strength > 40:
            is_in_resistance_zone = True

        if not is_in_resistance_zone:
            _band_position = (cp - bb_lower) / _band_width if _band_width > 0 else 0.0
            _route_a_trend_override = (
                route == "a" and strength >= 17.0
                and _band_position >= 0.35
                and s.get("current_rsi", 50.0) >= 45.0
                and s.get("macd_line", 0.0) < s.get("macd_signal", 0.0)
            )
            _route_b_momentum_override = (
                route == "b" and strength >= 30.0
                and s.get("current_rsi", 50.0) >= 45.0
            )
            if _route_a_trend_override:
                logger.info(f"⚡ [RESISTANCE_ZONE_TREND_OVERRIDE] {sym} Route A 強勢做空 ({strength:.1f})，位於布林帶 {_band_position*100:.0f}% 且 MACD 已確認，允許順勢進場")
            elif _route_b_momentum_override:
                logger.info(f"⚡ [RESISTANCE_ZONE_OVERRIDE] {sym} Route B 強勢做空 ({strength:.1f}) 且 RSI {s.get('current_rsi', 50.0):.1f} 未超賣，允許跌破進場")
            elif strength >= 18.0:
                # ───【限價掛單機制：空單】───
                # 當強訊號現價低於阻力區時，轉化為 pullback 限價掛單。
                # 低波動時掛在阻力區的中間點，普通波動掛在阻力下限。
                s["force_pullback_entry"] = True
                atr_history_v = s.get("atr_history", [])
                atr_24h_avg_v = float(np.mean(atr_history_v)) if len(atr_history_v) > 0 else 0.0
                current_atr_v = s.get("current_atr", 0.0)
                is_low_vol = (atr_24h_avg_v > 0 and current_atr_v <= atr_24h_avg_v)
                resistance_zone_middle = bb_upper - _band_width * (zone_ratio / 2.0) if _band_width > 0 else bb_upper * (1 - tol / 2.0)
                s["close_price"] = resistance_zone_middle if (is_low_vol and bb_upper > 0) else resistance_zone_lower
                logger.info(f"🧲 [RESISTANCE_ZONE_LIMIT_CONVERT] {sym} 強度 {strength:.1f}，現價 {cp:.6f}，限價單掛在 {s['close_price']:.6f} ({'阻力區中間點' if is_low_vol else '阻力下限'})")
                is_in_resistance_zone = True
            else:
                distance_to_resistance = (bb_upper - cp) / bb_upper if bb_upper > 0 else 0
                logger.info(f"🛑 [RESISTANCE_ZONE] {sym} 賣出價 {cp:.6f} 不在布林帶上方{zone_ratio*100:.0f}%阻力區 (需 >= {resistance_zone_lower:.6f}；距上軌 {distance_to_resistance*100:.2f}%)，拒絕趨勢空單")
                return False

        if is_in_resistance_zone:
            logger.info(f"✅ [RESISTANCE_ZONE] {sym} 賣出價在阻力區 [{resistance_zone_lower:.6f} ~ {bb_upper:.6f}]，有阻力，允許進場")

    # =========================================================================
    # 🔴 STAGE 0: MACRO & STAGE 0.1: BULL DEFENSE (已註銷：短線以 1m 為主，不看宏觀面)
    # =========================================================================


    # =========================================================================
    # 🛑 STAGE 1: HARD GATES (硬門檻 - 不通過直接攔截)
    # =========================================================================
    # 1. 動態量能門檻過濾 (Adaptive Volume Gate)
    _ohlcv_v = s.get("ohlcv", [])
    current_volume = _ohlcv_v[-2][5] if len(_ohlcv_v) > 1 else (_ohlcv_v[-1][5] if _ohlcv_v else 0)
    volume_ma20 = s.get("vol_ma20", 0.0)
    atr_history_v = s.get("atr_history", [])
    atr_24h_avg_v = float(np.mean(atr_history_v)) if len(atr_history_v) > 0 else 0.0
    current_atr_v = s.get("current_atr", 0.0)
    is_low_vol_mode = (atr_24h_avg_v > 0 and current_atr_v <= atr_24h_avg_v)
    vol_multiplier = (0.15 if is_low_vol_mode else 0.2)
    dynamic_vol_threshold = volume_ma20 * vol_multiplier
    if current_volume <= dynamic_vol_threshold:
        mode_label = "低波動放寬模式 30%" if is_low_vol_mode else "高波動放寬 40%"
        if route in ("Extreme_Reversal", "Exhaustion_Entry"):
            # Exhaustion_Entry 仍需最低 5% 均量，避免完全沒人的行情反手
            _min_vol_floor = volume_ma20 * 0.05
            if route == "Exhaustion_Entry" and current_volume < _min_vol_floor:
                logger.info(f"🛑 [EXHAUSTION_NO_VOL] {sym} Exhaustion_Entry 量能太低 (當前: {current_volume:.1f} < 均量5%: {_min_vol_floor:.1f})，完全死水拒絕反手")
                return False
            logger.info(f"⚡ [ALLOW] [Filter:Volume] {sym} {route} 路由豁免死水量能攔截 (當前: {current_volume:.1f} | 門檻: {dynamic_vol_threshold:.1f} | {mode_label})")
        else:
            logger.info(f"🛑 [REJECT] [Filter:Volume] {sym} 量能嚴重不足 (當前: {current_volume:.1f} <= 門檻: {dynamic_vol_threshold:.1f} | {mode_label})，判定為死水行情。")
            return False

    # --- ATR 收縮 + BB 過伸過濾 (Volatility Shrink Filter) ---
    # 最後一棒特徵：價格已到 BB 極端 + 近期波動在縮小 → 動能快耗盡
    _atr_hist_vs = s.get("atr_history", [])
    if len(_atr_hist_vs) >= 10:
        _atr_recent = float(np.mean(_atr_hist_vs[-5:]))
        _atr_prev   = float(np.mean(_atr_hist_vs[-10:-5]))
        _atr_shrinking = _atr_prev > 0 and _atr_recent < _atr_prev * 0.80
        if _atr_shrinking:
            _bb_upper_vs = s.get("bb_upper", 0.0)
            _bb_lower_vs = s.get("bb_lower", 0.0)
            _near_bb_top = _bb_upper_vs > 0 and cp > _bb_upper_vs * 0.997
            _near_bb_bot = _bb_lower_vs > 0 and cp < _bb_lower_vs * 1.003
            _is_last_push = (side == "buy" and _near_bb_top) or (side == "sell" and _near_bb_bot)
            if _is_last_push and strength < 25.0 and route not in ("Extreme_Reversal",):
                logger.info(f"🛑 [ATR_SHRINK] {sym} ATR收縮({_atr_recent:.5f} < {_atr_prev:.5f}×0.80) 且貼近BB極端，疑似最後一棒，拒絕進場")
                return False

    # 2. 空單 RSI 極限保護：RSI > 75 才允許做空（超買區），RSI 太低反而不能追空
    current_rsi = s.get("current_rsi", 50.0)
    if side == 'sell' and current_rsi < 25.0:
        logger.info(f"🛑 [REJECT] [Filter:RSI_Limit] {sym} 觸發RSI極限保護 (RSI: {current_rsi:.1f} < 25.0)，拒絕在極端超賣區追空。")
        return False

    # 3. 15m 跨時框趨勢對齊 (Multi-Timeframe Alignment)
    # 原本只是 WARNING，高強度訊號可完全繞過 → 改為硬性封鎖，需 RSI 確認超買/超賣才允許逆勢
    ema20_15m = s.get("ema20_15m", 0.0)
    ema50_15m = s.get("ema50_15m", 0.0)
    current_rsi_mtf = s.get("current_rsi", 50.0)
    # 分數高不代表方向正確；逆 15m 趨勢不得再以強度分數跳過。
    # 使用者要求放寬：超買/超賣豁免門檻從 60/40 收窄到 55/45，讓 RSI 已經明顯偏向
    # 反轉方向（但還沒到傳統超買/超賣 60/40）的訊號也能突破 15m 趨勢封鎖，仍保留
    # 中性 RSI（45~55）時不逆勢的保護，不是整道過濾器失效。
    # 使用者進一步指示：只放寬空單這邊（跟 BTC 4H+1H 熊市大方向一致，風險比多單
    # 逆勢低），賣出門檻再收到 50（中性線）；多單維持 45 不動，因為多單本來就是在
    # 逆著目前 BTC 熊市大方向，不應該一起放寬。
    # 使用者要求放寬：逆勢做空的 RSI 門檻降至 42.0 (原 50.0)，增加窄幅波動中的限價掛單機會
    _mtf_sell_rsi_override = 42.0
    _mtf_buy_rsi_override = 45.0
    if ema20_15m > 0 and ema50_15m > 0 and route not in ("Extreme_Reversal", "Exhaustion_Entry"):
        if side == 'sell' and ema20_15m > ema50_15m:
            if current_rsi_mtf >= _mtf_sell_rsi_override:
                logger.info(f"⚠️ [WARN] [Filter:MTF_Trend] {sym} 15m 大趨勢向上，逆勢做空 — RSI {current_rsi_mtf:.1f} 已達 {_mtf_sell_rsi_override:.0f} 偏多門檻，允許")
            else:
                logger.info(f"🛑 [BLOCK] [Filter:MTF_Trend] {sym} 15m 大趨勢向上，逆勢做空 且 RSI {current_rsi_mtf:.1f} < {_mtf_sell_rsi_override:.0f}，拒絕")
                return False
        elif side == 'buy' and ema20_15m < ema50_15m:
            if current_rsi_mtf <= _mtf_buy_rsi_override:
                logger.info(f"⚠️ [WARN] [Filter:MTF_Trend] {sym} 15m 大趨勢向下，逆勢做多 — RSI {current_rsi_mtf:.1f} 已達 {_mtf_buy_rsi_override:.0f} 偏空門檻，允許")
            else:
                logger.info(f"🛑 [BLOCK] [Filter:MTF_Trend] {sym} 15m 大趨勢向下，逆勢做多 且 RSI {current_rsi_mtf:.1f} > {_mtf_buy_rsi_override:.0f}，拒絕")
                return False

    # 4. Pre-Entry Quality Filter：實體 + 量能同步爆發（過濾弱訊號假突破）
    _ohlcv_q = s.get("ohlcv", [])
    if len(_ohlcv_q) >= 21:
        # 使用前20根已收盤 K 線計算平均實體大小
        closed_candles = _ohlcv_q[-21:-1]
        avg_body_size = float(np.mean([abs(c[4] - c[1]) for c in closed_candles]))
        # 評估已收盤的訊號 K 線 (ohlcv[-2])
        signal_candle = _ohlcv_q[-2]
        current_body_size = abs(signal_candle[4] - signal_candle[1])
        eval_vol = signal_candle[5]
        vol_ma20_q = s.get("vol_ma20", 0.0)
        if avg_body_size > 0 and vol_ma20_q > 0:
            # 嚴格 AND 條件：實體 > 1.3x 均值 且 量能 > 1.4x 均量
            if current_body_size <= avg_body_size * 0.8 or eval_vol <= vol_ma20_q * 1.0:
                if is_relaxed or strength >= 20.0 or route in ("Exhaustion_Entry", "Automatic_Reverse", "Extreme_Reversal"):
                    logger.info(f"⚡ [ALLOW] [Filter:Quality] {sym} 強勢({strength:.1f})、特殊路由或寬鬆模式，豁免實體/量能嚴格門檻")
                else:
                    logger.info(f"🛑 [WEAK_SIGNAL_SKIP] {sym} 訊號缺乏爆發力 (實體: {current_body_size/avg_body_size:.2f}x | 量能: {eval_vol/vol_ma20_q:.2f}x)，拒絕進場")
                    return False

    # 5. 收盤確認 (Candle Close Check)
    if not is_relaxed and route not in ("Extreme_Reversal",) and len(s["ohlcv"]) >= 2:
        prev_close = s["ohlcv"][-2][4]
        open_price = s["ohlcv"][-1][1]
        close_price = s["ohlcv"][-1][4]
        if side == 'buy' and not (close_price > prev_close or close_price > open_price):
            logger.info(f"🛑 [REJECT] [Filter:Candle_Close] {sym} 收盤未確認 (當前收盤: {close_price:.4f} <= 前收: {prev_close:.4f} 且 <= 開盤: {open_price:.4f})。")
            return False
        elif side == 'sell' and not (close_price < prev_close or close_price < open_price):
            logger.info(f"🛑 [REJECT] [Filter:Candle_Close] {sym} 收盤未確認 (當前收盤: {close_price:.4f} >= 前收: {prev_close:.4f} 且 >= 開盤: {open_price:.4f})。")
            return False

    # --- 趨勢斜率過濾 (Trend Slope Filter) ---
    # EMA20 斜率 < 0.05% (3根K線) → 平台盤整，拒絕上車
    ema20_now = s.get("ema20", 0.0)
    ema20_hist = s.get("ema20_history", [])
    if ema20_now > 0 and len(ema20_hist) >= 3:
        ema20_past = ema20_hist[-3]
        slope_pct = (ema20_now - ema20_past) / ema20_past if ema20_past > 0 else 0
        slope_threshold = 0.0005
        _slope_exempt = strength >= 20.0 or route in ("Exhaustion_Entry", "Automatic_Reverse", "Extreme_Reversal")
        if side == "buy" and slope_pct < slope_threshold and not _slope_exempt:
            logger.info(f"🛑 [WEAK_SLOPE_SKIP] {sym} 做多但 EMA20 斜率太平緩 ({slope_pct*100:.4f}% < {slope_threshold*100:.4f}%)，拒絕上車")
            return False
        elif side == "sell" and slope_pct > -slope_threshold and not _slope_exempt:
            logger.info(f"🛑 [WEAK_SLOPE_SKIP] {sym} 做空但 EMA20 斜率太平緩 ({slope_pct*100:.4f}% > -{slope_threshold*100:.4f}%)，拒絕上車")
            return False

    # [新增] MTF Correlation Lock (4H)
    upper_4h = s.get("bb_upper_4h")
    lower_4h = s.get("bb_lower_4h")
    atr = s.get("current_atr", 0.0)
    if upper_4h is not None and lower_4h is not None and atr > 0:
        if side == 'buy' and (upper_4h - cp) < atr * 0.2:
            logger.info(f"🛑 觸發 [MTF 4H 強壓力位] {sym} 現價 {cp} 貼近 4H 布林上軌 {upper_4h:.4f} (<0.2*ATR)，禁止多單追高")
            return False
        if side == 'sell' and (cp - lower_4h) < atr * 0.2:
            logger.info(f"🛑 觸發 [MTF 4H 強壓力位] {sym} 現價 {cp} 貼近 4H 布林下軌 {lower_4h:.4f} (<0.2*ATR)，禁止空單地板空")
            return False

    is_trend = route == "a"
    if side == 'buy' and not ctx.MARKET_WIND.get("allow_long", True) and is_trend:
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤瀑布風控] 大盤異常跌勢，禁止開多")
        return False
    if side == 'sell' and not ctx.MARKET_WIND.get("allow_short", True) and is_trend:
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤上漲風控] 大盤異常漲勢，禁止開空")
        return False

    # --- [BTC 1H 趨勢大盤過濾] ---
    btc_1h = ctx.MARKET_WIND.get("btc_trend_1h")
    if is_trend and btc_1h is not None:
        if side == 'buy' and btc_1h == "BEAR":
            logger.info(f"⚠️ [BTC 1H 大盤過濾] BTC 1H 確認為熊市跌勢，但已依指示放寬，允許小幣逆勢做多")

    # --- [過熱噴發過濾 (Moving Average Deviation Filter)] ---
    # 升級版：分層門檻 + 適用範圍擴大到所有路由
    # 核心邏輯：價格離 EMA20 越遠 = 越容易在進場後立即回撤觸發 SL
    ema20 = s.get("ema20", 0.0)
    if ema20 > 0:
        ema_dev = (cp - ema20) / ema20  # 正 = 在 EMA 上方，負 = 下方
        # 門檻：Extreme_Reversal/Exhaustion_Entry 允許 4%，強訊號(≥24)允許 3%，普通路由 2.5%（原1.5%，已放寬）
        if route in ("Extreme_Reversal", "Exhaustion_Entry"):
            _ema_hard_limit = 0.04
        elif strength >= 24.0:
            _ema_hard_limit = 0.03   # 極強訊號豁免：動能幣偏離高達 3% 仍允許
        else:
            _ema_hard_limit = 0.025  # 放寬：原 1.5%，改為 2.5%，減少過熱誤判
        if side == "buy" and ema_dev > _ema_hard_limit:
            logger.info(f"🛑 {sym} 觸發 [EMA過熱過濾] 多單但現價超過 EMA20 {ema_dev*100:.1f}% (> {_ema_hard_limit*100:.1f}%)，過熱噴發，等回測")
            return False
        if side == "sell" and ema_dev < -_ema_hard_limit:
            logger.info(f"🛑 {sym} 觸發 [EMA過熱過濾] 空單但現價低於 EMA20 {abs(ema_dev)*100:.1f}% (> {_ema_hard_limit*100:.1f}%)，過熱下挫，等回測")
            return False
    if is_trend:
        pass  # is_trend 已由上方統一的 EMA 距離過濾處理，不需重複

    # --- [15m EMA 趨勢過濾] (已註銷：短線以 1m 為主，不看 15m 大時框趨勢)
    # --- [BTC 4H 趨勢過濾] (已註銷：短線以 1m 為主，不看 4H 宏觀面)


    _short_history_exempt = route in ("Extreme_Reversal", "Exhaustion_Entry", "Automatic_Reverse")
    if len(s["ohlcv"]) < 20 and not _short_history_exempt:
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線不足] 當前長度 {len(s['ohlcv'])} < 20")
        return False

    # --- MTF 1H & 15m 趨勢過濾 (放寬為軟性警告) ---
    if s.get("mtf_filter", True):
        ema50_1h = s.get("ema50_1h", 0)
        sma200_15m = s.get("sma200_15m", 0)
        _countertrend_route = route in ("Extreme_Reversal", "Exhaustion_Entry", "Automatic_Reverse")

        if ema50_1h > 0:
            if side == 'buy' and cp <= ema50_1h and not _countertrend_route:
                logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [Filter:Trend_Mismatch] 1H大趨勢向下 (EMA50 {ema50_1h:.4f})，一般訊號不得以強度覆蓋，拒絕做多")
                return False
            # 空單 MTF 1H EMA50 過濾：Exhaustion_Entry 不受限（反轉策略）
            if side == 'sell' and not _countertrend_route:
                if cp >= ema50_1h:
                    logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [Filter:Trend_Mismatch] 1H大趨勢向上 (EMA50 {ema50_1h:.4f})，一般訊號不得以強度覆蓋，拒絕做空")
                    return False
                if sma200_15m > 0 and cp >= sma200_15m:
                    logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [Filter:Trend_Mismatch] 15m趨勢向上 (SMA200 {sma200_15m:.4f})，拒絕一般做空訊號")
                    return False

    # --- 盤整/低波動過濾 (Choppiness) ---
    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)

    bb_up = s.get("bb_up", 0.0)
    bb_down = s.get("bb_down", 0.0)
    bb_width_pct = (bb_up - bb_down) / cp if cp > 0 else 0

    if atr_24h_avg > 0 and current_atr < atr_24h_avg * 0.25:
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [波動率過濾] 當前 ATR 過小，處於極度盤整 (current={current_atr:.5f}, avg={atr_24h_avg:.5f})")
        return False
    if bb_width_pct > 0 and bb_width_pct < 0.0015:
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [波動率過濾] 布林帶極度收斂 (寬度={bb_width_pct*100:.2f}%)，避免洗盤")
        return False

    # --- [ATR 爆發閘門 (Volatility Spike Gate)] ---
    # 瞬時波動率 > 2.3× 歷史平均 → 市場正處於「閃崩/閃漲」狀態，SL 必然過寬，拒絕常規進場
    # 豁免：Exhaustion_Entry（耗竭反轉）與 Extreme_Reversal（極端反轉）本就在極端波動中操作
    # 另外，對於強訊號且 RSI 未過熱的強訊號的情況，放寬一次，避免高品質訊號被過度封鎖。
    # 使用者先前要求增加開倉次數，門檻從 2.0x/2.3x 放寬到 2.5x/2.8x；後來發現邊緣訊號
    # 進場後常常原地打轉，要求拉回一點，改成一般 2.3x、強訊號 2.8x（介於原始與寬鬆之間）。
    _atr_spike_exempt = route in ("Exhaustion_Entry", "Extreme_Reversal")
    _atr_spike_ratio = current_atr / atr_24h_avg if atr_24h_avg > 0 else 0.0
    # RSI 偏熱時不給強訊號豁免：ATR 瞬間放大時如果 RSI 又已經在追價方向的極端
    # （做多時 RSI>=70、做空時 RSI<=30），代表這筆是在「已經漲/跌過頭+瞬間又暴衝」
    # 雙重疊加的最壞組合下進場，即使訊號強度夠高也容易一進場就被巴（實測 ETHUSDT
    # 強度30.55、RSI71.9、ATR 2.37x 放行進場，13分鐘後停損出場）。強訊號的豁免只
    # 保留給 RSI 還在正常範圍、單純是波動放大的情況。
    _rsi_now = s.get("current_rsi", 50.0)
    _rsi_hot = (side == 'buy' and _rsi_now >= 70.0) or (side == 'sell' and _rsi_now <= 30.0)
    _allow_mild_atr_spike = (strength >= 24.0) and (atr_24h_avg > 0) and (_atr_spike_ratio <= 2.8) and not _rsi_hot
    if not _atr_spike_exempt and atr_24h_avg > 0 and current_atr > atr_24h_avg * 2.3:
        if _allow_mild_atr_spike:
            logger.info(f"⚡ [ALLOW] [ATR爆發閘門] {sym} 強勢({strength:.1f}) 且 ATR 輕微爆發 ({_atr_spike_ratio:.2f}x)，RSI 未過熱 ({_rsi_now:.1f})，放寬進場")
        else:
            _hot_note = f"，且 RSI 已過熱 ({_rsi_now:.1f})" if _rsi_hot else ""
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [ATR爆發閘門] 當前 ATR ({current_atr:.5f}) > 歷史平均 2.3x ({atr_24h_avg*2.3:.5f})，市場閃崩/閃漲中{_hot_note}，拒絕進場防止滑點掃損")
            return False

    # --- 極端動能反轉確認（所有路由適用）---
    # 反轉路由可以逆大趨勢，但不能在 RSI 極端且 MACD/價格尚未翻向時直接接刀。
    _macd_line = float(s.get("macd_line", 0.0) or 0.0)
    _macd_signal = float(s.get("macd_signal", 0.0) or 0.0)
    _recent_closes = [float(c[4]) for c in s.get("ohlcv", [])[-2:]]
    _price_turn_up = len(_recent_closes) >= 2 and _recent_closes[-1] > _recent_closes[-2]
    _price_turn_down = len(_recent_closes) >= 2 and _recent_closes[-1] < _recent_closes[-2]
    if side == "buy" and _rsi_now <= 35.0:
        if _macd_line <= _macd_signal or not _price_turn_up:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [ExtremeMomentumConfirm] RSI={_rsi_now:.1f} 極弱，MACD/價格尚未同步止跌，拒絕接刀做多")
            return False
    elif side == "sell" and _rsi_now >= 65.0:
        if _macd_line >= _macd_signal or not _price_turn_down:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [ExtremeMomentumConfirm] RSI={_rsi_now:.1f} 極強，MACD/價格尚未同步止漲，拒絕摸頭做空")
            return False
    if route not in ("Extreme_Reversal", "Exhaustion_Entry", "Automatic_Reverse") and not is_entry_pin_safe(sym, side):
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 反向長影線/方向未確認")
        return False

    # --- 同向虧損冷卻（全強度適用，不受訊號強度限制）---
    # 原實作埋在 strength > 15 分支裡，導致弱訊號完全繞過 4 小時冷卻保護
    if route not in ("Exhaustion_Entry", "Extreme_Reversal", "Automatic_Reverse"):
        _last_loss = s.get("last_loss_time_short", 0) if side == "sell" else s.get("last_loss_time_long", 0)
        _cooldown_elapsed = time.time() - _last_loss
        if _cooldown_elapsed < 60:  # 1 分鐘（放寬便於驗證）
            _remaining = (60 - _cooldown_elapsed) / 60
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [同向虧損冷卻] 同向({side})虧損後冷卻剩餘 {_remaining:.1f} 分鐘，攔截")
            return False

    # 量能確認過濾器 (衰竭進場策略 Exhaustion_Entry 允許低量能)
    if route != "Exhaustion_Entry" and strength <= 15.0 and not is_entry_volume_confirmed(sym, side):
        return False
    elif route != "Exhaustion_Entry" and strength > 15.0:
        # 強勢訊號只保留最低限度的量能要求 (5% 均量)
        if s["current_vol"] < s["vol_ma20"] * 0.05:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 強勢訊號但量能極度枯竭 (當前 {s['current_vol']:.0f} < 均量 5%)，攔截")
            return False

        # 加入「量能背離」過濾 (強度 15~20 適用，>20 豁免，Extreme_Reversal 永遠豁免)
        if strength <= 20.0 and route != "Extreme_Reversal":
            if s["current_vol"] >= s["vol_ma20"] * 3.0:
                logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能背離過濾] 強勢訊號({strength:.1f})但當前量 ({s['current_vol']:.0f}) 過大 (>= 3.0x均量 {s['vol_ma20']*3.0:.0f})，視為趨勢延續，攔截")
                return False
        elif strength <= 20.0 and route == "Extreme_Reversal" and s["current_vol"] >= s["vol_ma20"] * 3.0:
            logger.info(f"@@COIN_DEBUG@@ ⚡ {sym} [Extreme_Reversal 豁免] 高量={s['current_vol']:.0f} (>= 1.5x均量) 視為量能高潮，反轉確認加分！")

        # 價格結構確認 (Price Action Confirmation)，擴大至過去 3 根 K 線
        # 反轉 / 耗竭路由本來就以突破/回檔為主，不能再被普通趨勢結構門檻卡住
        _struct_exempt_routes = ("Extreme_Reversal", "Exhaustion_Entry", "Automatic_Reverse")
        if route in _struct_exempt_routes:
            logger.info(f"⚡ [ALLOW] [Filter:Structure] {sym} {route} 路由豁免結構過濾，允許反轉/耗竭進場")
        elif len(s["ohlcv"]) >= 2:
            current_close = s["ohlcv"][-1][4]
            lookback = min(3, len(s["ohlcv"]) - 1)
            past_candles = s["ohlcv"][-lookback-1:-1]
            past_highs = [c[2] for c in past_candles]
            past_lows = [c[3] for c in past_candles]
            avg_high = sum(past_highs) / len(past_highs)
            avg_low = sum(past_lows) / len(past_lows)
            # 容許 0.3% 誤差，避免強勢訊號只差一點點就被結構過濾擋下
            _struct_tolerance = 0.003

            if side == "sell":
                struct_ok = (current_close < avg_high * (1 + _struct_tolerance)) or (current_close < max(past_lows) * (1 + _struct_tolerance))
                if not struct_ok:
                    logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [結構過濾] 空單強勢({strength:.1f})但收盤價 ({current_close:.4f}) 未低於3K平均高點({avg_high:.4f})且未破任一低點({max(past_lows):.4f})，攔截")
                    return False
            if side == "buy":
                struct_ok = (current_close > avg_low * (1 - _struct_tolerance)) or (current_close > min(past_highs) * (1 - _struct_tolerance))
                if not struct_ok:
                    logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [結構過濾] 多單強勢({strength:.1f})但收盤價 ({current_close:.4f}) 未高於3K平均低點({avg_low:.4f})且未破任一高點({min(past_highs):.4f})，攔截")
                    return False

        # --- 新增：三道轉折防護機制 (High-Point Decay, RSI History, Cooldown) ---
        # 1. 同向虧損冷卻期 (Same-Side Cooldown)
        COOLDOWN_HOURS = 4
        COOLDOWN_SEC = COOLDOWN_HOURS * 3600
        now = time.time()

        last_loss_time = s.get("last_loss_time_short", 0) if side == "sell" else s.get("last_loss_time_long", 0)
        if now - last_loss_time < COOLDOWN_SEC:
            remaining_mins = (COOLDOWN_SEC - (now - last_loss_time)) / 60
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [同向虧損冷卻] 過去 4 小時內曾發生同向({side})虧損平倉，冷卻剩餘 {remaining_mins:.1f} 分鐘，攔截進場")
            return False

        # 2. 判斷是否為「逆勢轉折交易」
        sma200_15m = s.get("sma200_15m", 0)
        current_close = s.get("close_price", s["ohlcv"][-1][4]) if len(s.get("ohlcv", [])) >= 1 else 0.0
        is_counter_trend = False

        if route == "Extreme_Reversal":
            is_counter_trend = True
        else:
            if side == "sell":
                if sma200_15m > 0 and current_close > sma200_15m:
                    is_counter_trend = True
            else:  # buy
                if sma200_15m > 0 and current_close < sma200_15m:
                    is_counter_trend = True

        # 以下兩道「嚴格空間防禦」僅針對「逆勢轉折交易」開啟
        if is_counter_trend:
            # 3. 距離高低點衰減過濾 (High-Point Decay Filter)
            if len(s["ohlcv"]) >= 20:
                past_20 = s["ohlcv"][-20:]
                highest_20 = max([c[2] for c in past_20])
                lowest_20 = min([c[3] for c in past_20])
                COUNTER_TREND_MAX_DECAY_PCT = 0.025

                if side == "sell":
                    decay_pct = (highest_20 - current_close) / highest_20 if highest_20 > 0 else 0
                    if decay_pct > COUNTER_TREND_MAX_DECAY_PCT:
                        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [衰減過濾] 逆勢空單強勢({strength:.1f})但現價({current_close})距離20K高點({highest_20})已跌落 {decay_pct*100:.1f}% (> 2.5%)，視為半山腰追空，攔截")
                        return False
                else:
                    decay_pct = (current_close - lowest_20) / lowest_20 if lowest_20 > 0 else 0
                    if decay_pct > COUNTER_TREND_MAX_DECAY_PCT:
                        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [衰減過濾] 逆勢多單強勢({strength:.1f})但現價({current_close})距離20K低點({lowest_20})已反彈 {decay_pct*100:.1f}% (> 2.5%)，視為半山腰追多，攔截")
                        return False

            # 4. RSI 超買/超賣歷史確認 (RSI History Confirmation)
            # 反轉路由不應被此條件完全擋下，因為它們本來就可能在 RSI 尚未回撤時立即反轉
            if route not in ("Extreme_Reversal", "Exhaustion_Entry", "Automatic_Reverse") and "rsi_history" in s and len(s["rsi_history"]) > 0:
                recent_rsis = s["rsi_history"][-10:]
                if side == "sell":
                    highest_rsi = max(recent_rsis)
                    if highest_rsi < 45.0:
                        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [RSI歷史確認] 逆勢空單進場前，近 10 根 RSI 最高僅 {highest_rsi:.1f} (< 45.0)，未經歷過熱，視為逆勢空單假突破，攔截")
                        return False
                else:
                    lowest_rsi = min(recent_rsis)
                    if lowest_rsi > 55.0:
                        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [RSI歷史確認] 逆勢多單進場前，近 10 根 RSI 最低僅 {lowest_rsi:.1f} (> 55.0)，未見明顯回撤，視為逆勢多單假突破，攔截")
                        return False

    # 實盤最小量限制
    if route not in ("Exhaustion_Entry", "Extreme_Reversal"):
        min_volume = s["vol_ma20"] * 0.05
        if s["current_vol"] < min_volume:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [實盤最小量過濾] 當前 {s['current_vol']:.2f} < 均量 10% ({min_volume:.2f})")
            return False

    # =========================================================================
    # 🪙 STAGE 2 & 3: BONUS SYSTEM & EXECUTION THRESHOLD (加分系統與最終審查)
    # =========================================================================
    # 1. 基礎分 (Base Score)
    macd_hist, prev_macd_hist = _macd_vals(s)
    macd_line       = s.get("macd_line", 0.0)
    macd_signal     = s.get("macd_signal", 0.0)
    prev_macd_line  = s.get("prev_macd_line", 0.0)
    prev_macd_signal = s.get("prev_macd_signal", 0.0)
    current_rsi     = s.get("current_rsi", 50.0)

    macd_score = 0.0
    rsi_score = 0.0

    if side == 'buy':
        if prev_macd_line <= prev_macd_signal and macd_line > macd_signal:
            macd_score = 5.0
        elif macd_hist > 0 and macd_hist > prev_macd_hist:   # 需 MACD 為正才算多頭動能加分
            macd_score = 3.0
        if current_rsi > 48.0:
            rsi_score = 4.0
    else:
        if prev_macd_line >= prev_macd_signal and macd_line < macd_signal:
            macd_score = 5.0
        elif macd_hist < 0 and macd_hist < prev_macd_hist:   # 需 MACD 為負才算空頭動能加分
            macd_score = 3.0
        if current_rsi < 52.0:
            rsi_score = 4.0

    base_score = 7.0 + macd_score + rsi_score

    # 2. 加分項目 A (強勢訊號): signal_strength > 20.0, 給予 +5
    bonus_a = 0.0
    if strength > 20.0:
        bonus_a = 5.0

    # 3. 加分項目 B (量價協同)
    is_volume_price_aligned = False
    bonus_b = 0.0
    if len(s["ohlcv"]) >= 2:
        c_close = s["ohlcv"][-1][4]
        c_open = s["ohlcv"][-1][1]
        c_vol = s["ohlcv"][-1][5]
        p_vol = s["ohlcv"][-2][5]
        if side == 'buy':
            if c_close > c_open and c_vol > p_vol:
                is_volume_price_aligned = True
        elif side == 'sell':
            if c_close < c_open and c_vol > p_vol:
                is_volume_price_aligned = True

    if is_volume_price_aligned:
        bonus_b = 3.0

    total_score = base_score + bonus_a + bonus_b
    MIN_ENTRY_SCORE = 4.0  # 原 7.5，放寬讓更多硬條件已通過的訊號能實際成交

    if total_score < MIN_ENTRY_SCORE:
        logger.info(f"🛑 [REJECT] {sym}: 硬條件通過，但總分未達標 (綜合得分: {total_score:.1f} < 門檻: {MIN_ENTRY_SCORE:.1f})")
        return False

    logger.info(f"💚 [PASS] {sym}: 完美通過全套風控，准予開倉！(總得分: {total_score:.1f}, 基礎分: {base_score:.1f}, 加分A: {bonus_a:.1f}, 加分B: {bonus_b:.1f})")

    # --- 【進場方向一致性檢查】(Directional Consistency / Direction_Safety) ---
    # 確保進場方向與當前 K 線的收盤動態一致，防止在「反轉 K」上強行進場
    # 豁免：Extreme_Reversal / Exhaustion_Entry / Automatic_Reverse 本就逆勢操作，不受此限
    if route not in ("Extreme_Reversal", "Exhaustion_Entry", "Automatic_Reverse") and len(s.get("ohlcv", [])) >= 2:
        prev_close_dc = s["ohlcv"][-2][4]
        current_close_dc = s.get("close_price", s["ohlcv"][-1][4])

        if side == "buy" and current_close_dc < prev_close_dc:
            # 放寬：強度 >= 18 可豁免在下跌K線開多（原本完全封鎖無論強度）
            if strength >= 18.0:
                logger.info(f"⚡ [Direction_Safety] {sym} 多單在下跌K線，但強度 {strength:.1f} >= 18，豁免進場")
            else:
                logger.info(f"🛑 [Direction_Safety] {sym} 多單訊號但當前收盤 ({current_close_dc:.4f}) < 前收 ({prev_close_dc:.4f})，強度 {strength:.1f} < 18，K線仍在下跌，拒絕做多")
                return False
        elif side == "sell" and current_close_dc > prev_close_dc and strength < 18.0:  # 放寬：原 20.0 → 18.0
            logger.info(f"🛑 [Direction_Safety] {sym} 空單訊號但當前收盤 ({current_close_dc:.4f}) > 前收 ({prev_close_dc:.4f})，動能不足 (strength={strength:.1f} < 18.0)，拒絕進場")
            return False

    return True
