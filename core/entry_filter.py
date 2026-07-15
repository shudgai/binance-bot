import logging
import time

from core import ctx
from core.balance import is_daily_loss_halted
from core.config import COIN_PROFILE_CONFIG, DEFAULT_NEW_COIN_PROFILE, get_entry_strictness_profile
from core.state_manager import is_symbol_locked

logger = logging.getLogger(__name__)

MA_ENTRY_ROUTES = ("MA_Cross", "MA_Breakout", "MA25_Pullback")
BTC_MACRO_MAX_AGE_SEC = 180.0
BTC_MIXED_MIN_VOLUME_RATIO = 0.80


def btc_macro_entry_guard(sym, side):
    """Gate altcoin entries with fresh, completed-candle BTC 1H and 4H trends."""
    normalized = str(sym or "").upper().replace(":", "").replace(chr(47), "")
    if normalized == "BTCUSDT":
        return True, "BTC uses its own MA structure", "SELF"
    if side not in ("buy", "sell"):
        return False, "invalid entry side", "INVALID"

    wind = ctx.MARKET_WIND
    trend_1h = str(wind.get("btc_trend_1h", "NEUTRAL") or "NEUTRAL").upper()
    trend_4h = str(wind.get("btc_trend_4h", "NEUTRAL") or "NEUTRAL").upper()
    updated_at = float(wind.get("btc_macro_updated_at", 0.0) or 0.0)
    if updated_at <= 0 or time.time() - updated_at > BTC_MACRO_MAX_AGE_SEC:
        return False, "BTC 1H+4H 已收線方向資料缺失或超過 3 分鐘", "STALE"

    if trend_1h == trend_4h == "BULL":
        if side == "sell":
            return False, "BTC 1H+4H 雙多，禁止山寨幣開空", "BULL"
        return True, "BTC 1H+4H 雙多，同向做多", "BULL"
    if trend_1h == trend_4h == "BEAR":
        if side == "buy":
            return False, "BTC 1H+4H 雙空，禁止山寨幣開多", "BEAR"
        return True, "BTC 1H+4H 雙空，同向做空", "BEAR"

    state = ctx.STATES.get(sym, {})
    candles = state.get("ohlcv", [])
    vol_ma20 = float(state.get("vol_ma20", 0.0) or 0.0)
    closed_volume = float(candles[-2][5]) if len(candles) >= 2 else 0.0
    volume_ratio = closed_volume / vol_ma20 if vol_ma20 > 0 else 0.0
    if volume_ratio < BTC_MIXED_MIN_VOLUME_RATIO:
        return False, f"BTC 1H+4H 方向混合 ({trend_1h}+{trend_4h})，個幣量能 {volume_ratio:.2f}x 不足", "MIXED"
    return True, f"BTC 1H+4H 方向混合 ({trend_1h}+{trend_4h})，個幣強量放行", "MIXED"


def is_ma_direction_aligned(state, side, route=None):
    """Require a completed-candle three-MA trend stack and non-adverse MA slopes."""
    candles = state.get("ohlcv", [])
    if len(candles) < 2:
        return False
    closed_price = float(candles[-2][4])
    ma7 = float(state.get("ma7", 0.0) or 0.0)
    ma25 = float(state.get("ma25", 0.0) or 0.0)
    ma99 = float(state.get("ma99", 0.0) or 0.0)
    prev_ma7 = float(state.get("prev_ma7", ma7) or ma7)
    prev_ma25 = float(state.get("prev_ma25", ma25) or ma25)
    if min(closed_price, ma7, ma25, ma99) <= 0:
        return False
    normalized_route = str(route or "").lower()
    if side == "buy" and normalized_route == "ma_cross":
        return (closed_price > ma99 and prev_ma7 <= prev_ma25 and ma7 > ma25
                and ma7 > prev_ma7 and ma25 >= prev_ma25)
    if side == "sell" and normalized_route == "ma_cross":
        return (closed_price < ma99 and prev_ma7 >= prev_ma25 and ma7 < ma25
                and ma7 < prev_ma7 and ma25 <= prev_ma25)
    if side == "buy":
        return closed_price > ma99 and ma7 > ma25 > ma99 and ma7 > prev_ma7 and ma25 >= prev_ma25
    if side == "sell":
        return closed_price < ma99 and ma7 < ma25 < ma99 and ma7 < prev_ma7 and ma25 <= prev_ma25
    return False


def is_last_closed_1m_aligned(state, side):
    candles = state.get("ohlcv", [])
    if len(candles) < 2:
        return True
    candle = candles[-2]
    candle_open, candle_close = float(candle[1]), float(candle[4])
    return candle_close >= candle_open if side == "buy" else candle_close <= candle_open


def get_dynamic_volume_factor(states):
    current = sum(float(s.get("current_vol", 0.0) or 0.0) for s in states.values())
    average = sum(float(s.get("vol_ma20", 0.0) or 0.0) for s in states.values())
    return 1.0 if average > 0 and current / average < 1.0 else 1.2


def is_entry_volume_confirmed(sym, side):
    s = ctx.STATES[sym]
    candles = s.get("ohlcv", [])
    vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)
    if len(candles) < 2 or vol_ma20 <= 0:
        return False
    closed_volume = float(candles[-2][5])
    route = str(s.get("entry_reason", "") or "")
    required = 1.0 if route == "MA_Cross" else 1.2 if route == "MA_Breakout" else 0.8
    return closed_volume >= vol_ma20 * required


def is_valid_candle(sym, side):
    """Reject a completed candle whose opposing wick dominates its body."""
    candles = ctx.STATES[sym].get("ohlcv", [])
    if len(candles) < 2:
        return False
    candle = candles[-2]
    candle_open, high, low, close = map(float, candle[1:5])
    body = max(abs(close - candle_open), close * 0.0001)
    upper_wick = high - max(candle_open, close)
    lower_wick = min(candle_open, close) - low
    if side == "buy":
        return upper_wick <= body * 1.8
    return lower_wick <= body * 1.8


def is_entry_pin_safe(sym, side):
    return is_valid_candle(sym, side)


def has_strong_local_momentum_override(route, strength):
    """Compatibility helper: only an already-valid MA route can be considered strong."""
    return route in MA_ENTRY_ROUTES and float(strength) >= 25.0


def is_entry_allowed(sym, side, route="MA_Cross", strength=0.0):
    """MA-only final entry guard; RSI, MACD and Bollinger never affect this decision."""
    s = ctx.STATES[sym]
    if route not in MA_ENTRY_ROUTES:
        logger.info(f"🛑 [MA_ONLY] {sym} 拒絕已刪除的非 MA 路由：{route}")
        return False
    if side not in ("buy", "sell") or s.get("status") != "ACTIVE":
        return False
    if is_daily_loss_halted() or is_symbol_locked(sym):
        return False

    if sym not in COIN_PROFILE_CONFIG:
        COIN_PROFILE_CONFIG[sym] = DEFAULT_NEW_COIN_PROFILE.copy()

    candles = s.get("ohlcv", [])
    if len(candles) < 2:
        return False
    closed_price = float(candles[-2][4])
    ma7 = float(s.get("ma7", 0.0) or 0.0)
    ma25 = float(s.get("ma25", 0.0) or 0.0)
    ma99 = float(s.get("ma99", 0.0) or 0.0)
    if min(closed_price, ma7, ma25, ma99) <= 0:
        return False
    if not is_ma_direction_aligned(s, side, route):
        logger.info(f"🛑 [MA_DIRECTION] {sym} 未通過 MA7/MA25/MA99 完整排列與斜率確認")
        return False

    s["entry_reason"] = route
    if not is_entry_volume_confirmed(sym, side):
        logger.info(f"🛑 [MA_VOLUME] {sym} {route} 已收線成交量不足")
        return False
    if not is_entry_pin_safe(sym, side):
        logger.info(f"🛑 [MA_WICK] {sym} 反向影線過長，取消 {route}")
        return False

    atr = float(s.get("current_atr", 0.0) or 0.0)
    if atr > 0 and len(candles) >= 3:
        reference = float(candles[-3][4])
        adverse = (reference - closed_price) if side == "buy" else (closed_price - reference)
        if adverse >= atr * 2.0:
            s["crash_cooldown_until"] = time.time() + 900
    if time.time() < float(s.get("crash_cooldown_until", 0.0) or 0.0):
        logger.info(f"🛑 [MA_AdverseMove] {sym} 近期逆向波動過大，等待冷卻")
        return False

    return True
