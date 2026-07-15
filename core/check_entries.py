import logging
import asyncio
import os
import json
import time
import numpy as np
from datetime import datetime, timezone

from core import ctx
from core.config import (COIN_PROFILE_CONFIG, DEFAULT_NEW_COIN_PROFILE,
    DUAL_SHOT_MIN_PROFIT_ROOM, RSI_PERIOD, DAILY_LOSS_LIMIT_PCT,
    DEFAULT_LOSS_REENTRY_COOLDOWN_SEC, get_entry_strictness_profile)
from core.indicators import (_get_atr, calculate_ema, calculate_macd,
    calculate_adx, calculate_bollinger_bands, _calc_sl_tp)
from core.balance import is_daily_loss_halted
import core.balance as _bal
from core.state_manager import get_open_position_count, reset_coin_state
from core.signal_engine import compute_signal_strength, _load_disabled_symbols
from core.entry_filter import btc_macro_entry_guard, is_entry_allowed
from services.bot_manager_service import set_entry_diagnosis

logger = logging.getLogger(__name__)

_TRADE_HISTORY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trade_history.json")
_LOSS_HISTORY_CACHE_MTIME = None
_LOSS_HISTORY_CACHE = {}


def _entry_structure_quality(sym, side, route, price):
    """Validate entry against the nearest 20-candle support/resistance and score its room."""
    s = ctx.STATES.get(sym, {})
    candles = s.get("ohlcv", [])
    price = float(price or 0.0)
    atr = float(s.get("current_atr", 0.0) or 0.0)
    prior = candles[-22:-2] if len(candles) >= 22 else candles[:-2]
    if price <= 0 or atr <= 0 or len(prior) < 10:
        return False, "支撐/阻力或 ATR 資料不足", 0.0

    resistance = max(float(c[2]) for c in prior)
    support = min(float(c[3]) for c in prior)
    min_room = max(price * 0.006, atr * 1.2)
    max_breakout_extension = max(price * 0.0015, atr * 0.75)
    s["_entry_support"] = support
    s["_entry_resistance"] = resistance

    if side == "buy":
        if price <= resistance:
            room = resistance - price
            valid = room >= min_room
            reason = f"多單距上方阻力僅 {room/price*100:.2f}%" if not valid else "long_room_ok"
            score = min(room / min_room, 3.0) * 3.0
        else:
            extension = price - resistance
            valid = extension <= max_breakout_extension
            reason = f"多單突破後延伸 {extension/price*100:.2f}% 過遠" if not valid else "long_breakout_ok"
            score = max(0.0, 8.0 - extension / max_breakout_extension * 4.0)
    else:
        if price >= support:
            room = price - support
            valid = room >= min_room
            reason = f"空單距下方支撐僅 {room/price*100:.2f}%" if not valid else "short_room_ok"
            score = min(room / min_room, 3.0) * 3.0
        else:
            extension = support - price
            valid = extension <= max_breakout_extension
            reason = f"空單跌破後延伸 {extension/price*100:.2f}% 過遠" if not valid else "short_breakout_ok"
            score = max(0.0, 8.0 - extension / max_breakout_extension * 4.0)

    s["_entry_structure_score"] = round(score, 4)
    return valid, reason, score


def _ma_candidate_quality(sym, side, strength, route, price):
    s = ctx.STATES[sym]
    structure_ok, structure_reason, structure_score = _entry_structure_quality(sym, side, route, price)
    if not structure_ok:
        return False, structure_reason, 0.0
    candles = s.get("ohlcv", [])
    closed_volume = float(candles[-2][5]) if len(candles) >= 2 else 0.0
    vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)
    volume_ratio = closed_volume / vol_ma20 if vol_ma20 > 0 else 0.0
    atr = float(s.get("current_atr", 0.0) or 0.0)
    ma_gap = abs(float(s.get("ma7", 0.0) or 0.0) - float(s.get("ma25", 0.0) or 0.0))
    gap_score = min(ma_gap / atr, 2.0) * 3.0 if atr > 0 else 0.0
    volume_score = min(volume_ratio, 2.5) * 3.0
    route_bonus = {"MA25_Pullback": 4.0, "MA_Cross": 3.0, "MA_Breakout": 2.0}.get(route, 0.0)
    quality = float(strength) + structure_score + gap_score + volume_score + route_bonus
    return True, "ok", round(quality, 4)


async def _funding_rate_guard(sym, side):
    """Cache funding and reject entries whose next payment is exceptionally adverse."""
    import sys
    s = ctx.STATES[sym]
    if "unittest" in sys.modules or os.getenv("TESTING") == "true":
        s["_expected_funding_cost_pct"] = 0.0
        return True, "test"
    now = time.time()
    rate = s.get("funding_rate")
    if rate is None or now - float(s.get("funding_rate_updated_at", 0.0) or 0.0) >= 900:
        try:
            from core.exchange_client import exchange_market_data
            data = await exchange_market_data.fetch_funding_rate(sym)
            info = data.get("info", {}) if isinstance(data, dict) else {}
            rate = float((data or {}).get("fundingRate") or info.get("lastFundingRate") or 0.0)
            s["funding_rate"] = rate
            s["funding_rate_updated_at"] = now
        except Exception as exc:
            logger.info(f"⚠️ [FundingRate] {sym} 取得資金費率失敗，沿用快取：{exc}")
            rate = float(rate or 0.0)
    adverse_rate = max(float(rate), 0.0) if side == "buy" else max(-float(rate), 0.0)
    s["_expected_funding_cost_pct"] = adverse_rate
    if adverse_rate >= 0.001:
        payer = "多單" if side == "buy" else "空單"
        return False, f"{payer}預計支付資金費率 {adverse_rate*100:.3f}%／期，成本過高"
    return True, f"funding={float(rate)*100:.4f}%"


def _load_history_loss_times():
    """Return latest losing close time by (symbol, side), surviving bot restarts."""
    global _LOSS_HISTORY_CACHE_MTIME, _LOSS_HISTORY_CACHE
    try:
        mtime = os.path.getmtime(_TRADE_HISTORY_PATH)
        if mtime == _LOSS_HISTORY_CACHE_MTIME:
            return _LOSS_HISTORY_CACHE
        with open(_TRADE_HISTORY_PATH, "r", encoding="utf-8") as fh:
            history = json.load(fh)
        result = {}
        for trade in history if isinstance(history, list) else []:
            if float(trade.get("profit_pct", 0.0) or 0.0) >= -0.001:
                continue
            entry = float(trade.get("actual_entry", 0.0) or 0.0)
            exit_price = float(trade.get("actual_exit", 0.0) or 0.0)
            if entry <= 0 or exit_price <= 0 or entry == exit_price:
                continue
            # 虧損交易可由價差反推方向：出場低於進場是多單，反之是空單。
            side = "buy" if exit_price < entry else "sell"
            timestamp = trade.get("timestamp")
            try:
                closed_at = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except (TypeError, ValueError):
                continue
            symbol = str(trade.get("symbol", "")).replace(":", "").upper()
            key = (symbol, side)
            result[key] = max(result.get(key, 0.0), closed_at)
        _LOSS_HISTORY_CACHE_MTIME = mtime
        _LOSS_HISTORY_CACHE = result
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return _LOSS_HISTORY_CACHE


def get_last_same_side_loss_time(sym, side, state_time=0.0):
    history_time = _load_history_loss_times().get((sym.upper(), side), 0.0)
    return max(float(state_time or 0.0), float(history_time or 0.0))



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

    # MA lifecycle uses closed candles only. The last OHLCV row is the live 5m
    # candle and must not move MA7/25/99 (or their crossover state) every tick.
    completed_closes = closes[:-1]
    if len(completed_closes) >= 100:
        s["ma7"] = float(np.mean(completed_closes[-7:]))
        s["ma25"] = float(np.mean(completed_closes[-25:]))
        s["ma99"] = float(np.mean(completed_closes[-99:]))
        s["prev_ma7"] = float(np.mean(completed_closes[-8:-1]))
        s["prev_ma25"] = float(np.mean(completed_closes[-26:-1]))
        s["prev_ma99"] = float(np.mean(completed_closes[-100:-1]))
        s["ma_candle_ts"] = int(ohlcv[-2][0])
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
            # 顯示用 RSI 上限設為 99，避免無限值。
            s["current_rsi"] = min(99.0, 100.0 - (100.0 / (1.0 + rs)))
        elif np.any(deltas > 0):
            s["current_rsi"] = 99.0  # 期間內全為漲K，但不等同真正超買
        else:
            s["current_rsi"] = 50.0  # 無波動
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



async def check_entries():
    from core.orders import execute_order

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

        if s["status"] != "ACTIVE":
            continue

        has_position = abs(s["qty"]) > 0.000001
        current_direction = "buy" if s["qty"] > 0 else "sell" if s["qty"] < 0 else None

        # 雷達監控池與可交易池分離。既有持倉仍正常管理；只有新開倉會被觀察期攔截。
        if not has_position:
            from core.symbol_profile import SYMBOL_PROFILES
            _radar_profile = SYMBOL_PROFILES.get(sym, {})
            if _radar_profile and not bool(_radar_profile.get("_trade_eligible", False)):
                _eligibility_reason = _radar_profile.get("_trade_eligibility_reason", "雷達觀察中")
                set_entry_diagnosis(f"{sym}: {_eligibility_reason}")
                continue

        # 開倉錯誤冷卻（例如幣安 -1007 送出狀態未知）：確認交易所端真的沒有新倉位後，
        # 短暫暫停這個幣種，避免立刻用同樣的條件反覆撞在同一個逾時問題上。
        if not has_position and time.time() < s.get("order_fail_cooldown_until", 0):
            continue

        # 開倉數限制 (針對新開倉)
        if not has_position and open_count >= dynamic_max_positions:
            continue

        # MA25 回調由已收線 MA 路由直接產生，不再使用舊 EMA20 等待佇列。

        current_candle_time = s["ohlcv"][-1][0] if s["ohlcv"] else 0

        # 原本的計算邏輯
        side_strength = compute_signal_strength(sym)
        if side_strength is None or side_strength[0] is None:
            block_reason = s.get("entry_block_reason") or "暫無有效訊號"
            set_entry_diagnosis(f"{sym}: {block_reason}")
            continue
        side, strength, route = side_strength

        macro_ok, macro_reason, macro_mode = btc_macro_entry_guard(sym, side)
        s["_btc_macro_mode"] = macro_mode
        if not macro_ok:
            s["entry_block_reason"] = macro_reason
            set_entry_diagnosis(f"{sym}: {macro_reason}")
            logger.info(f"🛑 [BTC_Macro_Guard] {sym} {side}：{macro_reason}")
            continue

        funding_ok, funding_reason = await _funding_rate_guard(sym, side)
        if not funding_ok:
            s["entry_block_reason"] = funding_reason
            set_entry_diagnosis(f"{sym}: {funding_reason}")
            logger.info(f"🛑 [FundingRate_Block] {sym} {funding_reason}")
            continue

        # [Layer 0] 每幣種最低信號強度門檻
        profile = get_entry_strictness_profile()
        coin_profile_min_sig = COIN_PROFILE_CONFIG.get(sym, DEFAULT_NEW_COIN_PROFILE).get("min_signal_strength", 20.0)
        # 原本用 min() 取兩者較低的門檻，等於嚴格模式的全域門檻(15.0)永遠蓋掉個別幣種
        # 特別調高的門檻（今天稍早才把主力幣/新幣門檻拉高到 18~24，min() 卻讓實際生效
        # 門檻一直卡在 15.0，等於那次調整從未真正生效）。改成 max()，兩個門檻都當作
        # 下限，用較嚴格的那個，個別幣種調高的門檻才會真正生效。
        min_sig = max(coin_profile_min_sig, profile.get("min_signal_strength", 10.0))
        # 當整體環境處於寬鬆模式時，我們應該真的放寬個別幣種的門檻，而不是卡死在 max()
        if profile.get("min_signal_strength", 15.0) < 15.0:
            # 依據 profile 放寬的程度等比例降低幣種門檻
            reduction = 15.0 - profile.get("min_signal_strength", 15.0)
            min_sig = max(coin_profile_min_sig - reduction, profile.get("min_signal_strength", 10.0), 6.0)
        # 大盤盤整（BTC 1H ADX 過低、沒有明確趨勢）時，動能型多空訊號普遍缺乏後續動能，
        # 今天實測 AVAX/TRX/DOT/WLD/LINK 好幾筆都是這個情況：峰值都在 0.5% 以下就陰跌
        # 打平出場。盤整期間拉高門檻，減少這種訊號品質不足以撐過盤整雜訊的進場。
        if ctx.MARKET_WIND.get("is_ranging"):
            min_sig += 5.0
        if macro_mode == "MIXED":
            min_sig += 3.0
        if strength < min_sig:
            set_entry_diagnosis(f"{sym}: 強度 {strength:.1f} < 門檻 {min_sig:.1f}")
            continue

        # --- 2. 多重共振過濾區塊 (Multi-Confluence Entry Filter) ---
        cp = s["close_price"]
        ema50_1h = s.get("ema50_1h", 0)

        vol_ma20 = s.get("vol_ma20", 0.0)
        volume = s["ohlcv"][-2][5] if len(s["ohlcv"]) > 1 else (s["ohlcv"][-1][5] if len(s["ohlcv"]) > 0 else 0)
        # 無條件存到 state（不管走哪個 route），供後面第二輪分配資金時依流動性打折用；
        # 每輪都重新算，不會有上一輪殘留的舊值被下一個候選誤用。
        s["_entry_liquidity_usdt"] = vol_ma20 * cp * 288

        # A. 數據完整性檢查
        if vol_ma20 == 0:
            set_entry_diagnosis(f"{sym}: 指標載入中 (VolMA20: {vol_ma20})")
            continue

        # MA 策略只保留成交量、流動性與 ATR 通用風控。

        # D. 真實性驗證 (Volume Confirmation) - 動態門檻
        _atr_hist_ce = s.get("atr_history", [])
        _atr_avg_ce = float(np.mean(_atr_hist_ce)) if len(_atr_hist_ce) > 0 else 0.0
        _atr_cur_ce = s.get("current_atr", 0.0)
        _is_low_vol_ce = (_atr_avg_ce > 0 and _atr_cur_ce <= _atr_avg_ce)
        # 已收盤 K 棒的量能確認。17 分以上已有方向、動能等多重共振，
        # 量能門檻放寬至均量 45%；一般訊號仍需 55%~65%，避免無量假突破。
        _strong_participation_strength = 17.0
        _d_multiplier = 0.45 if strength >= _strong_participation_strength else (0.55 if _is_low_vol_ce else 0.65)
        if volume < (vol_ma20 * _d_multiplier):
            s["low_participation_streak"] = s.get("low_participation_streak", 0) + 1
            logger.info(f"🛑 [CONFLUENCE_FAIL] {sym}: 量能極度不足 (當前量 {volume:.0f} < 均量 {vol_ma20:.0f} * {_d_multiplier})")
            set_entry_diagnosis(f"{sym}: 量能不足，無法進場")
            continue

        # E. 參與度過濾 (Participation Filter)
        profile = get_entry_strictness_profile()
        if len(s["ohlcv"]) > 1:
            current_vol = volume  # 已是 ohlcv[-2]
            prev_vol = s["ohlcv"][-3][5] if len(s["ohlcv"]) > 2 else s["ohlcv"][-2][5]
            price_change = cp - s["ohlcv"][-2][1]

            _rvol_multiplier = 0.45 if strength >= _strong_participation_strength else (0.55 if _is_low_vol_ce else 0.65)
            rvol_check = current_vol > (vol_ma20 * _rvol_multiplier)

            h24_quote_volume_est = vol_ma20 * cp * 288
            liquidity_check = h24_quote_volume_est > 1000000

            candle_open = s["ohlcv"][-2][1]
            candle_close = s["ohlcv"][-2][4]
            direction_ok = candle_close > candle_open if side == "buy" else candle_close < candle_open
            volume_price_sync = direction_ok and current_vol >= prev_vol * 0.70

            if route in ("MA_Cross", "MA_Breakout", "MA25_Pullback"):
                if not liquidity_check and profile.get("min_signal_strength", 10.0) > 10.0:
                    s["low_participation_streak"] = s.get("low_participation_streak", 0) + 1
                    logger.info(f"🛑 [LOW_PARTICIPATION] {sym} 被攔截：流動性不足 (估算24H交易額: {h24_quote_volume_est:,.0f} < 1,000,000)")
                    set_entry_diagnosis(f"{sym}: 流動性不足，放棄進場")
                    continue
                if not rvol_check and profile.get("min_signal_strength", 10.0) > 10.0:
                    _rvol_pct = int(_rvol_multiplier * 100)
                    s["low_participation_streak"] = s.get("low_participation_streak", 0) + 1
                    logger.info(f"🛑 [LOW_PARTICIPATION] {sym} 被攔截：量能爆發不足 (目前 {current_vol:.0f} 未達均量 {_rvol_pct}% | {'低波動放寬' if _is_low_vol_ce else '高波動嚴格'})")
                    set_entry_diagnosis(f"{sym}: 量能爆發不足，放棄進場")
                    continue
                if not volume_price_sync:
                    strong_volume_override = strength >= _strong_participation_strength and current_vol >= vol_ma20 * 0.45
                    if not strong_volume_override:
                        s["low_participation_streak"] = s.get("low_participation_streak", 0) + 1
                        logger.info(f"🛑 [LOW_PARTICIPATION] {sym} 量價不協同，無跟進量支持，放棄進場")
                        set_entry_diagnosis(f"{sym}: 量價不協同，放棄進場")
                        continue
                    logger.info(f"⚡ [VOLUME_OVERRIDE] {sym} 強度 {strength:.1f} 且量能達均量 0.45x，允許進場")

        s["low_participation_streak"] = 0
        _force_close_confirmation = False

        # E2. 即時 5m 波動底線：日 ATR 高不代表現在有行情，避免選到當下死水幣。
        _atr_pct_5m = (_atr_cur_ce / cp) if cp > 0 else 0.0
        if _atr_pct_5m < 0.0012:
            logger.info(f"🛑 [SLOW_MARKET] {sym} 5m ATR 僅 {_atr_pct_5m*100:.3f}% < 0.12%，放棄進場")
            set_entry_diagnosis(f"{sym}: 即時波動不足，放棄進場")
            continue

        logger.info(f"✅ [MA_RISK_PASS] {sym}: {side} MA 與通用風控通過 (Route: {route})")
        logger.info(f"🧭 [ENTRY_GATE] {sym} 進入最後進場檢查 | side={side} route={route} strength={strength:.2f}")

        # 已有持倉只由 MA 生命週期與硬停損管理，不建立反手新倉。
        if has_position:
            continue

        if not is_entry_allowed(sym, side, route, strength):
            continue

        # 同方向虧損後維持冷卻，避免重複使用同一個失效 MA 波段。
        _loss_reentry_cooldown = float(
            COIN_PROFILE_CONFIG.get(sym, {}).get(
                "loss_reentry_cooldown_sec", DEFAULT_LOSS_REENTRY_COOLDOWN_SEC
            ) or 0.0
        )
        _state_loss_time = s.get(
            "last_loss_time_long" if side == "buy" else "last_loss_time_short", 0.0
        )
        _same_side_loss_time = get_last_same_side_loss_time(
            sym, side, _state_loss_time
        )
        if (
            _loss_reentry_cooldown > 0
            and _same_side_loss_time > 0
            and time.time() - _same_side_loss_time < _loss_reentry_cooldown
        ):
            _remaining = _loss_reentry_cooldown - (time.time() - _same_side_loss_time)
            logger.info(
                f"⏳ [Loss Reentry Cooldown] {sym} 上次 {side} 虧損後同方向冷卻中，"
                f"剩餘 {_remaining / 60:.0f} 分鐘，拒絕 {route} 進場"
            )
            continue

        # --- 反手冷卻時間 (min_flip_time) 過濾 ---
        # 注意：這裡必須用機器人「自己」上一次真正進場的方向與出場時間
        # (last_entry_direction / last_exit_time)，不能用 last_trade_side /
        # last_trade_time —— 那兩個欄位是 core/trade_signal.py 從交易所「公開成交流」
        # (fetch_trades) 更新的，代表的是市場上任何人最後一筆成交的方向，跟本機器人
        # 有沒有做過交易完全無關，每幾秒就會因為別人的成交而隨機翻動，導致這個冷卻
        # 在機器人根本還沒進場過的幣種上也會誤觸發。
        last_trade_side = s.get("last_entry_direction", "")
        if last_trade_side != "" and side != last_trade_side:
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
        if last_entry_price > 0 and last_entry_dir != "":
            price_diff_pct = abs(p - last_entry_price) / last_entry_price
            if price_diff_pct < 0.003 and side != last_entry_dir:
                logger.info(f"🛑 [Filter:Choppiness] {sym} 欲 {side}，但現價 {p:.4f} 距離上次進場價 {last_entry_price:.4f} 誤差小於 0.3%，陷入原地盤整，拒絕雙巴被洗！")
                continue

        # --- R:R 盈虧比過濾 (Risk:Reward Filter) ---
        # 使用者先前要求增加開倉次數，門檻從 1.5/1.2/1.3 下修到 1.3/1.0/1.1；後來發現
        # 邊緣訊號進場後常常原地打轉、最高獲利很小就打平/小虧出場，要求拉回一點，
        # 犧牲一些開倉次數換單筆品質，改成 1.4/1.1/1.2（介於原始與寬鬆之間）。
        atr_val, sl_dist, tp_dist, expected_rr = _calc_sl_tp(sym, side, s, p, route)
        # base_rr_thresh 維持 1.2，搭配強訊號分級門檻；停利改為全倉管理後，
        # 此處只負責進場品質，不再假設有前半倉先行落袋。
        base_rr_thresh = s.get("min_rr", 1.2)

        # 使用者反映現在幾乎完全開不了倉：實測訊號強度大多落在 15~26，strength>20 才給
        # 最寬鬆 1.1 門檻的話，大部分訊號還是卡在 base_rr_thresh(1.4)~2.0。放寬斷點到
        # >14/>12，讓目前實際出現的訊號強度範圍也能吃到比較寬鬆的 R:R 門檻。
        rr_thresh = 1.1 if strength > 14.0 else (1.2 if strength > 12.0 else base_rr_thresh)
        if base_rr_thresh >= 2.0:
            rr_thresh = base_rr_thresh

        if expected_rr < rr_thresh:
            logger.info(f"🛑 [Filter:RR_Low] {sym} 預期盈虧比 {expected_rr:.2f} < {rr_thresh}，放棄暫存")
            continue

        expected_profit_pct = (tp_dist / p if p > 0 else 0) - float(s.get("_expected_funding_cost_pct", 0.0) or 0.0)
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
        if sym not in exempt_symbols and last_entry_time > 0 and (time.time() - last_entry_time) < 300:
            logger.info(f"⏳ [Flip Buffer] {sym} 訊號 {side} 被攔截 (距離上次開倉僅 {time.time() - last_entry_time:.0f}s)")
            continue

        # --- 錯誤方向禁止再進 (Wrong Direction Ban) ---
        _wd_time = s.get("wrong_dir_time", 0.0)
        _wd_side = s.get("wrong_dir_side", "")
        if _wd_side == side and time.time() - _wd_time < 300:
            logger.info(f"⏳ [Wrong Dir Ban] {sym} 同方向 {side} 剛在 {time.time()-_wd_time:.0f}s 前開錯方向，冷卻中 (5min)")
            continue

        # 訊號已由已收線 K 棒生成，直接加入候選，不再走舊二次確認路線。
        s["entry_reason"] = route
        from core.entry_reason_store import save_entry_reason
        save_entry_reason(sym, route)
        candidates.append((sym, side, strength, route))
        continue


    if not candidates:
        return

    # 候選可能來自上一根 K 的 pending 或回踩佇列；下單前重新驗證最新狀態。
    from core.symbol_profile import SYMBOL_PROFILES
    validated_candidates = []
    for sym, side, strength, route in candidates:
        s = ctx.STATES[sym]
        radar_profile = SYMBOL_PROFILES.get(sym, {})
        if s.get("status") != "ACTIVE":
            continue
        if abs(s.get("qty", 0.0)) > 0.000001:
            continue
        if radar_profile and not bool(radar_profile.get("_trade_eligible", False)):
            logger.info(f"🛑 [Final_Entry_Guard] {sym} 雷達資格已失效")
            continue
        radar_direction = radar_profile.get("_radar_entry_direction", "none")
        radar_readiness = float(radar_profile.get("_radar_entry_readiness", 0.0) or 0.0)
        expected_side = "buy" if radar_direction == "long" else "sell" if radar_direction == "short" else None
        if expected_side and radar_readiness >= 0.65 and side != expected_side:
            logger.info(f"🛑 [Final_Entry_Guard] {sym} 訊號 {side} 與雷達 {radar_direction} 不一致")
            continue
        if route not in ("MA_Cross", "MA_Breakout", "MA25_Pullback"):
            logger.info(f"🛑 [Final_Entry_Guard] {sym} 非 MA 路由已停用：{route}")
            continue
        ma_valid, ma_reason = is_entry_candidate_still_valid(sym, side, route, strength, s.get("close_price", 0.0))
        if not ma_valid:
            logger.info(f"🛑 [Final_Entry_Guard] {sym} MA 結構已失效：{ma_reason}")
            continue
        if not is_entry_allowed(sym, side, route, strength):
            continue
        price = float(s.get("close_price", 0.0) or 0.0)
        if price <= 0:
            continue
        _, _, tp_dist, latest_rr = _calc_sl_tp(sym, side, s, price, route)
        rr_floor = 1.1 if strength > 14.0 else (1.2 if strength > 12.0 else s.get("min_rr", 1.2))
        if (latest_rr < rr_floor or (tp_dist / price - float(s.get("_expected_funding_cost_pct", 0.0) or 0.0)) < 0.008):
            logger.info(f"[Final_Entry_Guard] {sym} latest RR or profit room insufficient")
            continue
        cooldown = float(COIN_PROFILE_CONFIG.get(sym, {}).get("loss_reentry_cooldown_sec", DEFAULT_LOSS_REENTRY_COOLDOWN_SEC) or 0.0)
        loss_time = get_last_same_side_loss_time(
            sym, side, s.get("last_loss_time_long" if side == "buy" else "last_loss_time_short", 0.0)
        )
        if cooldown > 0 and loss_time > 0 and time.time() - loss_time < cooldown:
            logger.info(f"🛑 [Final_Entry_Guard] {sym} 同方向虧損冷卻仍有效")
            continue
        quality_ok, quality_reason, quality_score = _ma_candidate_quality(sym, side, strength, route, price)
        if not quality_ok:
            logger.info(f"🛑 [Entry_Structure_Guard] {sym} {quality_reason}，放棄進場")
            continue
        s["_entry_quality_score"] = quality_score
        validated_candidates.append((sym, side, strength, route))

    candidates = validated_candidates
    if not candidates:
        return
    candidates.sort(key=lambda x: (-float(ctx.STATES[x[0]].get("_entry_quality_score", 0.0)), -x[2], x[0]))

    # The former range lane is removed: all three capital slots now belong to this MA strategy.
    inflight_symbols = {info.get("sym") for info in ctx.PENDING_LIMIT_ORDERS.values() if info.get("sym")}
    inflight_symbols.update(sym for sym, st in ctx.STATES.items() if st.get("is_ordering") and abs(st.get("qty", 0.0)) <= 0.000001)
    ma_capacity = max(0, 3 - open_count - len(inflight_symbols))
    remaining_slots = ma_capacity
    if remaining_slots <= 0:
        return
    logger.info(f"📊 [品質排行] {' | '.join(f'{sym}:{side}(品質={ctx.STATES[sym].get('_entry_quality_score', 0.0):.2f}, 訊號={strength:.2f})' for sym, side, strength, _ in candidates[:3])}")

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
        if remaining_slots <= 0:
            break
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

            # 流動性折扣：現有流動性檢查是二選一（過門檻 1,000,000 就全額進場、沒過就
            # 整筆擋掉），但「剛好壓線過關」跟「流動性充裕」風險完全不同，同樣全額進場
            # 不合理——薄的市場不管是進場追價還是將來急停損出場，滑點都會放大，甚至可能
            # 賣不掉（KAITOUSDT 教訓）。門檻剛過（1,000,000）打 5 折，到 3 倍門檻
            # （3,000,000）以上流動性視為充裕、不打折，中間線性插值。
            _LIQ_MIN = 1_000_000
            _LIQ_COMFORT = 3_000_000
            _liq_est = s.get("_entry_liquidity_usdt")
            if _liq_est is not None and _liq_est < _LIQ_COMFORT:
                _liq_ratio = max(0.0, min(1.0, (_liq_est - _LIQ_MIN) / (_LIQ_COMFORT - _LIQ_MIN)))
                _liq_discount = 0.5 + _liq_ratio * 0.5
                if _liq_discount < 1.0:
                    allocation_pct *= _liq_discount
                    logger.info(f"⚖️ [Liquidity_Discount] {sym} 估算24H交易額 {_liq_est:,.0f} 偏薄（門檻 {_LIQ_MIN:,.0f}），倉位打折至 {_liq_discount*100:.0f}%")

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

    macro_ok, macro_reason, _ = btc_macro_entry_guard(sym, side)
    if not macro_ok:
        return False, macro_reason

    atr = float(s.get("current_atr", 0.0) or 0.0)
    adverse_limit = max(reference_price * 0.0025, atr * 0.5)
    adverse_move = reference_price - current_price if side == "buy" else current_price - reference_price
    if adverse_move > adverse_limit:
        return False, (
            f"price moved adverse {adverse_move/reference_price*100:.2f}% "
            f"(limit {adverse_limit/reference_price*100:.2f}%)"
        )

    if route not in ("MA_Cross", "MA_Breakout", "MA25_Pullback"):
        return False, "non-MA route disabled"

    if route in ("MA_Cross", "MA_Breakout", "MA25_Pullback"):
        from core.entry_filter import is_ma_direction_aligned
        if not is_ma_direction_aligned(s, side, route):
            return False, "MA7/MA25/MA99 完整排列或斜率已失效"
        ma7 = float(s.get("ma7", 0.0) or 0.0)
        ma25 = float(s.get("ma25", 0.0) or 0.0)
        ma99 = float(s.get("ma99", 0.0) or 0.0)
        closed_price = float(s.get("ohlcv", [])[-2][4]) if len(s.get("ohlcv", [])) >= 2 else 0.0
        if side == "buy" and not (closed_price > ma99 and ma7 > ma25):
            return False, "多單不再符合 收盤價>MA99 且 MA7>MA25"
        if side == "sell" and not (closed_price < ma99 and ma7 < ma25):
            return False, "空單不再符合 收盤價<MA99 且 MA7<MA25"
    return True, "ok"
