import time
import numpy as np

from core import ctx
from core.config import (PAPER_TRADING, SL_ATR_MULTIPLIER, TP_ATR_MULTIPLIER,
    COIN_PROFILE_CONFIG)
from core.indicators import _get_atr, _macd_vals, calculate_macd
from core.symbol_profile import get_effective_exit_setting, has_strong_momentum, get_dynamic_atr_multiplier, SYMBOL_PROFILES
from core.balance import is_daily_loss_halted, get_fee_overhead
from core.state_manager import get_open_position_count, get_active_count, is_symbol_locked


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

    # --- 【新增：量能過濾】 ---
    current_vol = s.get("current_vol", 0.0)
    vol_ma20 = s.get("vol_ma20", 0.0)
    # 如果當前成交量低於平均量的 70%，視為無量虛假波動，拒絕進場
    if vol_ma20 > 0 and current_vol < vol_ma20 * 0.7:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能過濾] 當前量({current_vol:.0f}) < 70%均量({vol_ma20*0.7:.0f})，拒絕進場")
        return False
    # --------------------------

    pin_threshold = 4.0
    candle_range = max(high - low, 1e-8)
    body_ratio = body / candle_range
    if body_ratio < 0.35 or s.get("current_vol", 0.0) < max(100.0, s.get("vol_ma20", 0.0) * 0.5):
        pin_threshold = 3.0
    ema20 = s.get("ema20", 0.0)
    if ema20 > 0:
        if side == 'buy' and close_price < ema20:
            pin_threshold = 3.0
        if side == 'sell' and close_price > ema20:
            pin_threshold = 3.0

    enabled = pin_threshold < 4.0

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
        pin_threshold = max(pin_threshold, 5.0)
        enabled = False

    if enabled:
        print(f"@@COIN_DEBUG@@ 🔧 {sym} 反插針門檻收緊為 {pin_threshold:.1f} (body_ratio={body_ratio:.2f}, vol={s.get('current_vol',0):.0f}, ema20={ema20:.4f}) [enabled]")
    else:
        if is_strong_macd:
            print(f"@@COIN_DEBUG@@ 🚀 {sym} MACD動能強勁，放寬反插針門檻至 {pin_threshold:.1f} [relaxed]")
        else:
            print(f"@@COIN_DEBUG@@ 🔎 {sym} 反插針門檻維持寬鬆 {pin_threshold:.1f} [disabled]")

    if side == 'buy':
        # 移除嚴格的 prev_close 比較，允許提早進場抄底
        if upper_wick > body * pin_threshold:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 上影線過長 (上影線 {upper_wick:.4f} > 實體 {body:.4f} * {pin_threshold:.1f})")
            return False
        return True

    if side == 'sell':
        # Doji 辨別：實體越小於 0.05% 價格時，用 ATR 絕對値替代比例判斷
        atr_val = s.get("current_atr", 0.0)
        min_body = max(atr_val * 0.05, close_price * 0.0005) if atr_val > 0 else close_price * 0.0005
        if body < min_body:
            # Doji 蠟燭，跳過影線過濾直接送出
            return True
        if lower_wick > body * pin_threshold:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 下影線過長 (下影線 {lower_wick:.4f} > 實體 {body:.4f} * {pin_threshold:.1f})")
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
        vol_factor = 0.15
        print(f"@@COIN_DEBUG@@ ⚡ {sym} 整體市場量縮，動態放寬量能門檻至 0.15x")
    else:
        # [新增] RSI 強勢放寬量能門檻 vs 極端狂熱提高門檻
        rsi = s.get("current_rsi", 50.0)
        if s.get("is_extreme_high_rsi", False):
            vol_factor = vol_factor * 1.2
            print(f"@@COIN_DEBUG@@ ⚠️ {sym} 觸發 [極值防禦] RSI ({rsi:.1f}) 處於狂熱頂點，強制提高量能門檻至 {vol_factor:.2f}x 防追高")
        elif rsi > 70 or rsi < 30:
            vol_factor = 0.15
            print(f"@@COIN_DEBUG@@ ⚡ {sym} 行情強勢 (RSI: {rsi:.1f})，動態放寬量能門檻至 0.15x")
        else:
            # [新增] 根據 ATR 高低自動動態調整倍數
            atr_24h_avg = s.get("atr_24h_avg", 0.0)
            current_atr = s.get("current_atr", 0.0)
            atr_ratio = (current_atr / atr_24h_avg) if atr_24h_avg > 0 else 1.0

            if atr_ratio >= 1.5:
                vol_factor = min(1.1, vol_factor)
                print(f"@@COIN_DEBUG@@ ⚡ {sym} 波動率極高 (ATR ratio: {atr_ratio:.2f})，動態降低量能門檻至 {vol_factor}x")
            elif atr_ratio >= 1.2:
                vol_factor = max(1.0, vol_factor - 0.2)
                print(f"@@COIN_DEBUG@@ ⚡ {sym} 波動率偏高 (ATR ratio: {atr_ratio:.2f})，微調量能門檻至 {vol_factor:.1f}x")

    min_volume = vol_ma20 * vol_factor
    if current_vol < min_volume:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能過濾] 當前量 {current_vol:.2f} < 門檻 {min_volume:.2f} (均量:{vol_ma20:.2f} * {vol_factor})")
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
    rr_threshold = s.get("rr_threshold", 1.3)
    if rr_ratio < rr_threshold:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [盈虧比過濾] 預計R:R ({rr_ratio:.2f}) < {rr_threshold} (TP: {tp_multiplier}x, SL: {sl_multiplier}x)")
        return False

    # --- 手續費最小獲利空間：預期利潤必須 > 來回手續費（以名義值比例換算）---
    if cp_rr > 0 and current_atr > 0:
        fee_overhead_pct = get_fee_overhead(float(leverage_rr))  # 保證金基準的來回費用
        expected_profit_pct = expected_profit / cp_rr            # 預期利潤佔現價的比例
        min_profit_pct = fee_overhead_pct * 2.0                  # 至少要賺到手續費的 2 倍才值得進
        if expected_profit_pct < min_profit_pct:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [手續費門檻] 預期利潤 {expected_profit_pct*100:.3f}% < 最低門檻 {min_profit_pct*100:.3f}% (ATR 太小，手續費吃掉獲利)")
            return False

    return True


def is_entry_pin_safe(sym, side):
    """
    插針過濾 (Pin-Bar Safety Check)
    檢查最新 K 線是否存在對方向不利的長影線（插針假突破）。
    - 多單：若上影線 > 實體 * 2.0，代表壓力強，拒絕做多。
    - 空單：若下影線 > 實體 * 2.0，代表支撐強，拒絕做空。
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

    if side == "buy" and len(s["ohlcv"]) >= 2 and c <= s["ohlcv"][-2][4] and upper_wick > body * 2.0:
        return False  # 上影線過長，壓力強 → 拒絕多單
    if side == "sell" and lower_wick > body * 2.0:
        return False  # 下影線過長，支撐強 → 拒絕空單
    return True


def is_entry_allowed(sym, side, route="a", strength=0.0):
    s = ctx.STATES[sym]
    cp = s["close_price"]

    if route == "Automatic_Reverse":
        print(f"@@COIN_DEBUG@@ ⚡ [反手豁免] {sym} 來自強勢反手，跳過空間/趨勢/大盤過濾")
        return True

    # =========================================================================
    # 🔴 STAGE 0: MACRO CIRCUIT BREAKER (宏觀熔斷機制)
    # BTC 4H + 1H 雙熊 → 啟動「熊市防禦模式」，封鎖所有做多訊號
    # 除非滿足「極端超賣 RSI < 32」或「底背離確認」
    # =========================================================================
    btc_4h = ctx.MARKET_WIND.get("btc_trend_4h")
    btc_1h = ctx.MARKET_WIND.get("btc_trend_1h")
    bear_defense_mode = (btc_4h == "BEAR" and btc_1h == "BEAR")
    if bear_defense_mode and side == 'buy':
        current_rsi_macro = s.get("current_rsi", 50.0)
        divergence_confirmed = (s.get("divergence", "none") == "bullish")
        extreme_oversold    = (current_rsi_macro < 32.0)
        if not extreme_oversold and not divergence_confirmed:
            print(f"🔴 [MACRO_BLOCK] {sym} 熊市防禦模式：BTC 4H+1H 雙熊，封鎖做多，允許做空。"
                  f"(RSI: {current_rsi_macro:.1f} >= 32 且 無底背離)")
            return False
        reason = "極端超賣" if extreme_oversold else "底背離確認"
        print(f"⚡ [MACRO_ALLOW] {sym} 熊市防禦模式下通過特赦：{reason}！(RSI: {current_rsi_macro:.1f}, Div: {s.get('divergence', 'none')})")
    # 熊市防禦模式下，做空方向完全放行（不封鎖）

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
            print(f"⚡ [ALLOW] [Filter:Volume] {sym} {route} 路由豁免死水量能攔截 (當前: {current_volume:.1f} | 門檻: {dynamic_vol_threshold:.1f} | {mode_label})")
        else:
            print(f"🛑 [REJECT] [Filter:Volume] {sym} 量能嚴重不足 (當前: {current_volume:.1f} <= 門檻: {dynamic_vol_threshold:.1f} | {mode_label})，判定為死水行情。")
            return False

    # 2. 空單 RSI 極限保護：RSI > 75 才允許做空（超買區），RSI 太低反而不能追空
    current_rsi = s.get("current_rsi", 50.0)
    if side == 'sell' and current_rsi < 25.0:
        print(f"🛑 [REJECT] [Filter:RSI_Limit] {sym} 觸發RSI極限保護 (RSI: {current_rsi:.1f} < 25.0)，拒絕在極端超賣區追空。")
        return False

    # 3. 15m 跨時框趨勢對齊 (Multi-Timeframe Alignment)
    ema20_15m = s.get("ema20_15m", 0.0)
    ema50_15m = s.get("ema50_15m", 0.0)
    if ema20_15m > 0 and ema50_15m > 0 and route not in ("Extreme_Reversal", "Exhaustion_Entry"):
        if side == 'sell' and ema20_15m > ema50_15m:
            print(f"⚠️ [WARN] [Filter:MTF_Trend] {sym} 15m 大趨勢向上，逆勢做空 — 由 RR/利潤門檻把關")
        elif side == 'buy' and ema20_15m < ema50_15m:
            print(f"⚠️ [WARN] [Filter:MTF_Trend] {sym} 15m 大趨勢向下，逆勢做多 — 由 RR/利潤門檻把關")

    # 4. 收盤確認 (Candle Close Check)
    if route not in ("Extreme_Reversal",) and len(s["ohlcv"]) >= 2:
        prev_close = s["ohlcv"][-2][4]
        open_price = s["ohlcv"][-1][1]
        close_price = s["ohlcv"][-1][4]
        if side == 'buy' and not (close_price > prev_close or close_price > open_price):
            print(f"🛑 [REJECT] [Filter:Candle_Close] {sym} 收盤未確認 (當前收盤: {close_price:.4f} <= 前收: {prev_close:.4f} 且 <= 開盤: {open_price:.4f})。")
            return False
        elif side == 'sell' and not (close_price < prev_close or close_price < open_price):
            print(f"🛑 [REJECT] [Filter:Candle_Close] {sym} 收盤未確認 (當前收盤: {close_price:.4f} >= 前收: {prev_close:.4f} 且 >= 開盤: {open_price:.4f})。")
            return False

    # [新增] MTF Correlation Lock (4H)
    upper_4h = s.get("bb_upper_4h")
    lower_4h = s.get("bb_lower_4h")
    atr = s.get("current_atr", 0.0)
    if upper_4h is not None and lower_4h is not None and atr > 0:
        if side == 'buy' and (upper_4h - cp) < atr * 0.2:
            print(f"🛑 觸發 [MTF 4H 強壓力位] {sym} 現價 {cp} 貼近 4H 布林上軌 {upper_4h:.4f} (<0.2*ATR)，禁止多單追高")
            return False
        if side == 'sell' and (cp - lower_4h) < atr * 0.2:
            print(f"🛑 觸發 [MTF 4H 強壓力位] {sym} 現價 {cp} 貼近 4H 布林下軌 {lower_4h:.4f} (<0.2*ATR)，禁止空單地板空")
            return False

    is_trend = route == "a"
    if side == 'buy' and not ctx.MARKET_WIND.get("allow_long", True) and is_trend:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤瀑布風控] 大盤異常跌勢，禁止開多")
        return False
    if side == 'sell' and not ctx.MARKET_WIND.get("allow_short", True) and is_trend:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤上漲風控] 大盤異常漲勢，禁止開空")
        return False

    # --- [BTC 1H 趨勢大盤過濾] ---
    btc_1h = ctx.MARKET_WIND.get("btc_trend_1h")
    if is_trend and btc_1h is not None:
        if side == 'buy' and btc_1h == "BEAR":
            print(f"⚠️ [BTC 1H 大盤過濾] BTC 1H 確認為熊市跌勢，但已依指示放寬，允許小幣逆勢做多")

    # --- [過熱噴發過濾 (Moving Average Deviation Filter)] ---
    if is_trend:
        ema20 = s.get("ema20", 0.0)
        if ema20 > 0:
            deviation = (cp - ema20) / ema20
            if strength <= 20.0:
                if side == "buy" and cp <= s["ohlcv"][-2][4] and deviation > 0.08:
                    print(f"🛑 {sym} 觸發 [過熱過濾] 順勢做多但價格偏離 EMA20 已達 {deviation*100:.2f}% (> 8%)，視為過熱噴發，拒絕進場防接刀")
                    return False
                if side == "sell" and deviation < -0.08:
                    print(f"🛑 {sym} 觸發 [過熱過濾] 順勢做空但價格偏離 EMA20 已達 {abs(deviation)*100:.2f}% (> 8%)，視為過熱下挫，拒絕進場防地板空")
                    return False

    # --- [15m EMA 趨勢過濾] ---
    if is_trend:
        if strength >= 10.0:
            pass  # 強勢 Override，跳過 15m EMA 過濾
        else:
            ema20_15m = s.get("ema20_15m", 0.0)
            if ema20_15m > 0:
                if side == 'buy' and cp < ema20_15m:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [15m EMA過濾] 5m 趨勢做多，但 15m EMA 向下 (現價 {cp:.4f} < 15m_EMA20 {ema20_15m:.4f})")
                    return False
                if side == 'sell' and cp > ema20_15m:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [15m EMA過濾] 5m 趨勢做空，但 15m EMA 向上 (現價 {cp:.4f} > 15m_EMA20 {ema20_15m:.4f})")
                    return False

    # --- [BTC 4H 趨勢過濾] 硬性方向限制，避免逆勢開倉 ---
    btc_4h = ctx.MARKET_WIND.get("btc_trend_4h")
    if is_trend and btc_4h is not None:
        _btc4h_override = 14.0
        if side == 'buy' and btc_4h == "BEAR":
            if strength >= _btc4h_override:
                print(f"@@COIN_DEBUG@@ ⚡ {sym} [4H逆勢覆蓋] 熊市但訊號強度 {strength:.1f} >= {_btc4h_override}，允許做多")
            else:
                print(f"@@COIN_DEBUG@@ 🛑 {sym} [4H大盤過濾] BTC 4H 熊市，禁止做多 (強度 {strength:.1f} < {_btc4h_override})")
                return False
        if side == 'sell' and btc_4h == "BULL":
            if strength >= _btc4h_override:
                print(f"@@COIN_DEBUG@@ ⚡ {sym} [4H逆勢覆蓋] 牛市但訊號強度 {strength:.1f} >= {_btc4h_override}，允許做空")
            else:
                print(f"@@COIN_DEBUG@@ 🛑 {sym} [4H大盤過濾] BTC 4H 牛市，禁止做空 (強度 {strength:.1f} < {_btc4h_override})")
                return False

    if len(s["ohlcv"]) < 20:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線不足] 當前長度 {len(s['ohlcv'])} < 20")
        return False

    # --- MTF 1H & 15m 趨勢過濾 (放寬為軟性警告) ---
    if s.get("mtf_filter", True):
        ema50_1h = s.get("ema50_1h", 0)
        sma200_15m = s.get("sma200_15m", 0)
        _mtf_override_threshold = 11.0

        if ema50_1h > 0:
            if side == 'buy' and cp <= ema50_1h:
                if strength >= _mtf_override_threshold:
                    print(f"@@COIN_DEBUG@@ ⚠️ {sym} [MTF警告放行] 1H大趨勢向下，但訊號強度 {strength:.1f} >= {_mtf_override_threshold}，強勢覆蓋趨勢過濾，允許進場")
                else:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [Filter:Trend_Mismatch] 1H大趨勢向下 (EMA50 {ema50_1h:.4f})，訊號強度 {strength:.1f} < {_mtf_override_threshold} 不足，拒絕進場")
                    return False
            # 空單 MTF 1H EMA50 過濾：Exhaustion_Entry 不受限（反轉策略）
            if side == 'sell' and route != "Exhaustion_Entry":
                if cp >= ema50_1h:
                    if strength >= _mtf_override_threshold:
                        print(f"@@COIN_DEBUG@@ ⚠️ {sym} [MTF警告放行] 1H大趨勢向上，但訊號強度 {strength:.1f} >= {_mtf_override_threshold}，允許進場")
                    else:
                        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [Filter:Trend_Mismatch] 1H大趨勢向上 (EMA50 {ema50_1h:.4f})，訊號強度 {strength:.1f} < {_mtf_override_threshold} 不足，拒絕進場")
                        return False
                if sma200_15m > 0 and cp >= sma200_15m:
                    if strength >= _mtf_override_threshold:
                        print(f"@@COIN_DEBUG@@ ⚠️ {sym} [MTF警告放行] 15m趨勢向上 (SMA200 {sma200_15m:.4f})，強勢覆蓋，允許進場")
                    else:
                        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [Filter:Trend_Mismatch] 15m趨勢向上 (SMA200 {sma200_15m:.4f})，訊號強度 {strength:.1f} < {_mtf_override_threshold}，拒絕進場")
                        return False

    # --- 盤整/低波動過濾 (Choppiness) ---
    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)

    bb_up = s.get("bb_up", 0.0)
    bb_down = s.get("bb_down", 0.0)
    bb_width_pct = (bb_up - bb_down) / cp if cp > 0 else 0

    if atr_24h_avg > 0 and current_atr < atr_24h_avg * 0.25:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [波動率過濾] 當前 ATR 過小，處於極度盤整 (current={current_atr:.5f}, avg={atr_24h_avg:.5f})")
        return False
    if bb_width_pct > 0 and bb_width_pct < 0.0015:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [波動率過濾] 布林帶極度收斂 (寬度={bb_width_pct*100:.2f}%)，避免洗盤")
        return False
    if not is_entry_pin_safe(sym, side):
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 反向長影線/方向未確認")
        return False

    # 量能確認過濾器 (衰竭進場策略 Exhaustion_Entry 允許低量能)
    if route != "Exhaustion_Entry" and strength <= 15.0 and not is_entry_volume_confirmed(sym, side):
        return False
    elif route != "Exhaustion_Entry" and strength > 15.0:
        # 強勢訊號只保留最低限度的量能要求 (5% 均量)
        if s["current_vol"] < s["vol_ma20"] * 0.05:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 強勢訊號但量能極度枯竭 (當前 {s['current_vol']:.0f} < 均量 5%)，攔截")
            return False

        # 加入「量能背離」過濾 (強度 15~20 適用，>20 豁免，Extreme_Reversal 永遠豁免)
        if strength <= 20.0 and route != "Extreme_Reversal":
            if s["current_vol"] >= s["vol_ma20"] * 3.0:
                print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能背離過濾] 強勢訊號({strength:.1f})但當前量 ({s['current_vol']:.0f}) 過大 (>= 3.0x均量 {s['vol_ma20']*3.0:.0f})，視為趨勢延續，攔截")
                return False
        elif strength <= 20.0 and route == "Extreme_Reversal" and s["current_vol"] >= s["vol_ma20"] * 3.0:
            print(f"@@COIN_DEBUG@@ ⚡ {sym} [Extreme_Reversal 豁免] 高量={s['current_vol']:.0f} (>= 1.5x均量) 視為量能高潮，反轉確認加分！")

        # 價格結構確認 (Price Action Confirmation)，擴大至過去 3 根 K 線
        if len(s["ohlcv"]) >= 2:
            current_close = s["ohlcv"][-1][4]
            lookback = min(3, len(s["ohlcv"]) - 1)
            past_candles = s["ohlcv"][-lookback-1:-1]
            past_highs = [c[2] for c in past_candles]
            past_lows = [c[3] for c in past_candles]
            avg_high = sum(past_highs) / len(past_highs)
            avg_low = sum(past_lows) / len(past_lows)

            if side == "sell":
                struct_ok = (current_close < avg_high) or (current_close < max(past_lows))
                if not struct_ok:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [結構過濾] 空單強勢({strength:.1f})但收盤價 ({current_close:.4f}) 未低於3K平均高點({avg_high:.4f})且未破任一低點({max(past_lows):.4f})，攔截")
                    return False
            if side == "buy":
                struct_ok = (current_close > avg_low) or (current_close > min(past_highs))
                if not struct_ok:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [結構過濾] 多單強勢({strength:.1f})但收盤價 ({current_close:.4f}) 未高於3K平均低點({avg_low:.4f})且未破任一高點({min(past_highs):.4f})，攔截")
                    return False

        # --- 新增：三道轉折防護機制 (High-Point Decay, RSI History, Cooldown) ---
        # 1. 同向虧損冷卻期 (Same-Side Cooldown)
        COOLDOWN_HOURS = 4
        COOLDOWN_SEC = COOLDOWN_HOURS * 3600
        now = time.time()

        last_loss_time = s.get("last_loss_time_short", 0) if side == "sell" else s.get("last_loss_time_long", 0)
        if now - last_loss_time < COOLDOWN_SEC:
            remaining_mins = (COOLDOWN_SEC - (now - last_loss_time)) / 60
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [同向虧損冷卻] 過去 4 小時內曾發生同向({side})虧損平倉，冷卻剩餘 {remaining_mins:.1f} 分鐘，攔截進場")
            return False

        # 2. 判斷是否為「逆勢轉折交易」
        sma200_15m = s.get("sma200_15m", 0)
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
                        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [衰減過濾] 逆勢空單強勢({strength:.1f})但現價({current_close})距離20K高點({highest_20})已跌落 {decay_pct*100:.1f}% (> 2.5%)，視為半山腰追空，攔截")
                        return False
                else:
                    decay_pct = (current_close - lowest_20) / lowest_20 if lowest_20 > 0 else 0
                    if decay_pct > COUNTER_TREND_MAX_DECAY_PCT:
                        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [衰減過濾] 逆勢多單強勢({strength:.1f})但現價({current_close})距離20K低點({lowest_20})已反彈 {decay_pct*100:.1f}% (> 2.5%)，視為半山腰追多，攔截")
                        return False

            # 4. RSI 超買/超賣歷史確認 (RSI History Confirmation)
            if "rsi_history" in s and len(s["rsi_history"]) > 0:
                recent_rsis = s["rsi_history"][-10:]
                if side == "sell":
                    highest_rsi = max(recent_rsis)
                    if highest_rsi < 45.0:
                        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [RSI歷史確認] 逆勢空單進場前，近 10 根 RSI 最高僅 {highest_rsi:.1f} (< 45.0)，未經歷過熱，視為逆勢空單假突破，攔截")
                        return False
                else:
                    lowest_rsi = min(recent_rsis)
                    if lowest_rsi > 55.0:
                        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [RSI歷史確認] 逆勢多單進場前，近 10 根 RSI 最低僅 {lowest_rsi:.1f} (> 55.0)，未見明顯回撤，視為逆勢多單假突破，攔截")
                        return False

    # 實盤最小量限制
    if route not in ("Exhaustion_Entry", "Extreme_Reversal"):
        min_volume = s["vol_ma20"] * 0.05
        if s["current_vol"] < min_volume:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [實盤最小量過濾] 當前 {s['current_vol']:.2f} < 均量 10% ({min_volume:.2f})")
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
        elif macd_hist > prev_macd_hist:
            macd_score = 3.0
        if current_rsi > 48.0:
            rsi_score = 4.0
    else:
        if prev_macd_line >= prev_macd_signal and macd_line < macd_signal:
            macd_score = 5.0
        elif macd_hist < prev_macd_hist:
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
    MIN_ENTRY_SCORE = 11.0

    if total_score < MIN_ENTRY_SCORE:
        print(f"🛑 [REJECT] {sym}: 硬條件通過，但總分未達標 (綜合得分: {total_score:.1f} < 門檻: {MIN_ENTRY_SCORE:.1f})")
        return False

    print(f"💚 [PASS] {sym}: 完美通過全套風控，准予開倉！(總得分: {total_score:.1f}, 基礎分: {base_score:.1f}, 加分A: {bonus_a:.1f}, 加分B: {bonus_b:.1f})")

    # --- 【新增】進場方向絕對一致性檢查 (Directional Consistency / Direction_Safety) ---
    # 確保進場方向與當前 K 線的收盤動態一致，防止在「反轉 K」上強行進場
    # 豁免：Extreme_Reversal / Exhaustion_Entry / Automatic_Reverse 本就逆勢操作，不受此限
    if route not in ("Extreme_Reversal", "Exhaustion_Entry", "Automatic_Reverse") and len(s.get("ohlcv", [])) >= 2:
        prev_close_dc = s["ohlcv"][-2][4]
        current_close_dc = s.get("close_price", s["ohlcv"][-1][4])

        if side == "buy" and current_close_dc < prev_close_dc and strength < 15.0:
            print(f"🛑 [Direction_Safety] {sym} 多單訊號但當前收盤 ({current_close_dc:.4f}) < 前收 ({prev_close_dc:.4f})，動能不足 (strength={strength:.1f} < 15.0)，拒絕進場")
            return False
        elif side == "sell" and current_close_dc > prev_close_dc and strength < 15.0:
            print(f"🛑 [Direction_Safety] {sym} 空單訊號但當前收盤 ({current_close_dc:.4f}) > 前收 ({prev_close_dc:.4f})，動能不足 (strength={strength:.1f} < 15.0)，拒絕進場")
            return False

    return True
