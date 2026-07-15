import logging
import time

from core import ctx
from core.balance import is_daily_loss_halted
from core.config import COIN_PROFILE_CONFIG, DEFAULT_NEW_COIN_PROFILE, get_entry_strictness_profile
from core.state_manager import is_symbol_locked

logger = logging.getLogger(__name__)

MA_ENTRY_ROUTES = ("MA_Cross", "MA_Breakout", "MA25_Pullback")


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
    if side == "buy" and not (closed_price > ma99 and ma7 > ma25):
        return False
    if side == "sell" and not (closed_price < ma99 and ma7 < ma25):
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
