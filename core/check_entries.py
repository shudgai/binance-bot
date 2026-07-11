import logging
import asyncio
import os
import json
import time
import numpy as np

from core import ctx
from core.config import (COIN_PROFILE_CONFIG, DEFAULT_NEW_COIN_PROFILE,
    DUAL_SHOT_MIN_PROFIT_ROOM, RSI_PERIOD, DAILY_LOSS_LIMIT_PCT, get_entry_strictness_profile)
from core.indicators import (_get_atr, _macd_vals, calculate_ema, calculate_macd,
    calculate_adx, calculate_bollinger_bands, _calc_sl_tp)
from core.balance import is_daily_loss_halted
import core.balance as _bal
from core.state_manager import get_open_position_count, reset_coin_state
from core.signal_engine import (compute_signal_strength, is_reversal_still_valid,
    is_eligible_for_reverse, _load_disabled_symbols)
from core.entry_filter import is_entry_allowed
from services.bot_manager_service import set_entry_diagnosis

logger = logging.getLogger(__name__)

_PENDING_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "pending_signals_cache.json")
_PENDING_MAX_AGE_SEC = 1200  # 20 分鐘內存檔才還原，太舊的訊號還原也沒意義，讓它自然作廢


def save_pending_signals():
    """把「等待下一根K線收盤確認」中的訊號存檔。維護重啟服務（套用修復）時，main.py 的
    ctx.STATES 會整個重建，原本正在等確認的訊號會憑空消失、白等一輪——存檔讓下次啟動
    時能還原，不用眼睜睜看著剛通過風控的訊號被重啟清空。"""
    try:
        snapshot = {}
        for sym, s in ctx.STATES.items():
            if s.get("pending_side"):
                snapshot[sym] = {
                    "pending_side": s["pending_side"],
                    "pending_time": s.get("pending_time", 0),
                    "pending_strength": s.get("pending_strength", 5.0),
                    "pending_route": s.get("pending_route", "confirmed"),
                    "saved_at": time.time(),
                }
        with open(_PENDING_CACHE_PATH, "w") as f:
            json.dump(snapshot, f)
    except Exception:
        pass


def load_pending_signals():
    """啟動時還原上次存檔、還在等待確認中的訊號。超過 _PENDING_MAX_AGE_SEC 視為過期不還原。"""
    try:
        if not os.path.exists(_PENDING_CACHE_PATH):
            return
        with open(_PENDING_CACHE_PATH, "r") as f:
            snapshot = json.load(f)
        now = time.time()
        restored = []
        for sym, data in snapshot.items():
            if sym not in ctx.STATES:
                continue
            if now - data.get("saved_at", 0) > _PENDING_MAX_AGE_SEC:
                continue
            s = ctx.STATES[sym]
            s["pending_side"] = data.get("pending_side")
            s["pending_time"] = data.get("pending_time", 0)
            s["pending_strength"] = data.get("pending_strength", 5.0)
            s["pending_route"] = data.get("pending_route", "confirmed")
            restored.append(sym)
        if restored:
            logger.info(f"💾 [快取] 已還原 {len(restored)} 個等待確認中的訊號: {', '.join(restored)}")
    except Exception as e:
        logger.info(f"⚠️ [Pending快取] 讀取失敗: {e}")


def is_pending_confirmation_valid(side, candle, trigger_price=None, max_divergence=0.003):
    """Return whether the prior signal candle is still valid after the next bar closes."""
    if not candle or len(candle) < 5:
        return False

    open_price = candle[1]
    close_price = candle[4]
    high_price = candle[2]
    low_price = candle[3]

    # [新增] 價格背離檢查：防止訊號產生到確認期間，價格大幅反向變動
    if trigger_price is not None:
        price_diff = abs(close_price - trigger_price) / trigger_price
        if price_diff > max_divergence:
            logger.info(f"⚠️ [防背離] 價格變動 {price_diff*100:.2f}% 超過門檻 {max_divergence*100:.2f}%，取消訊號")
            return False

    if side == "buy":
        body = close_price - open_price
        upper_shadow = high_price - close_price
        return body > 0 and upper_shadow < body * 2.0

    if side == "sell":
        body = open_price - close_price
        lower_shadow = close_price - low_price
        return body > 0 and lower_shadow < body * 2.0

    return False


def detect_divergence(sym):
    s = ctx.STATES.get(sym)
    if not s or "rsi_history" not in s or len(s["rsi_history"]) < 3 or len(s.get("ohlcv", [])) < 3:
        return None

    closes = [x[4] for x in s["ohlcv"][-3:]]
    rsis = s["rsi_history"][-3:]

    # 價格創新低，但 RSI 沒創新低 (底背離)
    if closes[2] < closes[0] and rsis[2] > rsis[0]:
        return f"{sym} 出現底背離！價格破底 ({closes[0]:.4f}->{closes[2]:.4f}) 但 RSI 墊高 ({rsis[0]:.1f}->{rsis[2]:.1f})"
    return None


def check_all_divergence_logic():
    """自動掃描所有幣種的底背離訊號"""
    divergence_results = []
    for sym in ctx.ALL_SYMBOLS:
        res = detect_divergence(sym)
        if res:
            divergence_results.append(res)
    return divergence_results


def compute_indicators(sym):
    s = ctx.STATES[sym]
    ohlcv = s["ohlcv"]
    if len(ohlcv) < 20:
        return
    closes = np.array([x[4] for x in ohlcv])
    highs = np.array([x[2] for x in ohlcv])
    lows = np.array([x[3] for x in ohlcv])
    volumes = np.array([x[5] for x in ohlcv])
    s["closes"] = closes
    prev = s["prev_close"]
    for i in range(len(ohlcv)):
        h, l, c = ohlcv[i][2], ohlcv[i][3], ohlcv[i][4]
        if i == 0 and prev is not None:
            tr = max(h - l, abs(h - prev), abs(l - prev))
        elif i > 0:
            tr = max(h - l, abs(h - ohlcv[i-1][4]), abs(l - ohlcv[i-1][4]))
        else:
            tr = h - l
        s["tr_list"].append(tr)
    s["prev_close"] = ohlcv[-1][4]
    if len(s["tr_list"]) > 42:
        s["tr_list"] = s["tr_list"][-42:]
    if len(s["tr_list"]) >= 14:
        s["current_atr"] = float(np.mean(s["tr_list"][-14:]))
        s["atr_history"].append(s["current_atr"])
        if len(s["atr_history"]) > 1440:
            s["atr_history"] = s["atr_history"][-1440:]
        s["atr_ma20"] = float(np.mean(s["atr_history"][-20:])) if len(s["atr_history"]) >= 20 else s["current_atr"]
    if len(closes) > RSI_PERIOD:
        deltas = np.diff(closes[-(RSI_PERIOD + 1):])
        gains = deltas[deltas > 0].mean() if np.any(deltas > 0) else 1e-10
        if np.any(deltas < 0):
            losses = -deltas[deltas < 0].mean()
            rs = gains / losses
            # 正常計算，但 cap 99 避免數學上的 100 誤觸 Extreme_Reversal
            s["current_rsi"] = min(99.0, 100.0 - (100.0 / (1.0 + rs)))
        elif np.any(deltas > 0):
            s["current_rsi"] = 99.0  # 期間內全為漲K，但不等同真正超買
        else:
            s["current_rsi"] = 50.0  # 無波動
        if "rsi_history" not in s:
            s["rsi_history"] = []
        s["rsi_history"].append(s["current_rsi"])
        if len(s["rsi_history"]) > 10:
            s["rsi_history"].pop(0)
    s["vol_ma10"] = float(np.mean(volumes[-11:-1])) if len(volumes) >= 11 else float(np.mean(volumes[:-1]))
    s["vol_ma20"] = float(np.mean(volumes[-21:-1])) if len(volumes) >= 21 else float(np.mean(volumes[:-1]))
    # 使用「倒數第二根」（已完成 K 線）的量，避免當前未完成 K 線量偏低誤觸量能過濾
    s["current_vol"] = float(volumes[-2]) if len(volumes) >= 2 else float(volumes[-1])
    if len(closes) >= 20:
        s["ema20"] = calculate_ema(closes, 20)
    if len(closes) >= 50:
        s["ema50"] = calculate_ema(closes, 50)
    if len(closes) >= 26:
        m_line, m_sig, m_hist, p_line, p_sig = calculate_macd(closes)
        s["macd_line"] = m_line
        s["macd_signal"] = m_sig
        s["macd_hist"] = m_hist
        s["prev_macd_line"] = p_line
        s["prev_macd_signal"] = p_sig
    if len(closes) >= 15:
        s["adx"] = calculate_adx(highs, lows, closes, 14)
    if len(closes) >= 20:
        up, mid, low = calculate_bollinger_bands(closes)
        s["bb_up"] = up
        s["bb_mid"] = mid
        s["bb_low"] = low

    # --- Divergence Detection ---
    s["divergence"] = "none"
    if len(closes) >= 15 and len(s.get("rsi_history", [])) >= 10:
        window_closes = closes[-15:]
        window_rsi = s["rsi_history"][-10:]

        c_min = np.min(window_closes)
        c_max = np.max(window_closes)
        r_min = np.min(window_rsi)
        r_max = np.max(window_rsi)

        curr_c = closes[-1]
        curr_r = s["current_rsi"]
        prev_r = s["rsi_history"][-2] if len(s["rsi_history"]) >= 2 else curr_r

        if curr_c <= c_min and curr_r > r_min and curr_r > prev_r:
            s["divergence"] = "bullish"
        elif curr_c >= c_max and curr_r < r_max and curr_r < prev_r:
            s["divergence"] = "bearish"


async def check_entries():
    from core.orders import execute_order, close_position
    from core.market_data import load_open_positions

    disabled_syms = _load_disabled_symbols()
    # [每日熔斷] 先確認是否已觸發當日封鎖
    if is_daily_loss_halted():
        logger.info(f"[每日熔斷] 今日累計虧損已超上限 ({abs(_bal._DAILY_REALIZED_LOSS)*100:.2f}% >= {DAILY_LOSS_LIMIT_PCT*100:.1f}%)，跳過所有新進場！")
        return

    open_count = get_open_position_count()
    dynamic_max_positions = _bal.get_dynamic_max_slots()
    remaining_slots = dynamic_max_positions - open_count

    from core.config import ENTRY_STRICTNESS_MODE
    is_relaxed = (ENTRY_STRICTNESS_MODE == "relaxed")
    candidates = []
    for sym in ctx.ALL_SYMBOLS:
        s = ctx.STATES[sym]

        # 幣種已被使用者停用，跳過所有進場（但不影響現有持倉的管理）
        if sym in disabled_syms:
            continue

        # --- 自動反手快速通道 ---
        pending_rev = s.get("pending_reverse")
        if pending_rev:
            if s.get("status") == "COOLDOWN":
                # 冷卻中不允許反手快速通道繞過，避免剛平倉幾秒內又被打回同一個幣種，
                # 追出一個新倉位又立刻卡進出場失敗重試迴圈（曾發生 LINKUSDT 平倉 14 秒後
                # 又反手進場，出場單被交易所以 ReduceOnly 拒絕，卡住重試 17 分鐘拖累其他倉位）。
                s["pending_reverse"] = None
                s["pending_reverse_after_rescue"] = False
                logger.info(f"⏳ [反手取消] {sym} 冷卻中，放棄本次反手快速通道，等冷卻結束後由一般訊號重新評估")
            elif time.time() - s.get("pending_reverse_time", 0) < 300:  # 5 分鐘內有效
                if not s.get("is_ordering"):
                    # 反手前先確認方向仍然成立（大盤方向、價格位置、MACD 動能擴張），
                    # 不能單純因為原本方向停損了就假設反方向一定對，要看當下盤勢是否真的支持。
                    if await is_reversal_still_valid(sym, pending_rev):
                        logger.info(f"🔄 [自動反手執行] {sym} 偵測到反手訊號 ({pending_rev})，方向確認通過，開始建倉！")
                        price = s["close_price"]
                        s["pending_reverse"] = None
                        s["pending_reverse_after_rescue"] = False
                        s["is_ordering"] = True

                        async def _rev_task(sym, pending_rev, price):
                            try:
                                await execute_order(sym, pending_rev, price)
                            finally:
                                ctx.STATES[sym]["is_ordering"] = False
                                await load_open_positions()

                        asyncio.create_task(_rev_task(sym, pending_rev, price))
                    else:
                        logger.info(f"🚫 [反手取消] {sym} 反手訊號 ({pending_rev}) 未通過方向確認，暫不執行，繼續等待有效視窗內重新檢查")
                continue
            else:
                s["pending_reverse"] = None
                s["pending_reverse_after_rescue"] = False

        if s["status"] != "ACTIVE":
            continue

        has_position = abs(s["qty"]) > 0.000001
        current_direction = "buy" if s["qty"] > 0 else "sell" if s["qty"] < 0 else None

        # 開倉錯誤冷卻（例如幣安 -1007 送出狀態未知）：確認交易所端真的沒有新倉位後，
        # 短暫暫停這個幣種，避免立刻用同樣的條件反覆撞在同一個逾時問題上。
        if not has_position and time.time() < s.get("order_fail_cooldown_until", 0):
            continue

        # 開倉數限制 (針對新開倉)
        if not has_position and open_count >= dynamic_max_positions:
            continue

        # --- [NEW] 等待回踩 (Pullback Entry) 處理 ---
        if not has_position and s.get("waiting_pullback"):
            wp = s["waiting_pullback"]
            _wait_time = time.time() - wp["time"]
            if _wait_time > 3600:  # 1 小時沒回踩就放棄
                logger.info(f"⌛ [回踩過期] {sym} 經過 1 小時未回到支撐/壓力位，取消回踩計畫。")
                s["waiting_pullback"] = None
            else:
                p = s["close_price"]
                ema20 = s.get("ema20", 0.0)
                if ema20 > 0:
                    # 判斷是否回踩到 EMA20 (允許 0.2% 誤差)
                    if wp["side"] == "buy" and p <= ema20 * 1.002:
                        logger.info(f"🎯 [回踩進場] {sym} 成功回踩 (現價 {p:.4f} 接近 EMA20 {ema20:.4f})，準備建多單！")
                        candidates.append((sym, wp["side"], wp["strength"], wp["route"]))
                        s["waiting_pullback"] = None
                    elif wp["side"] == "sell" and p >= ema20 * 0.998:
                        logger.info(f"🎯 [回踩進場] {sym} 成功回抽 (現價 {p:.4f} 接近 EMA20 {ema20:.4f})，準備建空單！")
                        candidates.append((sym, wp["side"], wp["strength"], wp["route"]))
                        s["waiting_pullback"] = None
                continue  # 處於等回踩狀態時，跳過底下一般的新訊號判定

        current_candle_time = s["ohlcv"][-1][0] if s["ohlcv"] else 0

        # --- [新增] 自動反手訊號緩衝與 K 線收盤確認機制 ---
        if s.get("pending_reverse_trigger"):
            pending_rev_data = s["pending_reverse_trigger"]
            if current_candle_time > pending_rev_data.get("time", 0):
                logger.info(f"⏳ [{sym}] 進入新 K 線，驗證自動反手趨勢持續性...")
                if await is_reversal_still_valid(sym, pending_rev_data["side"]):
                    src = pending_rev_data.get("source", "Signal")
                    logger.info(f"⚡ [{sym}] [Reversal_Confirmed] {src} 反手確認！平倉並反手建倉 ({pending_rev_data['side']})，強度 {pending_rev_data.get('strength',0):.1f}")
                    # 1. 平倉舊倉位
                    await close_position(sym, current_direction, abs(s["qty"]), s["close_price"], s["avg_price"], reason="[AUTOMATIC_REVERSE]")
                    await asyncio.sleep(1)
                    reset_coin_state(sym)
                    # 2. 反手建倉，並記錄反手時間（冷卻 30 分鐘防連續反手）
                    s["last_reverse_time"] = time.time()
                    await execute_order(sym, pending_rev_data["side"], s["close_price"])
                else:
                    logger.info(f"❌ [{sym}] [Reversal_Cancelled] 觀察期間趨勢失效，取消反手，保留原倉位。")

                s["pending_reverse_trigger"] = None
                continue
            else:
                # 還在同一根 K 線，繼續觀察
                continue

        # --- 新增：等待收盤確認機制 ---
        if s.get("pending_side"):
            if is_relaxed:
                logger.info(f"⚡ [寬鬆即時確認] {sym} 寬鬆模式直接放行已還原的 pending {s['pending_side']} 訊號")
                side = s["pending_side"]
                strength = s.get("pending_strength", 5.0)
                route = s.get("pending_route", "confirmed")
                s["pending_side"] = None
                candidates.append((sym, side, strength, route))
                continue
            if current_candle_time <= s.get("pending_time", 0):
                continue

            # 換線了，檢查前一根(訊號K線)是否反轉
            if len(s["ohlcv"]) >= 2:
                prev_candle = s["ohlcv"][-2]
                prev_open = prev_candle[1]
                prev_close = prev_candle[4]

                # 使用新增的 trigger_price 進行背離檢查
                is_valid = is_pending_confirmation_valid(s["pending_side"], prev_candle, s.get("pending_trigger_price"))

                if is_valid:
                    # Second-Bar Confirmation：對比訊號K收盤價（非最高/低點）
                    # 原邏輯用 trigger_high * 0.985：下根開盤在 CLOSE 附近往往低於 HIGH 1-2%，
                    # 導致大量有效訊號被誤判為假突破。改用收盤價作基準更合理。
                    current_price = s["close_price"]
                    trigger_high = prev_candle[2]
                    trigger_low = prev_candle[3]

                    if s["pending_side"] == "buy":
                        if current_price < prev_close * 0.985:
                            logger.info(f"⚠️ [防二次誘騙] {sym} 第二根 K 線現價 {current_price:.4f} 低於訊號K收盤 {prev_close:.4f} 的 98.5%，但已放寬為小幅回抽，保留多單。")
                        elif current_price < prev_close * 0.990:
                            logger.info(f"⚠️ [防二次誘騙] {sym} 第二根 K 線現價 {current_price:.4f} 輕微回抽，保留多單。")
                    elif s["pending_side"] == "sell":
                        if current_price > prev_close * 1.015:
                            logger.info(f"⚠️ [防二次誘騙] {sym} 第二根 K 線現價 {current_price:.4f} 高於訊號K收盤 {prev_close:.4f} 的 101.5%，但已放寬為小幅反彈，保留空單。")
                        elif current_price > prev_close * 1.010:
                            logger.info(f"⚠️ [防二次誘騙] {sym} 第二根 K 線現價 {current_price:.4f} 輕微反彈，保留空單。")

                    # [新增] 量能續航檢查：放寬為跟進量 >= 訊號量的 10%，避免小量回抽被誤判
                    if is_valid:
                        signal_vol = prev_candle[5]
                        follow_vol = s.get("current_vol", 0)
                        if signal_vol > 0 and follow_vol < signal_vol * 0.1:
                            logger.info(f"⚠️ [量能續航] {sym} 跟進量 {follow_vol:.0f} 低於訊號量 {signal_vol:.0f} × 10%，但已放寬保留訊號")
                        elif signal_vol > 0 and follow_vol < signal_vol * 0.2:
                            logger.info(f"⚠️ [量能續航] {sym} 跟進量 {follow_vol:.0f} 略低於訊號量 {signal_vol:.0f} × 20%，保留訊號")

                if not is_valid:
                    # 記錄假突破事件，同區間再次觸發時提高閾值
                    if s.get("pending_side"):
                        s["fake_breakout"] = {
                            "time": time.time(),
                            "side": s["pending_side"],
                            "level_high": prev_candle[2],
                            "level_low": prev_candle[3],
                        }

                if is_valid:
                    s["fake_breakout"] = None
                    logger.info(f"✅ [訊號確認] {sym} {s['pending_side']} 訊號已確認 (K線收盤通過)")
                    side = s["pending_side"]
                    strength = s.get("pending_strength", 5.0)
                    route = s.get("pending_route", "confirmed")
                    s["pending_side"] = None
                    logger.info(f"🧭 [ENTRY_GATE] {sym} pending確認通過，加入候選隊列 | side={side} route={route} strength={strength:.2f}")
                    # 所有關卡在進入 pending 前已完成篩選，確認後直接放行
                    candidates.append((sym, side, strength, route))
                    continue
                else:
                    logger.info(f"❌ [訊號失效] {sym} {s['pending_side']} 訊號 K 線收盤反轉，取消開倉。")
                    s["pending_side"] = None
            else:
                s["pending_side"] = None
            continue

        # 原本的計算邏輯
        side_strength = compute_signal_strength(sym)
        if side_strength is None or side_strength[0] is None:
            set_entry_diagnosis(f"{sym}: 暫無有效訊號")
            continue
        side, strength, route = side_strength

        # [Layer 0] 每幣種最低信號強度門檻
        profile = get_entry_strictness_profile()
        coin_profile_min_sig = COIN_PROFILE_CONFIG.get(sym, DEFAULT_NEW_COIN_PROFILE).get("min_signal_strength", 20.0)
        # 原本用 min() 取兩者較低的門檻，等於嚴格模式的全域門檻(15.0)永遠蓋掉個別幣種
        # 特別調高的門檻（今天稍早才把主力幣/新幣門檻拉高到 18~24，min() 卻讓實際生效
        # 門檻一直卡在 15.0，等於那次調整從未真正生效）。改成 max()，兩個門檻都當作
        # 下限，用較嚴格的那個，個別幣種調高的門檻才會真正生效。
        min_sig = max(coin_profile_min_sig, profile.get("min_signal_strength", 10.0))
        if profile.get("min_signal_strength", 10.0) <= 10.0:
            min_sig = max(min_sig - 1.5, 6.0)
        # 大盤盤整（BTC 1H ADX 過低、沒有明確趨勢）時，動能型多空訊號普遍缺乏後續動能，
        # 今天實測 AVAX/TRX/DOT/WLD/LINK 好幾筆都是這個情況：峰值都在 0.5% 以下就陰跌
        # 打平出場。盤整期間拉高門檻，減少這種訊號品質不足以撐過盤整雜訊的進場。
        if ctx.MARKET_WIND.get("is_ranging"):
            min_sig += 5.0
        if strength < min_sig:
            set_entry_diagnosis(f"{sym}: 強度 {strength:.1f} < 門檻 {min_sig:.1f}")
            continue

        # --- 2. 多重共振過濾區塊 (Multi-Confluence Entry Filter) ---
        cp = s["close_price"]
        ema50_1h = s.get("ema50_1h", 0)
        sma200_15m = s.get("sma200_15m", 0)
        rsi = s.get("current_rsi", 50)
        macd_hist = s.get("macd_hist", 0.0)
        vol_ma20 = s.get("vol_ma20", 0.0)
        volume = s["ohlcv"][-2][5] if len(s["ohlcv"]) > 1 else (s["ohlcv"][-1][5] if len(s["ohlcv"]) > 0 else 0)

        # A. 數據完整性檢查
        if sma200_15m == 0 or vol_ma20 == 0:
            continue

        # Exhaustion_Entry 與 Extreme_Reversal 是反轉策略，不受一般動能與 RSI 限制
        _macd_tiny = 1e-6
        if route not in ["Exhaustion_Entry", "Extreme_Reversal"]:
            profile = get_entry_strictness_profile()
            rsi_floor = profile.get("rsi_long_floor", 20.0)
            rsi_ceiling = profile.get("rsi_long_ceiling", 78.0)
            if side == "buy":
                if rsi <= rsi_floor - 2.0:
                    logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 多單 RSI 極端超賣 ({rsi:.1f} <= 22)，防接刀")
                    set_entry_diagnosis(f"{sym}: RSI 超賣過頭，阻擋做多")
                    continue
                if macd_hist < -_macd_tiny and rsi < max(rsi_floor + 8.0, 35.0):
                    logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 多單 RSI 低 ({rsi:.1f}) 且 MACD 仍負 ({macd_hist:.6f})")
                    set_entry_diagnosis(f"{sym}: RSI/MACD 仍偏弱，阻擋做多")
                    continue
            else:  # sell
                rsi_floor = profile.get("rsi_short_floor", 20.0)
                rsi_ceiling = profile.get("rsi_short_ceiling", 72.0)
                if rsi >= rsi_ceiling + 6.0:
                    logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 空單 RSI 極端超買 ({rsi:.1f} >= 78)，防追高")
                    set_entry_diagnosis(f"{sym}: RSI 超買過頭，阻擋做空")
                    continue
                if macd_hist > _macd_tiny and rsi > min(rsi_ceiling - 4.0, 65.0):
                    logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 空單 RSI 高 ({rsi:.1f}) 且 MACD 仍正 ({macd_hist:.6f})")
                    set_entry_diagnosis(f"{sym}: RSI/MACD 仍偏強，阻擋做空")
                    continue

        # D. 真實性驗證 (Volume Confirmation) - 動態門檻
        _atr_hist_ce = s.get("atr_history", [])
        _atr_avg_ce = float(np.mean(_atr_hist_ce)) if len(_atr_hist_ce) > 0 else 0.0
        _atr_cur_ce = s.get("current_atr", 0.0)
        _is_low_vol_ce = (_atr_avg_ce > 0 and _atr_cur_ce <= _atr_avg_ce)
        _d_multiplier = 0.03 if _is_low_vol_ce else 0.04
        if route not in ("Exhaustion_Entry", "Extreme_Reversal") and volume < (vol_ma20 * _d_multiplier):
            logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 量能極度不足 (當前量 {volume:.0f} < 均量 {vol_ma20:.0f} * {_d_multiplier})")
            set_entry_diagnosis(f"{sym}: 量能不足，無法進場")
            continue

        # E. 參與度過濾 (Participation Filter)
        profile = get_entry_strictness_profile()
        if len(s["ohlcv"]) > 1:
            current_vol = volume  # 已是 ohlcv[-2]
            prev_vol = s["ohlcv"][-3][5] if len(s["ohlcv"]) > 2 else s["ohlcv"][-2][5]
            price_change = cp - s["ohlcv"][-2][1]

            _rvol_multiplier = 0.03 if _is_low_vol_ce else 0.04
            rvol_check = current_vol > (vol_ma20 * _rvol_multiplier)

            h24_quote_volume_est = vol_ma20 * cp * 288
            liquidity_check = h24_quote_volume_est > 1000000

            volume_price_sync = False
            if side == "buy" and cp <= s["ohlcv"][-2][4] and price_change > 0 and current_vol > prev_vol:
                volume_price_sync = True
            elif side == "sell" and price_change < 0 and current_vol > prev_vol:
                volume_price_sync = True

            if route != "Exhaustion_Entry":
                if not liquidity_check and profile.get("min_signal_strength", 10.0) > 10.0:
                    logger.info(f"🛑 [LOW_PARTICIPATION] {sym} 被攔截：流動性不足 (估算24H交易額: {h24_quote_volume_est:,.0f} < 1,000,000)")
                    set_entry_diagnosis(f"{sym}: 流動性不足，放棄進場")
                    continue
                if not rvol_check and profile.get("min_signal_strength", 10.0) > 10.0:
                    _rvol_pct = int(_rvol_multiplier * 100)
                    logger.info(f"🛑 [LOW_PARTICIPATION] {sym} 被攔截：量能爆發不足 (目前 {current_vol:.0f} 未達均量 {_rvol_pct}% | {'低波動放寬' if _is_low_vol_ce else '高波動嚴格'})")
                    set_entry_diagnosis(f"{sym}: 量能爆發不足，放棄進場")
                    continue
                if not volume_price_sync:
                    logger.info(f"⚠️ [LOW_PARTICIPATION] {sym} 量價不協同 (價格變動: {price_change:.6f}, 大於前量: {current_vol > prev_vol})，但已放寬不攔截")

        # F. 極端區域防禦 (Extreme Zone Defense)
        if route != "Exhaustion_Entry" and strength <= 15.0:
            profile = get_entry_strictness_profile()
            if side == "buy" and rsi > max(profile.get("rsi_long_ceiling", 78.0) + 4.0, 80.0):
                logger.info(f"🛑 [EXTREME_ZONE_FAIL] {sym} 被攔截：RSI {rsi:.1f} 極端超買，拒絕追高做多")
                continue
            if side == "sell" and rsi < min(profile.get("rsi_short_floor", 20.0) - 2.0, 25.0):
                logger.info(f"🛑 [EXTREME_ZONE_FAIL] {sym} 被攔截：RSI {rsi:.1f} 極端超賣，拒絕殺低做空")
                continue
        elif route != "Exhaustion_Entry" and strength > 15.0:
            profile = get_entry_strictness_profile()
            if side == "buy" and rsi > max(profile.get("rsi_long_ceiling", 78.0) + 10.0, 88.0):
                logger.info(f"🛑 [EXTREME_ZONE_FAIL] {sym} 強勢訊號仍被攔截：RSI {rsi:.1f} 極端超買頂部")
                continue
            if side == "sell" and rsi < min(profile.get("rsi_short_floor", 20.0) - 8.0, 12.0):
                logger.info(f"🛑 [EXTREME_ZONE_FAIL] {sym} 強勢訊號仍被攔截：RSI {rsi:.1f} 極端超賣底部")
                continue

        logger.info(f"✅ [CONFLUENCE_PASS] {sym}: {side} 四重防禦過濾皆通過！(Route: {route})")
        logger.info(f"🧭 [ENTRY_GATE] {sym} 進入最後進場檢查 | side={side} route={route} strength={strength:.2f}")

        # --- 方向鎖定 (Direction Lock) 與 高門檻自動反手 ---
        if has_position:
            if side != current_direction:
                if await is_eligible_for_reverse(sym, strength):
                    if not s.get("pending_reverse_trigger"):
                        s["pending_reverse_trigger"] = {
                            "side": side,
                            "time": current_candle_time,
                            "strength": strength,
                            "source": "Signal",
                        }
                        logger.info(f"⚡ [{sym}] [Pending_Reversal_Detected] 反轉訊號強度 {strength:.1f}，等待下一根 K 收盤確認...")
                    continue
                else:
                    continue
            else:
                # 金字塔順勢加碼已在 execute_order() 無條件停用（core/orders.py:1253
                # `if s["entry_count"] > 0 and not is_rescue_dca: return`，跟持倉是
                # 賺是賠無關，一律拒絕）。這裡以前還是會放行到候選清單、跑完整套
                # CONFLUENCE_PASS/ENTRY_GATE/Allocation_Ratio/execute_order 流程，
                # 才在最後一步被拒絕——實測 UNIUST持續有效的強訊號每輪都重新跑一次
                # 這整套白工，擠壓掉主迴圈時間，導致「持倉時每 5 秒快速刷新價格」的
                # 空檔幾乎沒有，讓 Tight_Trailing_Stop 偵測到峰值回撤時價格已經比真正
                # 該觸發的時間點多跌了快 1%。這裡直接跳過，不再產生加碼候選訊號。
                # 真正的救援攤平（虧損逼近停損時的最後一次性攤平）走的是 exits.py 的
                # _attempt_forced_rescue（is_rescue_dca=True），不受這裡影響。
                continue

        if not is_entry_allowed(sym, side, route, strength):
            continue

        # --- 反手冷卻時間 (min_flip_time) 過濾 ---
        # 注意：這裡必須用機器人「自己」上一次真正進場的方向與出場時間
        # (last_entry_direction / last_exit_time)，不能用 last_trade_side /
        # last_trade_time —— 那兩個欄位是 core/trade_signal.py 從交易所「公開成交流」
        # (fetch_trades) 更新的，代表的是市場上任何人最後一筆成交的方向，跟本機器人
        # 有沒有做過交易完全無關，每幾秒就會因為別人的成交而隨機翻動，導致這個冷卻
        # 在機器人根本還沒進場過的幣種上也會誤觸發。
        last_trade_side = s.get("last_entry_direction", "")
        if last_trade_side != "" and side != last_trade_side and route != "Automatic_Reverse":
            flip_elapsed = time.time() - s.get("last_exit_time", 0)
            last_exit = s.get("last_exit_reason", "")
            is_stop_loss = "Stop" in last_exit or "Loss" in last_exit or "Trailing" in last_exit or "Momentum_Fade" in last_exit

            if is_stop_loss:
                min_flip = 60
            else:
                min_flip = s.get("min_flip_time", 300)

            if flip_elapsed < min_flip:
                logger.info(f"⏳ [Filter:Cooldown] [獲利防反手] {sym} 欲 {side}，但距離上次做 {last_trade_side} 僅 {flip_elapsed:.0f}s (獲利後需冷卻 {min_flip}s)，保護利潤不接刀！")
                continue

        # --- 同價位防雙巴鎖 (Price Zone Lock) ---
        p = s["close_price"]
        last_entry_price = s.get("last_entry_price", 0.0)
        last_entry_dir = s.get("last_entry_direction", "")
        if last_entry_price > 0 and last_entry_dir != "" and route != "Automatic_Reverse":
            price_diff_pct = abs(p - last_entry_price) / last_entry_price
            if price_diff_pct < 0.003 and side != last_entry_dir:
                logger.info(f"🛑 [Filter:Choppiness] {sym} 欲 {side}，但現價 {p:.4f} 距離上次進場價 {last_entry_price:.4f} 誤差小於 0.3%，陷入原地盤整，拒絕雙巴被洗！")
                continue

        # --- 動能背離過濾 (Divergence Filter) ---
        divergence_type = s.get("divergence", "none")
        if route == "Automatic_Reverse":
            if (side == "buy" and cp <= s["ohlcv"][-2][4] and divergence_type == "bullish") or (side == "sell" and divergence_type == "bearish"):
                strength *= 1.5
                logger.info(f"🌟 [Divergence_Boost] {sym} 偵測到強烈背離，權重提升至 {strength:.2f}")
            else:
                strength *= 0.9
        else:
            if divergence_type == "bearish" and side == "buy":
                logger.info(f"@@COIN_DEBUG@@ 🛑 [Divergence_Block] {sym} 頂背離阻擋做多 → 訊號取消")
                continue
            if divergence_type == "bullish" and side == "sell":
                logger.info(f"@@COIN_DEBUG@@ 🛑 [Divergence_Block] {sym} 底背離阻擋做空 → 訊號取消")
                continue

        # --- 1H 多重時間週期 (Multi-Timeframe) 過濾 ---
        if s.get("mtf_filter", True):
            # 門檻整合 7dceb33 與今天的修正成綜合版，統一訂在 20（跟 entry_filter.py
            # 的 _MACRO_OVERRIDE_STRENGTH、方向集中度豁免門檻對齊），不要太難開倉。
            #
            # Route B（EMA20 回測彈跳，core/signal_engine.py route_b_long/short）本身
            # 只看 5m 的 EMA20/50 關係，完全不含任何 1H/15m 大週期的判斷——這道 1H 過濾
            # 正是為了補上 Route B 缺的大週期確認，是它最需要的安全網。但實測 Route B
            # 的強度分數常常整輪多個不相干幣種同時落在 26~32 的相近區間（明顯是被 BTC/
            # 大盤動能帶動的共同分數，不是各幣種自己的進場品質），導致這道安全網幾乎每次
            # 都被強度跳過：實測 TRUMPUSDT(RSI 71.7)/DOTUSDT(RSI 64.3)/ADAUSDT(RSI 42.9，
            # 完全不算超買) 在同一小時內全部用這個 Override 跳過 1H 趨勢確認去追空，結果
            # 6 戰 6 敗。改成 Route B 一律不給強度豁免、必須真的通過 1H 趨勢確認；Route A
            # 本身條件更完整（含 5m RSI 方向/EMA50 gate 等更多重確認），繼續保留強度豁免。
            if route != "b" and (strength > 20.0 or route == "Automatic_Reverse"):
                logger.info(f"🚀 [強勢訊號 Override] {sym} 強度 {strength:.2f} 極高或來自反手，跳過 MTF 趨勢過濾直接允許進場")
            else:
                ema50_1h = s.get("ema50_1h", 0.0)
                if ema50_1h > 0:
                    if side == "buy" and p < ema50_1h:
                        logger.info(f"📉 [1H 過濾] {sym} 1H 趨勢向下 (現價 {p:.4f} < EMA50 {ema50_1h:.4f})，忽略買入訊號")
                        continue
                    if side == "sell" and p > ema50_1h:
                        logger.info(f"📈 [1H 過濾] {sym} 1H 趨勢向上 (現價 {p:.4f} > EMA50 {ema50_1h:.4f})，忽略賣出訊號")
                        continue

        # --- R:R 盈虧比過濾 (Risk:Reward Filter) ---
        # 使用者先前要求增加開倉次數，門檻從 1.5/1.2/1.3 下修到 1.3/1.0/1.1；後來發現
        # 邊緣訊號進場後常常原地打轉、最高獲利很小就打平/小虧出場，要求拉回一點，
        # 犧牲一些開倉次數換單筆品質，改成 1.4/1.1/1.2（介於原始與寬鬆之間）。
        atr_val, sl_dist, tp_dist, expected_rr = _calc_sl_tp(sym, side, s, p, route)
        base_rr_thresh = s.get("min_rr", 1.4)

        # 使用者反映現在幾乎完全開不了倉：實測訊號強度大多落在 15~26，strength>20 才給
        # 最寬鬆 1.1 門檻的話，大部分訊號還是卡在 base_rr_thresh(1.4)~2.0。放寬斷點到
        # >14/>12，讓目前實際出現的訊號強度範圍也能吃到比較寬鬆的 R:R 門檻。
        rr_thresh = 1.1 if strength > 14.0 else (1.2 if strength > 12.0 else base_rr_thresh)
        if base_rr_thresh >= 2.0:
            rr_thresh = base_rr_thresh

        if route != "Automatic_Reverse" and expected_rr < rr_thresh:
            logger.info(f"🛑 [Filter:RR_Low] {sym} 預期盈虧比 {expected_rr:.2f} < {rr_thresh}，放棄暫存")
            continue

        expected_profit_pct = tp_dist / p if p > 0 else 0
        if expected_profit_pct < DUAL_SHOT_MIN_PROFIT_ROOM:
            logger.info(f"⚠️ [獲利空間過濾] {sym} 預期潛在利潤過小 ({expected_profit_pct*100:.2f}% < {DUAL_SHOT_MIN_PROFIT_ROOM*100:.1f}%)，無法覆蓋手續費與滑點，放棄暫存")
            continue

        # 絕對獲利空間硬門檻 (MinProfit Hard Gate)
        # 防止在極低波動（ATR 極小）時進場。原本 1.5%，使用者反映現在幾乎開不了倉，
        # 降到 0.8%（防止過低波動進場的用意還在，只是門檻沒那麼高）。
        _HARD_MIN_PROFIT_PCT = 0.008  # 0.8% 硬門檻
        if expected_profit_pct < _HARD_MIN_PROFIT_PCT:
            logger.info(f"🛑 [Filter:MinProfit_Hard] {sym} 預期獲利僅 {expected_profit_pct*100:.2f}%，遠低於 {_HARD_MIN_PROFIT_PCT*100:.1f}% 硬門檻，拒絕進場")
            continue

        # --- Flip Buffer: 防止快速反手 ---
        last_entry_time = s.get("last_entry_time", 0.0)
        exempt_symbols = ["UNIUSDT"]
        if sym not in exempt_symbols and route != "Automatic_Reverse" and last_entry_time > 0 and (time.time() - last_entry_time) < 300:
            logger.info(f"⏳ [Flip Buffer] {sym} 訊號 {side} 被攔截 (距離上次開倉僅 {time.time() - last_entry_time:.0f}s)")
            continue

        # --- 錯誤方向禁止再進 (Wrong Direction Ban) ---
        _wd_time = s.get("wrong_dir_time", 0.0)
        _wd_side = s.get("wrong_dir_side", "")
        if _wd_side == side and time.time() - _wd_time < 300:
            logger.info(f"⏳ [Wrong Dir Ban] {sym} 同方向 {side} 剛在 {time.time()-_wd_time:.0f}s 前開錯方向，冷卻中 (5min)")
            continue

        # --- 假突破記憶檢查 (Fake Breakout Memory) ---
        _fb = s.get("fake_breakout")
        if _fb and time.time() - _fb["time"] < 1800 and route not in ("Extreme_Reversal", "Automatic_Reverse"):
            _fb_level = _fb["level_high"] if _fb["side"] == "buy" else _fb["level_low"]
            _current_dist = abs(p - _fb_level) / max(_fb_level, 1e-8)
            _atr_fb = s.get("current_atr", 0)
            if _current_dist < (_atr_fb * 2 / max(p, 1e-8)) and side == _fb["side"]:
                _boost_needed = 5.0
                _effective_min = min_sig + _boost_needed
                if strength < _effective_min:
                    logger.info(f"⏳ [假突破記憶] {sym} 距上次同向假突破不到 2 ATR ({_current_dist*100:.3f}%)，強度 {strength:.1f} < {_effective_min:.1f}，暫停進場")
                    continue
                logger.info(f"⚠️ [假突破記憶] {sym} 距上次同向假突破不到 2 ATR，但強度 {strength:.1f} >= {_effective_min:.1f}，允許進場")
                strength *= 0.85

        # 通過 Flip Buffer，進入 pending 狀態等待下一根 K 線確認
        if is_relaxed:
            logger.info(f"⚡ [寬鬆即時開倉] {sym} 通過寬鬆篩選，繞過收盤等待直接進場！")
            # 寬鬆模式繞過 pending 確認，原本沒有機會走到下面設定 entry_reason 的那一行，
            # 導致這種路線進場的單子平倉記錄永遠是 UNKNOWN，這裡補上。
            s["entry_reason"] = route
            from core.entry_reason_store import save_entry_reason
            save_entry_reason(sym, route)
            candidates.append((sym, side, strength, route))
            continue

        # [新增] 記錄訊號產生時的價格，用於後續防範「開倉背離」
        s["pending_trigger_price"] = s["close_price"]
        s["pending_side"] = side
        s["pending_time"] = current_candle_time
        s["pending_strength"] = strength
        s["pending_route"] = route
        # 保留到平倉記錄，避免 trade_history 全部 UNKNOWN。同時落地存檔（entry_reason_store），
        # 因為這個欄位只存在記憶體內的 ctx.STATES，bot 重啟就會被清空——今天一天內重啟
        # 很多次，導致幾乎所有平倉記錄的 entry_reason 都變成 UNKNOWN，完全查不到當初為何
        # 進場。落地存檔後，重啟時可以比照 entry_time_store 的做法一併還原。
        s["entry_reason"] = route
        from core.entry_reason_store import save_entry_reason
        save_entry_reason(sym, route)

        logger.info(f"⏳ [等待確認] {sym} 產生 {side} 訊號 ({route})，等待目前 K 線收盤確認...")
        logger.info(f"🧭 [ENTRY_GATE] {sym} 進入 pending 狀態 | side={side} route={route} strength={strength:.2f}")
        set_entry_diagnosis(f"{sym}: 等待 K 線收盤確認")

    save_pending_signals()

    if not candidates:
        return

    candidates.sort(key=lambda x: -x[2])
    logger.info(f"📊 [訊號排行] {' | '.join(f'{sym}:{side}({strength:.2f})' for sym, side, strength, _ in candidates[:3])}")

    # 資金分配比例（raw_ratio）原本用「本輪全部候選訊號」的強度總和當分母，但槽位數
    # 有限（remaining_slots），本輪候選常常遠多於實際會被派發的數量——實測同一輪出現
    # 13 個賣出候選、槽位只剩 3 個，ADA/DOT/AVAX 強度都到 31~32（很強），分到的比例
    # 卻被其餘 10 個「根本不會真的開倉」的候選一起拉低到只剩 11%，資金被稀釋到跟強度
    # 完全不成比例。改成只用「實際會被派發的前 remaining_slots 名」（candidates 已經
    # 依強度排序）當分母，讓分配比例真正反映這批「會開倉的訊號」之間的相對強弱，不被
    # 陪榜、根本拿不到槽位的候選稀釋。
    _weight_pool = candidates[:remaining_slots] if remaining_slots > 0 else candidates
    total_weight = sum(strength for _, _, strength, _ in _weight_pool)

    for sym, side, strength, route in candidates:
        s = ctx.STATES[sym]
        has_pos = abs(s["qty"]) > 0.000001

        if not has_pos:
            # 使用者要求移除「機會成本輪替」：原本槽位滿了會找一個已經停滯夠久、
            # 獲利卻不再往上走的舊倉位平倉讓位給更強新訊號，但這會把還在正常發展、
            # 只是還沒繼續創新高的獲利倉位提早平倉。現在槽位滿了就單純跳過這個候選，
            # 交給既有的停損/停利/停滯超時機制自然決定舊倉位何時該出場。
            if remaining_slots <= 0:
                continue

            # --- 同方向集中度風控 (Direction Concentration Guard) ---
            # 使用者反映：好幾次同一時段內，雷達選出的幣種訊號一面倒向同一個方向
            # （實測案例 AVAXUSDT/DOTUSDT/BCHUSDT/LINKUSDT/ADAUSDT 5 筆同時做空），
            # 導致整個帳戶對大盤同一個方向的逆風完全沒有分散——30 分鐘內 BTC 只是
            # 緩漲 0.46%，5 筆就同時虧損收場。但使用者指出：如果大盤當下真的是
            # 確認趨勢，同方向本來就該多開，不該被當成「押注」硬擋——问题只在於
            # 「沒有大盤趨勢依據、單純幾個幣種訊號剛好同時同向」這種巧合式集中。
            # 所以這裡改成有條件放行：BTC 4H+1H 雙重確認同向時（跟 MACRO_BLOCK
            # 用的是同一組 ctx.MARKET_WIND 資料），視為真趨勢單邊行情，不設上限；
            # 沒有大盤同向確認時，才視為缺乏依據的巧合式堆疊，套用集中度上限，
            # 除非訊號強度極高（統一對齊 20，跟本檔其他強訊號豁免門檻一致）。
            # 原本用「總槽位數 - 2」這個固定差值算，在槽位數=5 時等於 60%（3/5）；
            # 但槽位數改成 3 之後，同一個公式算出來變成只剩 1，比例上收得比原本嚴
            # 很多。改成統一用比例（60%）反推，槽位數=5 時還是算出 3（跟原本一致），
            # 槽位數=3 時算出 2，比例維持一致，不會因為總槽位變少而被不成比例地收緊。
            _MAX_SAME_DIRECTION = max(1, round(dynamic_max_positions * 0.6))
            _DIRECTION_OVERRIDE_STRENGTH = 20.0
            # 趨勢放行也要有強度下限（15.0，低於一般豁免門檻 20，因為已經有大盤
            # 4H+1H 雙重確認撐腰，不用比純強訊號豁免更嚴）。實測 2026/7/8 熊市盤整
            # 一整天，BTC 4H+1H 幾乎全程雙熊，導致這道「趨勢放行」形同常態解除
            # 集中度上限，連強度只有 11~14 的弱訊號都能佔滿第三個槽位，讓帳戶在
            # 同一波短線反彈中三個倉位一起同方向受創（XLM/SUI/BCH/TRUMP 等案例）。
            # 加上這道下限，讓真正弱訊號即使大盤趨勢確認也不能無條件擠佔集中度
            # 上限外的名額，只有訊號本身也有一定強度時才放行。
            _TREND_OVERRIDE_MIN_STRENGTH = 15.0
            _same_dir_count = sum(
                1 for _s in ctx.STATES.values()
                if abs(_s.get("qty", 0.0)) > 0.000001 and (_s["qty"] > 0) == (side == 'buy')
            )
            if _same_dir_count >= _MAX_SAME_DIRECTION:
                _btc_4h = ctx.MARKET_WIND.get("btc_trend_4h")
                _btc_1h = ctx.MARKET_WIND.get("btc_trend_1h")
                _macro_confirms_direction = (
                    (side == 'sell' and _btc_4h == "BEAR" and _btc_1h == "BEAR") or
                    (side == 'buy' and _btc_4h == "BULL" and _btc_1h == "BULL")
                )
                if _macro_confirms_direction and strength >= _TREND_OVERRIDE_MIN_STRENGTH:
                    logger.info(f"🧭 [方向集中度-趨勢放行] {sym} 同方向倉位已達 {_same_dir_count}，但 BTC 4H+1H 趨勢確認同向 ({_btc_4h}/{_btc_1h}) 且強度 {strength:.1f} >= {_TREND_OVERRIDE_MIN_STRENGTH}，判定為真趨勢單邊行情，允許加開")
                elif strength < _DIRECTION_OVERRIDE_STRENGTH:
                    _reason = f"大盤無同向趨勢確認 (4H:{_btc_4h}/1H:{_btc_1h})" if not _macro_confirms_direction else f"雖有趨勢確認但強度 {strength:.1f} < {_TREND_OVERRIDE_MIN_STRENGTH} 放行下限"
                    logger.info(f"🧭 [方向集中度風控] {sym} 目前已有 {_same_dir_count} 筆同方向({side})倉位 >= 上限 {_MAX_SAME_DIRECTION}，{_reason}，且強度 {strength:.1f} < {_DIRECTION_OVERRIDE_STRENGTH}，放棄本次訊號以分散風險")
                    continue

            remaining_slots -= 1
            logger.info(f"⚡ [即時開倉] {sym} 觸發訊號 ({route} 路線)，即刻首倉進場！")
            set_entry_diagnosis(f"{sym}: 準備立即開倉 ({route})")
        # 金字塔順勢加碼（has_pos 且同方向）已在上方「方向鎖定」區塊直接 continue 掉，
        # 不會有 has_pos=True 的候選走到這裡；execute_order() 那邊的無條件停用
        # （core/orders.py:1253）留著當防禦性保底，避免未來其他路徑意外繞過這裡。

        if not s.get("is_ordering"):
            s["is_ordering"] = True

            # --- 動態權重分配 (Dynamic Position Sizing) ---
            # 使用者指出：原本的算法只看「這個訊號佔本輪候選訊號強度總和的比例」，如果
            # 這輪只有它一個候選（很常見），比例永遠是 100%、直接封頂 85%——導致一個強度
            # 只有 12（偏弱）的訊號跟強度 30+ 的頂級訊號拿到一樣多的資金，跟訊號本身的
            # 品質完全脫鉤。改成同時看「訊號自身的絕對強度」：強度越高，允許動用的資金
            # 上限越高；弱訊號即使是本輪唯一候選，也不會自動封頂到 85%。
            # 門檻取自實測 984 筆進場訊號的強度分布：min≈10（最弱仍通過篩選）、
            # p90≈32（前10%頂級訊號）。
            raw_ratio = strength / total_weight if total_weight > 0 else 1.0
            _strength_floor = 10.0
            _strength_ceiling = 32.0
            _min_alloc_pct = 0.30
            _max_alloc_pct = 0.85
            _strength_scaled = max(0.0, min(1.0, (strength - _strength_floor) / (_strength_ceiling - _strength_floor)))
            absolute_alloc_pct = _min_alloc_pct + _strength_scaled * (_max_alloc_pct - _min_alloc_pct)
            allocation_pct = min(raw_ratio, absolute_alloc_pct, _max_alloc_pct)

            weight_label = f"{allocation_pct*100:.1f}%"
            logger.info(f"⚖️ [Allocation_Ratio] {sym} 強度 {strength:.1f} (原始佔比 {raw_ratio*100:.1f}%, 絕對強度換算上限 {absolute_alloc_pct*100:.1f}%)，實際分配資金為: {weight_label}")
            if not has_pos:
                logger.info(f"🛒 [ENTRY_DISPATCH] {sym} 將進入 execute_order | side={side} route={route} strength={strength:.2f} allocation={allocation_pct:.3f}")

            async def _entry_task(sym, side, price, alloc_pct, signal_strength, entry_route):
                try:
                    order_data = await execute_order(sym, side, price, alloc_pct,
                                                      signal_strength=signal_strength,
                                                      entry_route=entry_route)
                    if order_data and order_data.get("avgPrice") and order_data.get("filledQty"):
                        from core.state_manager import update_state_with_fill
                        update_state_with_fill(sym, order_data)
                        # Ensure last_entry metadata is updated with actual filled data
                        s = ctx.STATES[sym]
                        s["last_entry_price"] = float(order_data.get("avgPrice"))
                        s["last_entry_direction"] = side if side == "buy" else "sell"
                except Exception as e:
                    logger.error(f"🚨 [EntryTask_Error] {sym}: {e}")
                finally:
                    ctx.STATES[sym]["is_ordering"] = False

            asyncio.create_task(_entry_task(sym, side, s["close_price"], allocation_pct, strength, route))

        s["pending_side"] = None
        s["pending_confirm_high"] = 0
        s["pending_confirm_low"] = 0


def is_entry_candidate_still_valid(sym, side, route, strength, signal_price=0.0):
    """Revalidate a delayed entry against the latest direction and risk state."""
    s = ctx.STATES.get(sym)
    if not s:
        return False, "missing state"

    current_price = float(s.get("close_price", 0.0) or 0.0)
    reference_price = float(signal_price or current_price)
    if current_price <= 0 or reference_price <= 0:
        return False, "invalid price"

    atr = float(s.get("current_atr", 0.0) or 0.0)
    adverse_limit = max(reference_price * 0.0025, atr * 0.5)
    adverse_move = reference_price - current_price if side == "buy" else current_price - reference_price
    if adverse_move > adverse_limit:
        return False, (
            f"price moved adverse {adverse_move/reference_price*100:.2f}% "
            f"(limit {adverse_limit/reference_price*100:.2f}%)"
        )

    divergence = s.get("divergence", "none")
    if side == "buy" and divergence == "bearish":
        return False, "bearish divergence"
    if side == "sell" and divergence == "bullish":
        return False, "bullish divergence"

    if route != "Automatic_Reverse":
        refreshed = compute_signal_strength(sym)
        if not refreshed or refreshed[0] != side:
            return False, f"latest signal no longer supports {side}"

    return True, "ok"