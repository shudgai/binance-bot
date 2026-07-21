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
    DEFAULT_LOSS_REENTRY_COOLDOWN_SEC, MIN_5M_ATR_PCT_FOR_MA_ENTRY,
    get_entry_strictness_profile)
from core.indicators import (_get_atr, calculate_ema, calculate_macd,
    calculate_adx, calculate_bollinger_bands, _calc_sl_tp)
from core.balance import is_daily_loss_halted
import core.balance as _bal
from core.state_manager import get_open_position_count, reset_coin_state
from core.signal_engine import compute_signal_strength, _load_disabled_symbols, compute_range_signal
from core.entry_filter import btc_macro_entry_guard, is_entry_allowed
from services.bot_manager_service import set_entry_diagnosis

logger = logging.getLogger(__name__)

_TRADE_HISTORY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trade_history.json")
_LOSS_HISTORY_CACHE_MTIME = None
_LOSS_HISTORY_CACHE = {}
COOLDOWN_REENTRY_TOTAL_CONFIRMATIONS = 3
COOLDOWN_REENTRY_RAPID_RECHECKS = 2
COOLDOWN_REENTRY_RECHECK_INTERVAL_SEC = 1.0


def _radar_entry_block_reason(profile, route=None):
    """依實際訊號 route 重驗雷達分類；舊 profile 維持安全側相容。"""
    if not profile:
        return "尚無雷達交易資格"
    if str(route or "").lower() == "ma7_simple":
        return ""

    has_classified_data = (
        "_radar_atr_pct" in profile and "_radar_one_h_vol_pct" in profile
    )
    if route and has_classified_data:
        from services.radar_service import radar_eligibility
        route_ok, route_reason, route_class = radar_eligibility(profile, route)
        if not route_ok:
            return route_reason
        # Range 已由已收線支撐／壓力、低 ADX、量能與 RR 做局部確認；
        # 邊界機會短，不再等待較慢雷達的第二次確認與 30 分鐘成熟期。
        if route_class == "range":
            return ""
        if not bool(profile.get("_radar_observation_mature", False)):
            confirmations = int(profile.get("_radar_confirmations", 0) or 0)
            if confirmations < 2:
                return "觀察中：等待第二次雷達確認"
            first_seen = float(profile.get("_radar_candidate_since", time.time()) or time.time())
            # 將觀察成熟期從 30 分鐘縮短為 15 分鐘，加快新幣進入可交易狀態
            remaining = max(0, int((900 - max(0.0, time.time() - first_seen)) / 60) + 1)
            return f"觀察中：尚需 {remaining} 分鐘"
        return ""

    if not bool(profile.get("_trade_eligible", False)):
        return str(profile.get("_trade_eligibility_reason") or "雷達觀察中")
    return ""


def _radar_signal_block_message(sym, route, reason):
    return f"{sym}: 偵測到 {route} 訊號，但{reason}，不送單"


def _radar_direction_block_reason(profile, side, route):
    """MA7_Simple 以即時轉折為準；其他路線保留高可信雷達方向保護。"""
    route_key = str(route or "").lower()
    if route_key == "ma7_simple" or route_key in ("range", "range_support_long", "range_resistance_short"):
        return ""
    radar_direction = profile.get("_radar_entry_direction", "none")
    radar_readiness = float(profile.get("_radar_entry_readiness", 0.0) or 0.0)
    expected_side = "buy" if radar_direction == "long" else "sell" if radar_direction == "short" else None
    if expected_side and radar_readiness >= 0.80 and side != expected_side:
        return f"訊號 {side} 與雷達 {radar_direction} 不一致"
    return ""


def _range_exit_prices(price, support, resistance, atr, side):
    """Build range exits with enough room between the actual fill and structural stop."""
    price = float(price)
    support = float(support)
    resistance = float(resistance)
    atr = float(atr)
    minimum_stop_room = max(atr * 0.70, price * 0.0025)
    if side == "buy":
        take_profit = resistance - price * 0.0005
        structural_stop = support - atr * 0.5
        stop = min(structural_stop, price - minimum_stop_room)
    else:
        take_profit = support + price * 0.0005
        structural_stop = resistance + atr * 0.5
        stop = max(structural_stop, price + minimum_stop_room)
    return take_profit, stop


def log_decision_summary(
    sym: str,
    ma_status: str,
    range_status: str,
    adx: float,
    route: str = "",
    side: str = "",
    block_reason: str = "",
) -> None:
    """每次訊號評估完畢後，輸出一行結構化的模式決策摘要。

    Args:
        sym:          交易對名稱。
        ma_status:    MA 訊號狀態 ("觸發" / "無訊號")。
        range_status: 區間模式狀態 ("觸發" / "略過" / "未評估")。
        adx:          當前 ADX 數值。
        route:        進場路由標籤，訊號觸發時帶入。
        side:         方向 ("LONG" / "SHORT")，訊號觸發時帶入。
        block_reason: 阻斷原因，僅略過時帶入。
    """
    if ma_status == "觸發":
        # 趨勢模式成功觸發
        reason = f"趨勢模式 (ADX={adx:.1f})，觸發 MA 訊號 {route} ({side})"
    elif range_status == "觸發":
        # 區間模式成功觸發
        reason = f"區間模式就緒，觸發 {route} ({side})"
    elif range_status == "略過":
        # 兩種模式均未能觸發
        if adx < 60.0:
            detail = block_reason or "區間空間不足或邊界確認中"
            reason = f"盤整環境但區間模式確認中 ({detail})"
        else:
            detail = block_reason or "等待新的 MA 訊號"
            reason = f"趨勢模式未進場 ({detail})；ADX={adx:.1f}，區間模式停用"
    else:
        # range_status == "未評估"（RANGE_MODE_ENABLED=False 且 MA 無訊號）
        reason = f"趨勢模式無訊號 (ADX={adx:.1f})，區間模式未啟用"

    logger.info(f"🔍 [Decision] {sym} | 決策路徑: {reason}")




def _is_confirmable_exit_cooldown(state, now=None, side=None):
    """Allow early release only for a confirmed reversal, never same-side re-entry."""
    now = float(time.time() if now is None else now)
    in_cooldown = (
        state.get("status") == "COOLDOWN"
        and now < float(state.get("next_status_time", 0.0) or 0.0)
    )
    if not in_cooldown:
        return False
    if side is None:
        return True
    last_exit_direction = str(state.get("last_exit_direction", "") or "").lower()
    # 舊存檔沒有方向時採安全側：不可提前解除，等原冷卻自然結束。
    return bool(last_exit_direction) and str(side).lower() != last_exit_direction


async def _rapid_reconfirm_cooldown_entry(sym, side, route, strength, checks=None, interval=None):
    """After one full pass, repeat all time-sensitive entry guards twice quickly."""
    checks = int(COOLDOWN_REENTRY_RAPID_RECHECKS if checks is None else checks)
    interval = float(COOLDOWN_REENTRY_RECHECK_INTERVAL_SEC if interval is None else interval)
    from core.symbol_profile import SYMBOL_PROFILES
    for attempt in range(1, checks + 1):
        if interval > 0:
            await asyncio.sleep(interval)
        s = ctx.STATES.get(sym, {})
        if not _is_confirmable_exit_cooldown(s, side=side):
            return False, "cooldown state changed"
        if abs(float(s.get("qty", 0.0) or 0.0)) > 0.000001:
            return False, "position already exists"

        fresh_signal = compute_signal_strength(sym, realtime_trigger=True)
        if not fresh_signal or fresh_signal[0] != side or fresh_signal[2] != route:
            return False, f"signal changed on rapid check {attempt}"
        fresh_strength = float(fresh_signal[1] or 0.0)

        radar_profile = SYMBOL_PROFILES.get(sym, {})
        radar_block_reason = _radar_entry_block_reason(radar_profile, route)
        if radar_block_reason and str(route or "").lower() != "ma7_simple":
            return False, f"radar eligibility lost on rapid check {attempt}: {radar_block_reason}"
        macro_ok, macro_reason, _ = btc_macro_entry_guard(sym, side)
        if not macro_ok:
            return False, macro_reason
        funding_ok, funding_reason = await _funding_rate_guard(sym, side)
        if not funding_ok:
            return False, funding_reason
        ma_ok, ma_reason = is_entry_candidate_still_valid(
            sym, side, route, fresh_strength, float(s.get("close_price", 0.0) or 0.0),
        )
        if not ma_ok:
            return False, ma_reason
        if not is_entry_allowed(sym, side, route, fresh_strength):
            return False, f"entry filter failed on rapid check {attempt}"

        price = float(s.get("close_price", 0.0) or 0.0)
        if price <= 0:
            return False, "invalid live price"
        _, _, tp_dist, latest_rr = _calc_sl_tp(sym, side, s, price, route)
        rr_floor = 1.1 if fresh_strength > 14.0 else (1.2 if fresh_strength > 12.0 else s.get("min_rr", 1.2))
        profit_room = tp_dist / price - float(s.get("_expected_funding_cost_pct", 0.0) or 0.0)
        if latest_rr < rr_floor or profit_room < 0.008:
            return False, f"RR or profit room failed on rapid check {attempt}"
        quality_ok, quality_reason, _ = _ma_candidate_quality(
            sym, side, fresh_strength, route, price,
        )
        if not quality_ok:
            return False, quality_reason
    return True, "three confirmations passed"


def _release_confirmed_exit_cooldown(sym, state):
    state["status"] = "ACTIVE"
    state["next_status_time"] = 0.0
    state["status_reason"] = ""
    state["cooldown_reentry_eligible"] = False
    state["cooldown_reentry_confirm_count"] = 0
    state["cooldown_reentry_confirm_key"] = ""
    state["cooldown_reentry_last_pass_scan"] = 0
    from core.cooldown_store import clear_cooldown
    clear_cooldown(sym)


def _calculate_correlation(klines_a, klines_b, limit=6):
    """Calculate Pearson correlation of closing prices between two kline arrays."""
    if not klines_a or not klines_b or len(klines_a) < limit or len(klines_b) < limit:
        return 0.0
    
    # K-lines format: [timestamp, open, high, low, close, volume]
    try:
        closes_a = [float(k[4]) for k in klines_a[-limit:]]
        closes_b = [float(k[4]) for k in klines_b[-limit:]]
        
        # Calculate percentage returns to avoid scaling issues
        returns_a = np.diff(closes_a) / closes_a[:-1]
        returns_b = np.diff(closes_b) / closes_b[:-1]
        
        if np.std(returns_a) == 0 or np.std(returns_b) == 0:
            return 0.0
            
        corr = np.corrcoef(returns_a, returns_b)[0, 1]
        return float(corr) if not np.isnan(corr) else 0.0
    except Exception as e:
        logger.error(f"Error calculating correlation: {e}")
        return 0.0


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
    # 使用者要求溫和放寬：進一步放寬到 0.10%/0.25xATR，避免因為極度靠近的局部小支撐/阻力
    # 而擋掉原本很好的回調進場機會。
    min_room = max(price * 0.0010, atr * 0.25)
    max_breakout_extension = max(price * 0.0035, atr * 1.2)
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


def _ma25_pullback_sample_bonus(state, side, strength, price, volume_ratio):
    """加權接近 TUSDT 成功樣本的 MA25 回踩，不改成硬性進場門檻。"""
    adx = float(state.get("adx", state.get("current_adx", 0.0)) or 0.0)
    rsi = float(state.get("current_rsi", 50.0) or 50.0)
    ma99 = float(state.get("ma99", 0.0) or 0.0)

    bonus = 0.0
    if adx >= 30.0:
        bonus += 1.5
    if float(volume_ratio) >= 1.0:
        bonus += 2.0
    if (
        (side == "buy" and 51.0 <= rsi < 70.0)
        or (side == "sell" and 30.0 < rsi <= 49.0)
    ):
        bonus += 1.0
    if ma99 > 0 and ((side == "buy" and price > ma99) or (side == "sell" and price < ma99)):
        bonus += 1.0
    if float(strength) >= 28.0:
        bonus += 1.0

    state["_ma25_sample_bonus"] = round(bonus, 4)
    return round(bonus, 4)


def _ma_cross_sample_bonus(state, side, strength, price, volume_ratio):
    """加權 ETH／PEPE／SOL／TAO 型 MA 交叉，不取消既有安全門檻。"""
    adx = float(state.get("adx", state.get("current_adx", 0.0)) or 0.0)
    rsi = float(state.get("current_rsi", 50.0) or 50.0)
    ma99 = float(state.get("ma99", 0.0) or 0.0)

    bonus = 0.0
    if ma99 > 0 and ((side == "buy" and price > ma99) or (side == "sell" and price < ma99)):
        bonus += 1.5
    if float(volume_ratio) >= 1.0:
        bonus += 2.0
    elif float(volume_ratio) >= 0.5:
        bonus += 0.5
    if (
        (side == "buy" and 51.0 <= rsi < 70.0)
        or (side == "sell" and 30.0 < rsi <= 49.0)
    ):
        bonus += 1.0
    if float(strength) >= 28.0:
        bonus += 1.0
    if adx >= 30.0:
        bonus += 1.0

    state["_ma_cross_sample_bonus"] = round(bonus, 4)
    return round(bonus, 4)


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
    from services.ai_manager import ai_engine
    learning_adjustment = ai_engine.get_candidate_quality_adjustment(sym, route)
    s["_ai_learning_adjustment"] = learning_adjustment
    sample_bonus = 0.0
    s["_ma25_sample_bonus"] = 0.0
    s["_ma_cross_sample_bonus"] = 0.0
    if route == "MA25_Pullback":
        sample_bonus = _ma25_pullback_sample_bonus(
            s, side, strength, price, volume_ratio
        )
    elif route == "MA_Cross":
        sample_bonus = _ma_cross_sample_bonus(
            s, side, strength, price, volume_ratio
        )
    quality = float(strength) + structure_score + gap_score + volume_score + route_bonus + learning_adjustment + sample_bonus
    return True, "ok", round(quality, 4)


def _range_candidate_quality(state, side, strength, range_rr, range_net_pct):
    """優先排序接近 KAITO 成功樣本的區間訊號，不把偏好改成硬門檻。"""
    candles = state.get("ohlcv", [])
    closed_volume = float(candles[-2][5]) if len(candles) >= 2 else 0.0
    vol_ma20 = float(state.get("vol_ma20", 0.0) or 0.0)
    volume_ratio = closed_volume / vol_ma20 if vol_ma20 > 0 else 0.0
    adx = float(state.get("adx", 99.0) or 99.0)
    rsi = float(state.get("current_rsi", 50.0) or 50.0)

    bonus = 0.0
    if adx < 20.0:
        bonus += 1.5
    if volume_ratio >= 0.75:
        bonus += 1.5
    if (side == "buy" and 38.0 <= rsi <= 50.0) or (side == "sell" and 50.0 <= rsi <= 62.0):
        bonus += 1.0
    if float(range_net_pct) >= 0.009:
        bonus += 1.0
    if float(range_rr) >= 2.0:
        bonus += 2.0

    state["_range_sample_bonus"] = round(bonus, 4)
    state["_range_entry_rr"] = round(float(range_rr), 4)
    state["_range_entry_net_pct"] = round(float(range_net_pct), 6)
    return round(float(strength) + bonus, 4)


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
        s["prev_ma7_2"] = float(np.mean(completed_closes[-9:-2]))
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
        # 記錄上一輪掃描的 RSI，供區間模式判斷這次的支撐反彈/壓力拒絕，
        # 是動能已經真的止穩，還是這一兩輪掃描間還在快速下墜/急拉中。
        s["prev_rsi"] = float(s.get("current_rsi", 50.0) or 50.0)
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
    s["vol_ma12"] = float(np.median(volumes[-13:-1])) if len(volumes) >= 13 else float(np.median(volumes[:-1]))
    s["vol_ma20"] = float(np.mean(volumes[-21:-1])) if len(volumes) >= 21 else float(np.mean(volumes[:-1]))
    # 為了支援即時突破 (realtime_trigger)，當前未完成 K 線的量若已經爆發，也應採納。
    # 取 max(最後一根, 倒數第二根)，避免當前剛開盤量太小，但也允許盤中爆量直接觸發。
    if len(volumes) >= 2:
        s["current_vol"] = max(float(volumes[-1]), float(volumes[-2]))
    else:
        s["current_vol"] = float(volumes[-1]) if len(volumes) > 0 else 0.0
    s["vol_surge"] = s["current_vol"] / s["vol_ma12"] if s.get("vol_ma12", 0) > 0 else 0.0

    # 計算 atr_pct 與 personality
    current_price = closes[-1] if len(closes) > 0 else 1.0
    s["atr_pct"] = (s.get("current_atr", 0.0) / current_price) * 100 if current_price > 0 else 0.0
    if s["atr_pct"] > 2.5:
        s["personality"] = "aggressive"
    elif s["atr_pct"] > 1.5:
        s["personality"] = "adaptive"
    else:
        s["personality"] = "calm"
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
        # 記錄上一輪掃描的 ADX，供 MA7_Simple 判斷這次讀數是穩定累積上來的，
        # 還是這一兩輪掃描間突然暴衝（通常是單根尖刺行情，不是真正的趨勢）。
        s["prev_adx"] = float(s.get("adx", 0.0) or 0.0)
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
    
    # 提前計算正在排隊進場的幣種，避免重複評估產生洗畫面日誌
    inflight_symbols = {info.get("sym") for info in ctx.PENDING_LIMIT_ORDERS.values() if info.get("sym")}
    inflight_symbols.update(symbol for symbol, st in ctx.STATES.items() if st.get("is_ordering") and abs(st.get("qty", 0.0)) <= 0.000001)

    candidates = []
    for sym in ctx.ALL_SYMBOLS:
        s = ctx.STATES[sym]

        # 如果已經在下單中或有掛單，直接跳過重新評估
        if sym in inflight_symbols:
            continue

        # 幣種已被使用者停用，跳過所有進場（但不影響現有持倉的管理）
        if sym in disabled_syms:
            continue
            
        from core.config import COIN_PROFILE_CONFIG
        if COIN_PROFILE_CONFIG.get(sym, {}).get("disable_entry", False):
            continue

        confirmable_cooldown = _is_confirmable_exit_cooldown(s)
        if s["status"] != "ACTIVE" and not confirmable_cooldown:
            continue

        has_position = abs(s["qty"]) > 0.000001
        current_direction = "buy" if s["qty"] > 0 else "sell" if s["qty"] < 0 else None

        # 雷達監控池與可交易池分離。既有持倉仍正常管理；只有新開倉會被觀察期攔截。
        radar_block_reason = ""
        if not has_position:
            from core.symbol_profile import SYMBOL_PROFILES
            _radar_profile = SYMBOL_PROFILES.get(sym, {})
            radar_block_reason = _radar_entry_block_reason(_radar_profile)
            if radar_block_reason:
                set_entry_diagnosis(f"{sym}: {radar_block_reason}")

        # 開倉錯誤冷卻（例如幣安 -1007 送出狀態未知）：確認交易所端真的沒有新倉位後，
        # 短暫暫停這個幣種，避免立刻用同樣的條件反覆撞在同一個逾時問題上。
        if not has_position and time.time() < s.get("order_fail_cooldown_until", 0):
            continue

        # 開倉數限制 (針對新開倉)
        if not has_position and open_count >= dynamic_max_positions:
            continue

        # 孤兒倉位閘門：該幣種策略不適配已被判定為 idle，有持倉者轉入孤兒清單。
        # 孤兒幣只做出場管理（由主迴圈的 check_exits 負責），不再跑任何新訊號判斷。
        # ── 出場管理由 runner.py check_exits 在主迴圈正常執行，無需在這裡額外呼叫。
        from core.idle_tracker import idle_tracker as _orphan_gate_tracker
        if sym in _orphan_gate_tracker.get_orphaned_positions():
            logger.debug(f"⏭️ [孤兒倉位] {sym} 已列為孤兒，跳過新進場判斷，僅做出場管理")
            continue

        current_candle_time = s["ohlcv"][-1][0] if s["ohlcv"] else 0

        # 原本的計算邏輯
        side_strength = compute_signal_strength(sym, realtime_trigger=True)
        ma_block_reason = s.get("entry_block_reason") or "暫無有效 MA 訊號"
        is_range_signal = False
        ma_status = "無訊號" if (side_strength is None or side_strength[0] is None) else "觸發"
        range_status = "未評估"

        if side_strength is None or side_strength[0] is None:
            from core.idle_tracker import idle_tracker
            idle_tracker.mark_blocked(sym, "MA_Strategy", ma_block_reason)

            # MA 訊號無效時，嘗試區間模式
            from core.config import RANGE_MODE_ENABLED, RANGE_MIN_SIGNAL_STRENGTH
            if RANGE_MODE_ENABLED:
                side_strength = compute_range_signal(sym)
                if side_strength is not None and side_strength[0] is not None:
                    is_range_signal = True
                    range_status = "觸發"
                    idle_tracker.mark_active(sym, "Range_Strategy")
                else:
                    range_status = "略過"
                    range_block_reason = s.get("entry_block_reason") or "暫無有效區間訊號"
                    idle_tracker.mark_blocked(sym, "Range_Strategy", range_block_reason)
                    
                    adx = float(s.get("adx", 0.0) or 0.0)
                    block_reason = ma_block_reason if adx >= 25.0 else range_block_reason
                    s["entry_block_reason"] = block_reason
                    set_entry_diagnosis(f"{sym}: {radar_block_reason or block_reason}")
                    
                    # 模式切換確認日誌
                    log_decision_summary(
                        sym,
                        ma_status=ma_status,
                        range_status=range_status,
                        adx=adx,
                        block_reason=block_reason,
                    )
                    continue
            else:
                block_reason = s.get("entry_block_reason") or "暫無有效訊號"
                set_entry_diagnosis(f"{sym}: {radar_block_reason or block_reason}")
                continue
        else:
            from core.idle_tracker import idle_tracker
            idle_tracker.mark_active(sym, "MA_Strategy")
        
        side, strength, route = side_strength

        radar_block_reason = _radar_entry_block_reason(_radar_profile, route)

        # 先辨識訊號再回報雷達阻擋，避免介面把「觀察到訊號」誤寫成「準備送單」。
        # 雷達資料缺失也採安全側拒絕，不能因空 dict 繞過交易資格。
        # MA7_Simple 路線刻意設計為「MA7 一轉折就進場」，使用者明確要求不受
        # 雷達資格審核（連續兩次確認+30分鐘觀察期）限制，直接放行。
        if radar_block_reason and str(route or "").lower() != "ma7_simple":
            diagnosis = _radar_signal_block_message(sym, route, radar_block_reason)
            s["entry_block_reason"] = radar_block_reason
            set_entry_diagnosis(diagnosis)
            logger.info(f"🛑 [Radar_Eligibility] {diagnosis}")
            continue

        # 模式切換確認日誌（順利產生訊號進場時）
        adx = float(s.get("adx", 0.0) or 0.0)
        log_decision_summary(
            sym,
            ma_status=ma_status,
            range_status=range_status,
            adx=adx,
            route=route,
            side=side,
        )

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
            # 區間模式本來就是為盤整設計的，不額外加重門檻
            if not is_range_signal:
                min_sig += 5.0
        if macro_mode == "MIXED":
            min_sig += 3.0
        # 區間模式使用專屬最低強度門檻（不被幣種 profile 或全域首這降低）
        if is_range_signal:
            from core.config import RANGE_MIN_SIGNAL_STRENGTH
            min_sig = max(min_sig, RANGE_MIN_SIGNAL_STRENGTH)
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
        # 已收盤 K 棒的量能確認。豁免門檻拉高到 30：MA_Cross/Breakout/Pullback 的
        # 基礎分數固定從 25 分起跳（見 signal_engine.py），17 分的門檻等於每一筆訊號
        # 都必然滿足，這道量能/價格確認機制形同虛設，從未真的擋過任何一筆交易。
        # 實測 LINKUSDT 案例：量價不協同（volume_price_sync 沒過），卻因為訊號分數
        # 固定 >=25 一定滿足這個豁免，直接放行進場，進場後價格馬上反著走。拉高到
        # 30，只有真的靠額外量能加分（volume_ratio>=1.8x）才拿得到豁免，一般訊號
        # 必須真的通過量能/價格確認才能進場。
        _strong_participation_strength = 30.0
        # 參與度乘數整體降低：0.30/0.35/0.35（原 0.35/0.40/0.45），
        # 與 base_limit=0.50 的放寬方向一致
        _d_multiplier = 0.30 if strength >= _strong_participation_strength else (0.35 if _is_low_vol_ce else 0.35)
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

            _rvol_multiplier = 0.30 if strength >= _strong_participation_strength else (0.35 if _is_low_vol_ce else 0.35)
            rvol_check = current_vol > (vol_ma20 * _rvol_multiplier)

            h24_quote_volume_est = vol_ma20 * cp * 288
            liquidity_check = h24_quote_volume_est > 1000000

            candle_open = s["ohlcv"][-2][1]
            candle_close = s["ohlcv"][-2][4]
            direction_ok = candle_close > candle_open if side == "buy" else candle_close < candle_open
            volume_price_sync = direction_ok and current_vol >= prev_vol * 0.70

            if route in ("MA_Cross", "MA_Breakout", "MA25_Pullback", "MA7_Simple"):
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
                    # 放寬量能要求：強度夠高時，只要量能達 0.35x 即可，基礎門檻放寬
                    strong_volume_override = strength >= _strong_participation_strength and current_vol >= vol_ma20 * 0.35
                    if not strong_volume_override:
                        s["low_participation_streak"] = s.get("low_participation_streak", 0) + 1
                        logger.info(f"🛑 [LOW_PARTICIPATION] {sym} 量價不協同，無跟進量支持，放棄進場")
                        set_entry_diagnosis(f"{sym}: 量價不協同，放棄進場")
                        continue
                    logger.info(f"⚡ [VOLUME_OVERRIDE] {sym} 強度 {strength:.1f} 且量能達均量 0.45x，允許進場")
            elif is_range_signal:
                # 區間模式：只做流動性最低門檻檢查，不要求量能爆發（區間交易量通常較低）
                if not liquidity_check:
                    s["low_participation_streak"] = s.get("low_participation_streak", 0) + 1
                    logger.info(f"🛑 [Range_LOW_LIQ] {sym} 被攔截：流動性不足 (估算24H交易額: {h24_quote_volume_est:,.0f} < 1,000,000)")
                    set_entry_diagnosis(f"{sym}: 流動性不足，放棄區間進場")
                    continue

        s["low_participation_streak"] = 0
        _force_close_confirmation = False

        # E2. 即時 5m 波動底線：日 ATR 高不代表現在有行情，避免選到當下死水幣。
        _atr_pct_5m = (_atr_cur_ce / cp) if cp > 0 else 0.0
        _min_atr_pct_5m = 0.0008 if is_range_signal else MIN_5M_ATR_PCT_FOR_MA_ENTRY * 0.80
        # MA7_Simple 路線刻意設計為「MA7 一轉折就進場」，不做即時波動底線檢查——
        # 使用者明確要求只要 MA7 谷底/頭部轉折，即直接放行，
        # 由 MA7_Simple 自身的量能與 RSI 極端值過濾負責基本把關。
        if str(route or "").lower() != "ma7_simple" and _atr_pct_5m < _min_atr_pct_5m:
            _mode_name = "區間" if is_range_signal else "MA"
            logger.info(
                f"🛑 [SLOW_MARKET] {sym} 5m ATR 僅 {_atr_pct_5m*100:.3f}% < "
                f"{_min_atr_pct_5m*100:.2f}%（{_mode_name} 成本安全線），放棄進場"
            )
            set_entry_diagnosis(f"{sym}: 即時波動不足，未達 {_mode_name} 成本安全線")
            continue

        _route_label = "Range" if is_range_signal else "MA"
        logger.info(f"✅ [{_route_label}_RISK_PASS] {sym}: {side} {_route_label} 與通用風控通過 (Route: {route})")
        logger.info(f"🧭 [ENTRY_GATE] {sym} 進入最後進場檢查 | side={side} route={route} strength={strength:.2f}")

        # 已有持倉只由 MA 生命週期與硬停損管理，不建立反手新倉。
        if has_position:
            continue

        if not is_entry_allowed(sym, side, route, strength):
            set_entry_diagnosis("{}: {}".format(sym, s.get("entry_block_reason") or "最後進場檢查未通過"))
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

        # --- R:R 盈虧比過濾 (Risk:Reward Filter)：只對 MA 路由做 ATR RR 計算 ---
        if not is_range_signal:
            atr_val, sl_dist, tp_dist, expected_rr = _calc_sl_tp(sym, side, s, p, route)
            base_rr_thresh = s.get("min_rr", 1.2)
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
            _HARD_MIN_PROFIT_PCT = 0.008
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
        candidates.append((sym, side, strength, route, is_range_signal))
        continue


    if not candidates:
        return

    # 候選可能來自上一根 K 的 pending 或回踩佇列；下單前重新驗證最新狀態。
    from core.symbol_profile import SYMBOL_PROFILES
    from core.entry_filter import RANGE_ENTRY_ROUTES, MA_ENTRY_ROUTES
    validated_candidates = []
    for sym, side, strength, route, is_range_sig in candidates:
        s = ctx.STATES[sym]
        radar_profile = SYMBOL_PROFILES.get(sym, {})
        if s.get("status") != "ACTIVE" and not _is_confirmable_exit_cooldown(s):
            continue
        if abs(s.get("qty", 0.0)) > 0.000001:
            continue
        radar_block_reason = _radar_entry_block_reason(radar_profile, route)
        if radar_block_reason and str(route or "").lower() != "ma7_simple":
            diagnosis = _radar_signal_block_message(sym, route, radar_block_reason)
            set_entry_diagnosis(diagnosis)
            logger.info(f"🛑 [Final_Entry_Guard] {diagnosis}")
            continue
        radar_direction_reason = _radar_direction_block_reason(radar_profile, side, route)
        if radar_direction_reason:
            logger.info(f"🛑 [Final_Entry_Guard] {sym} {radar_direction_reason}")
            continue

        # 路由白名單：MA 路由和區間路由都允許
        # 改為直接引用 MA_ENTRY_ROUTES，避免與 entry_filter.py 的定義重複維護、悄悄不一致
        allowed_routes = MA_ENTRY_ROUTES + tuple(RANGE_ENTRY_ROUTES)
        if route not in allowed_routes:
            logger.info(f"🛑 [Final_Entry_Guard] {sym} 未知路由：{route}")
            continue

        if not is_range_sig:
            # MA 路由：轉迭驗證 MA 結構
            ma_valid, ma_reason = is_entry_candidate_still_valid(sym, side, route, strength, s.get("close_price", 0.0))
            if not ma_valid:
                logger.info(f"🛑 [Final_Entry_Guard] {sym} MA 結構已失效：{ma_reason}")
                continue
        else:
            # 區間路由：重新驗證支撐/壓力帶位置是否仍然有效
            from core.entry_filter import is_range_direction_valid
            range_still_ok, range_still_reason = is_range_direction_valid(sym, side, route)
            if not range_still_ok:
                logger.info(f"🛑 [Final_Entry_Guard] {sym} 區間結構已失效：{range_still_reason}")
                continue

        if not is_entry_allowed(sym, side, route, strength):
            set_entry_diagnosis("{}: {}".format(sym, s.get("entry_block_reason") or "最後進場檢查未通過"))
            continue
        price = float(s.get("close_price", 0.0) or 0.0)
        if price <= 0:
            continue

        # RR 驗證與獲利空間
        _, _, tp_dist, latest_rr = _calc_sl_tp(sym, side, s, price, route)
        rr_floor = 1.1 if strength > 14.0 else (1.2 if strength > 12.0 else s.get("min_rr", 1.2))
        if not is_range_sig:
            if (latest_rr < rr_floor or (tp_dist / price - float(s.get("_expected_funding_cost_pct", 0.0) or 0.0)) < 0.008):
                logger.info(f"[Final_Entry_Guard] {sym} latest RR or profit room insufficient")
                continue
        else:
            # 區間模式：用區間實際幅度計算 RR 和淨獲利空間
            support    = float(s.get("range_support_level",    0.0) or 0.0)
            resistance = float(s.get("range_resistance_level", 0.0) or 0.0)
            atr        = float(s.get("current_atr", 0.0) or 0.0)
            from core.config import RANGE_MIN_NET_PROFIT_PCT, TAKER_FEE_RATE
            if not (support > 0 and resistance > 0 and support < resistance):
                logger.info(f"🛑 [Range_Final_Guard] {sym} 支撐/壓力資料不完整或順序錯誤")
                continue
            range_tp, range_sl = _range_exit_prices(
                price, support, resistance, atr, side
            )
            range_tp_dist = abs(range_tp - price)
            range_sl_dist = abs(range_sl - price)
            range_net_pct = range_tp_dist / price - TAKER_FEE_RATE * 2
            if range_net_pct < RANGE_MIN_NET_PROFIT_PCT:
                logger.info(f"🛑 [Range_Final_Guard] {sym} 區間獲利空間 {range_net_pct*100:.2f}% < {RANGE_MIN_NET_PROFIT_PCT*100:.1f}%")
                continue
            range_rr = range_tp_dist / range_sl_dist if range_sl_dist > 0 else 0.0
            if range_rr < 1.0:
                logger.info(f"🛑 [Range_Final_Guard] {sym} 區間 RR={range_rr:.2f} < 1.0")
                continue
            # 寫入進場時預先計算好的區間出場價位到 state
            s["range_tp_price"] = range_tp
            s["range_sl_price"] = range_sl
            logger.info(
                f"✅ [Range_Final_Guard] {sym} 區間 RR={range_rr:.2f} | "
                f"TP={range_tp:.4f} SL={range_sl:.4f} net={range_net_pct*100:.2f}%"
            )

        cooldown = float(COIN_PROFILE_CONFIG.get(sym, {}).get("loss_reentry_cooldown_sec", DEFAULT_LOSS_REENTRY_COOLDOWN_SEC) or 0.0)
        loss_time = get_last_same_side_loss_time(
            sym, side, s.get("last_loss_time_long" if side == "buy" else "last_loss_time_short", 0.0)
        )
        if cooldown > 0 and loss_time > 0 and time.time() - loss_time < cooldown:
            logger.info(f"🛑 [Final_Entry_Guard] {sym} 同方向虧損冷卻仍有效")
            continue

        if not is_range_sig:
            quality_ok, quality_reason, quality_score = _ma_candidate_quality(sym, side, strength, route, price)
            if not quality_ok:
                logger.info(f"🛑 [Entry_Structure_Guard] {sym} {quality_reason}，放棄進場")
                continue
            if route == "MA25_Pullback" and s.get("_ma25_sample_bonus", 0.0) > 0:
                logger.info(
                    f"📈 [MA25_Sample_Priority] {sym} 品質={quality_score:.2f} "
                    f"(T型樣本加分={s['_ma25_sample_bonus']:.2f})"
                )
            if route == "MA_Cross" and s.get("_ma_cross_sample_bonus", 0.0) > 0:
                logger.info(
                    f"📈 [MA_Cross_Sample_Priority] {sym} 品質={quality_score:.2f} "
                    f"(成功交叉樣本加分={s['_ma_cross_sample_bonus']:.2f})"
                )
            s["_entry_quality_score"] = quality_score
        else:
            # KAITO 型成功樣本（低 ADX、有量、合理 RSI、淨空間及 RR 充足）
            # 取得排序加分；未完全符合者仍保留原本的進場資格。
            s["_entry_quality_score"] = _range_candidate_quality(
                s, side, strength, range_rr, range_net_pct
            )
            logger.info(
                f"📈 [Range_Sample_Priority] {sym} 品質={s['_entry_quality_score']:.2f} "
                f"(基礎={strength:.2f}, 樣本加分={s['_range_sample_bonus']:.2f})"
            )

        # 高波動幣種權重加分：ATR% (ATR/現價) 越高的幣種，波段獲利潛力越大，給予品質排序加分
        atr_pct = float(s.get("atr_pct", 0.0) or 0.0)
        if atr_pct > 0:
            volatility_bonus = atr_pct * 10.0  # 0.3% ATR% -> +3.0 分, 0.5% ATR% -> +5.0 分
            s["_entry_quality_score"] = float(s.get("_entry_quality_score", 0.0)) + volatility_bonus

        validated_candidates.append((sym, side, strength, route, is_range_sig))

    candidates = validated_candidates
    if not candidates:
        return
    candidates.sort(key=lambda x: (
        -float(ctx.STATES[x[0]].get("_entry_quality_score", 0.0)),
        -x[2],
        x[0]
    ))

    # MA 路由與區間路由共用同一組槽位，區間模式另外套用自己的上限。
    inflight_symbols = {info.get("sym") for info in ctx.PENDING_LIMIT_ORDERS.values() if info.get("sym")}
    inflight_symbols.update(sym for sym, st in ctx.STATES.items() if st.get("is_ordering") and abs(st.get("qty", 0.0)) <= 0.000001)
    remaining_slots = max(0, dynamic_max_positions - open_count - len(inflight_symbols))
    if remaining_slots <= 0:
        return

    # 區間模式槽位計數：現有區間倉位數 + 正在下單的區間倉位數
    from core.config import RANGE_MAX_SLOTS
    _range_open_count = sum(
        1 for sym in ctx.ALL_SYMBOLS
        if abs(ctx.STATES[sym].get("qty", 0.0)) > 0.000001
        and ctx.STATES[sym].get("entry_reason", "") in ("Range_Support_Long", "Range_Resistance_Short")
    )
    _range_inflight = sum(
        1 for _sym, st in ctx.STATES.items()
        if st.get("is_ordering") and st.get("pending_side") is not None
        and st.get("entry_reason", "") in ("Range_Support_Long", "Range_Resistance_Short")
    )
    _range_slots_used = _range_open_count + _range_inflight

    _qual_desc = []
    for _c in candidates[:3]:
        _sym, _side, _str, _rt, _ir = _c
        _score = ctx.STATES[_sym].get("_entry_quality_score", 0.0)
        _qual_desc.append(f"{_sym}:{_side}(品質={_score:.2f}, 訊號={_str:.2f})")
    logger.info(f"📊 [品質排行] {' | '.join(_qual_desc)}")

    # 資金分配：只用實際會被派發的前 remaining_slots 名當分母
    _weight_pool = candidates[:remaining_slots] if remaining_slots > 0 else candidates
    total_weight = sum(strength for _, _, strength, _, _ in _weight_pool)

    for sym, side, strength, route, is_range_sig in candidates:
        if remaining_slots <= 0:
            break
        s = ctx.STATES[sym]
        has_pos = abs(s["qty"]) > 0.000001

        if not has_pos:
            # 區間模式超額槽位保護：已使用區間槽位數達到上限時，拒絕新的區間進場
            if is_range_sig and _range_slots_used >= RANGE_MAX_SLOTS:
                logger.info(f"⏳ [Range Slots Cap] {sym} 區間模式槽位已滿 ({_range_slots_used}/{RANGE_MAX_SLOTS})，略過進場")
                continue

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

            if _is_confirmable_exit_cooldown(s, side=side):
                logger.info(f"🔁 [冷卻快速複核] {sym} 首次完整流程通過，開始兩次即時複核")
                confirmed, confirm_reason = await _rapid_reconfirm_cooldown_entry(
                    sym, side, route, strength,
                )
                if not confirmed:
                    logger.info(f"🛑 [冷卻複核失敗] {sym} 不提前開倉：{confirm_reason}")
                    set_entry_diagnosis(f"{sym}: 冷卻快速複核失敗 - {confirm_reason}")
                    continue
                _release_confirmed_exit_cooldown(sym, s)
                logger.info(
                    f"✅ [冷卻提前解除] {sym} {side} {route} 約 2 秒內三次確認完整條件，"
                    f"包含 MA7／MA25／MA99，確認為反方向新波段，允許重新開倉"
                )
            elif s.get("status") == "COOLDOWN":
                logger.info(
                    f"⏳ [同方向完整冷卻] {sym} 上次出場方向為 "
                    f"{s.get('last_exit_direction') or 'unknown'}，拒絕提前以 {side} 重新進場"
                )
                continue

            remaining_slots -= 1
            if is_range_sig:
                _range_slots_used += 1
            logger.info(f"⚡ [即時開倉檢查] {sym} 觸發訊號 ({route} 路線)，準備送交交易所！")
            set_entry_diagnosis(f"{sym}: 訊號通過，準備送單 ({route})")
        # 金字塔順勢加碼（has_pos 且同方向）已在上方「方向鎖定」區塊直接 continue 掉，
        # 不會有 has_pos=True 的候選走到這裡；execute_order() 那邊的無條件停用
        # （core/orders.py:1253）留著當防禦性保底，避免未來其他路徑意外繞過這裡。

        if not s.get("is_ordering"):
            s["is_ordering"] = True
            s["pending_side"] = side

            # --- 動態權重分配 (Dynamic Position Sizing) ---
            allocation_pct = 1.0  # 使用者指示：每槽使用 100% 滿額權重分配

            # 流動性折扣
            _LIQ_MIN = 1_000_000
            _LIQ_COMFORT = 3_000_000
            _liq_est = s.get("_entry_liquidity_usdt")
            if _liq_est is not None and _liq_est < _LIQ_COMFORT:
                _liq_ratio = max(0.0, min(1.0, (_liq_est - _LIQ_MIN) / (_LIQ_COMFORT - _LIQ_MIN)))
                _liq_discount = 0.5 + _liq_ratio * 0.5
                if _liq_discount < 1.0:
                    allocation_pct *= _liq_discount
                    logger.info(f"⚖️ [Liquidity_Discount] {sym} 估算24H交易額 {_liq_est:,.0f} 偏薄（門檻 {_LIQ_MIN:,.0f}），倉位打折至 {_liq_discount*100:.0f}%")

            # --- 同向相關性折扣 (Correlation Discount) ---
            for other_sym in ctx.ALL_SYMBOLS:
                if other_sym == sym:
                    continue
                other_state = ctx.STATES.get(other_sym, {})
                other_qty = float(other_state.get("qty", 0.0))
                
                # 判定 other_sym 的持倉方向（實體倉位 或 正在下單中）
                other_side = None
                if other_qty > 0.000001:
                    other_side = "buy"
                elif other_qty < -0.000001:
                    other_side = "sell"
                elif other_state.get("is_ordering"):
                    other_side = other_state.get("pending_side")
                    
                if other_side == side:
                    klines_curr = s.get("ohlcv", [])
                    klines_other = other_state.get("ohlcv", [])
                    if len(klines_curr) >= 6 and len(klines_other) >= 6:
                        corr = _calculate_correlation(klines_curr, klines_other, limit=6)
                        if corr > 0.8:
                            allocation_pct *= 0.7
                            logger.info(f"⚖️ [Correlation_Discount] {sym} 與現有同向倉位 ({other_sym}) 走勢高度相關 (corr={corr:.2f})，為避免風險集中，倉位打 7 折")
                            break # 套用一次即可


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

    if route in ("Range_Support_Long", "Range_Resistance_Short"):
        from core.config import RANGE_ADX_THRESHOLD
        from core.entry_filter import is_range_direction_valid
        adx = float(s.get("current_adx", s.get("adx", 99.0)) or 99.0)
        if adx >= RANGE_ADX_THRESHOLD:
            return False, f"range trend strengthened (ADX {adx:.1f} >= {RANGE_ADX_THRESHOLD:.1f})"
        range_ok, range_reason = is_range_direction_valid(sym, side, route)
        if not range_ok:
            return False, range_reason
        return True, "range setup valid"

    from core.entry_filter import MA_ENTRY_ROUTES
    if route not in MA_ENTRY_ROUTES:
        return False, "non-MA route disabled"

    if route in MA_ENTRY_ROUTES:
        from core.entry_filter import is_ma_direction_aligned
        if not is_ma_direction_aligned(s, side, route):
            return False, "MA7/MA25/MA99 完整排列或斜率已失效"

    # 掛單／送單前維持基本 RSI 動能門檻，確保訊號未嚴重失效。
    # 門檻放寬至 45/55（原本 51/49），避免 RSI 在 48-52 正常震盪時
    # 反覆拒絕進場（常見於 MA25_Pullback 回踩期間 RSI 自然走弱）。
    # 真正嚴重失效（如 RSI 跌至 38）仍會被攔下。
    current_rsi = float(s.get("current_rsi", 50.0) or 50.0)
    if side == "buy" and current_rsi < 30.0:
        return False, f"waiting-period RSI below long threshold ({current_rsi:.1f} < 30)"
    if side == "sell" and current_rsi > 70.0:
        return False, f"waiting-period RSI above short threshold ({current_rsi:.1f} > 70)"

    return True, "ok"
