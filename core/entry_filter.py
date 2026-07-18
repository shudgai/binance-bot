import logging
import time

from core import ctx
from core.balance import is_daily_loss_halted
from core.config import COIN_PROFILE_CONFIG, DEFAULT_NEW_COIN_PROFILE, get_entry_strictness_profile
from core.state_manager import is_symbol_locked

logger = logging.getLogger(__name__)

MA_ENTRY_ROUTES = ("MA_Cross", "MA_Breakout", "MA25_Pullback", "MA7_Simple")
RANGE_ENTRY_ROUTES = ("Range_Support_Long", "Range_Resistance_Short")
ALL_ENTRY_ROUTES = MA_ENTRY_ROUTES + RANGE_ENTRY_ROUTES
BTC_MACRO_MAX_AGE_SEC = 180.0
BTC_MIXED_MIN_VOLUME_RATIO = 0.50


def btc_macro_entry_guard(sym, side):
    """Gate altcoin entries with fresh, completed-candle BTC 1H and 4H trends."""
    # 應使用者要求停用 BTC 大盤過濾，讓山寨幣（如 ETH/SOL/LTC）依賴自己的走勢獨立開倉
    return True, "BTC大盤過濾已停用，尊重個幣獨立走勢", "DISABLED"
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
        ma99_buffer = ma99 * 0.99
        return (closed_price > ma99_buffer and ma25 > ma99_buffer
                and prev_ma7 <= prev_ma25 and ma7 > ma25
                and ma7 > prev_ma7 and ma25 >= prev_ma25)
    if side == "sell" and normalized_route == "ma_cross":
        ma99_buffer = ma99 * 1.01
        return (closed_price < ma99_buffer and ma25 < ma99_buffer
                and prev_ma7 >= prev_ma25 and ma7 < ma25
                and ma7 < prev_ma7 and ma25 <= prev_ma25)
    if normalized_route == "ma7_simple":
        return ma7 > prev_ma7 if side == "buy" else ma7 < prev_ma7
    if side == "buy":
        # MA25_Pullback / MA_Breakout：不強求完整牛市排列（MA25>MA99），
        # 只要 MA7>MA25、斜率向上，且收盤與 MA25 均在 MA99 的 98% 緩衝帶以上即可。
        # 這樣允許剛突破 MA99、MA25 還未完全站上的情況進場，同時仍擋住深度跌破 MA99 的假訊號。
        ma99_buffer = ma99 * 0.98
        return (ma7 > ma25 and ma7 > prev_ma7 and ma25 >= prev_ma25
                and closed_price > ma99_buffer and ma25 > ma99_buffer)
    if side == "sell":
        # 空單對稱：MA7<MA25、斜率向下，收盤與 MA25 均在 MA99 的 102% 緩衝帶以下即可。
        ma99_buffer = ma99 * 1.02
        return (ma7 < ma25 and ma7 < prev_ma7 and ma25 <= prev_ma25
                and closed_price < ma99_buffer and ma25 < ma99_buffer)
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
    # 調降門檻以對齊全域放寬的 0.50x 基線與 CONFLUENCE_FAIL 的 0.40x-0.45x
    if route == "MA_Cross":
        required = 0.45
    elif route == "MA_Breakout":
        required = 0.6
    elif route in RANGE_ENTRY_ROUTES:
        required = 0.75  # 提高區間模式成交量門檻，避免在低成交量/弱支撐下進場接刀
    else:
        required = 0.4
    return closed_volume >= vol_ma20 * required


def is_valid_candle(sym, side, wick_multiplier=1.8):
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
        return upper_wick <= body * wick_multiplier
    return lower_wick <= body * wick_multiplier


def is_range_wick_safe(sym, side):
    """區間模式專用影線檢查：門檻比 MA 趨勢更寬鬆（3.5x vs 1.8x）。
    區間支撐/壓力附近震盪時，小 body + 較長影線屬正常現象，
    用 MA 趨勢的嚴格門檻會把合理的反彈進場機會全部擋掉。
    僅拒絕極端影線（代表方向被強烈否定的假訊號）。
    """
    candles = ctx.STATES[sym].get("ohlcv", [])
    if len(candles) < 2:
        return False
    candle = candles[-2]
    candle_open, high, low, close = map(float, candle[1:5])
    body = max(abs(close - candle_open), close * 0.0001)
    upper_wick = high - max(candle_open, close)
    lower_wick = min(candle_open, close) - low
    if side == "buy":
        return upper_wick <= body * 3.5
    return lower_wick <= body * 3.5


def is_entry_pin_safe(sym, side, route=None):
    # MA25_Pullback 本質是「觸碰 MA25 後反彈」，天生容易帶影線，
    # 用跟 Cross/Breakout 相同的 1.8 倍門檻偏嚴，比照區間路由的放寬邏輯，
    # 給 Pullback 單獨放寬至 2.5 倍；其餘路由維持原本 1.8 倍不變。
    if route == "MA25_Pullback":
        return is_valid_candle(sym, side, wick_multiplier=2.5)
    return is_valid_candle(sym, side)


def has_strong_local_momentum_override(route, strength):
    """Compatibility helper: only an already-valid MA route can be considered strong."""
    return route in MA_ENTRY_ROUTES and float(strength) >= 25.0


def is_range_direction_valid(sym, side, route):
    """區間掛單前確認雙邊區域完整、方向正確，且現價仍靠近對應邊界。"""
    s = ctx.STATES.get(sym)
    if not s:
        return False, "missing state"
    price = float(s.get("close_price", 0.0) or 0.0)
    atr = float(s.get("current_atr", 0.0) or 0.0)
    support = float(s.get("range_support_level", 0.0) or 0.0)
    resistance = float(s.get("range_resistance_level", 0.0) or 0.0)
    if price <= 0:
        return False, "invalid price"
    if support <= 0 or resistance <= 0 or support >= resistance:
        return False, "支撐與壓力帶資料不完整或順序錯誤"

    width = resistance - support
    # 訊號 K 棒收線後到最終下單守衛之間市場可能已彈走，放寬允許偏離上限至 2x ATR
    # 或區間寬度的 40%（原本 1x ATR / 20%），讓從支撐/壓力彈開後還在合理距離的
    # 進場機會不被誤擋。
    edge_tolerance = max(atr * 2.0, width * 0.40) if atr > 0 else width * 0.40
    breakout_tolerance = atr * 0.5 if atr > 0 else price * 0.005

    if route == "Range_Support_Long":
        if side != "buy":
            return False, "支撐路由方向必須為 buy"
        if price < support - breakout_tolerance:
            return False, f"現價 {price:.4f} 已跌穿支撐帶 {support:.4f} 過深，取消區間多單"
        if price > support + edge_tolerance:
            return False, f"現價已離開支撐邊界，偏離 {((price-support)/price)*100:.2f}%"
        return True, "range_support_ok"

    if route == "Range_Resistance_Short":
        if side != "sell":
            return False, "壓力路由方向必須為 sell"
        if price > resistance + breakout_tolerance:
            return False, f"現價 {price:.4f} 已突破壓力帶 {resistance:.4f} 過深，取消區間空單"
        if price < resistance - edge_tolerance:
            return False, f"現價已離開壓力邊界，偏離 {((resistance-price)/price)*100:.2f}%"
        return True, "range_resistance_ok"

    return False, f"非區間路由：{route}"


def is_entry_allowed(sym, side, route="MA_Cross", strength=0.0):
    """MA-only and Range final entry guard."""
    s = ctx.STATES[sym]
    is_ma_route = route in MA_ENTRY_ROUTES
    is_range_route = route in RANGE_ENTRY_ROUTES
    if not is_ma_route and not is_range_route:
        logger.info(f"🛑 [ROUTE_BLOCK] {sym} 拒絕未知路由：{route}")
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

    # ── 區間路由：使用區間專屬驗證，不要求 MA 三線排列 ──
    if is_range_route:
        range_ok, range_reason = is_range_direction_valid(sym, side, route)
        if not range_ok:
            logger.info(f"🛑 [Range_Direction] {sym} {range_reason}")
            return False
        # 量能最低門檻（區間模式也需要一定參與度）
        if not is_entry_volume_confirmed(sym, side):
            logger.info(f"🛑 [Range_Volume] {sym} {route} 已收線成交量不足")
            return False
        # 影線過長檢查（避免被假突破吸引）
        # 區間模式使用寬鬆門檻（3.5x），並加入冷卻避免同一根 K 棒每 10 秒重複拒絕。
        if time.time() < float(s.get("range_wick_cooldown_until", 0.0) or 0.0):
            return False
        if not is_range_wick_safe(sym, side):
            logger.info(f"🛑 [Range_Wick] {sym} 反向影線過長，取消 {route}")
            s["entry_block_reason"] = f"{route} 反向影線過長，取消進場"
            # 冷卻 60 秒：同一根 K 棒影線不會改變，不要每 10 秒重試
            s["range_wick_cooldown_until"] = time.time() + 60.0
            return False
        atr = float(s.get("current_atr", 0.0) or 0.0)
        if atr > 0 and len(candles) >= 3:
            reference = float(candles[-3][4])
            adverse = (reference - closed_price) if side == "buy" else (closed_price - reference)
            if adverse >= atr * 2.0:
                s["crash_cooldown_until"] = time.time() + 900
        if time.time() < float(s.get("crash_cooldown_until", 0.0) or 0.0):
            logger.info(f"🛑 [Range_AdverseMove] {sym} 近期逆向波動過大，等待冷卻")
            return False
        return True

    # ── MA 路由：原有邏輯 ──
    ma7  = float(s.get("ma7",  0.0) or 0.0)
    ma25 = float(s.get("ma25", 0.0) or 0.0)
    ma99 = float(s.get("ma99", 0.0) or 0.0)
    if min(closed_price, ma7, ma25, ma99) <= 0:
        return False
    if not is_ma_direction_aligned(s, side, route):
        logger.info(f"🛑 [MA_DIRECTION] {sym} 未通過 MA7/MA25/MA99 完整排列與斜率確認")
        s["entry_block_reason"] = "未通過 MA7／MA25／MA99 排列、1% 緩衝與斜率確認"
        return False

    s["entry_reason"] = route
    if not is_entry_volume_confirmed(sym, side):
        logger.info(f"🛑 [MA_VOLUME] {sym} {route} 已收線成交量不足")
        return False
    if not is_entry_pin_safe(sym, side, route=route):
        logger.info(f"🛑 [MA_WICK] {sym} 反向影線過長，取消 {route}")
        s["entry_block_reason"] = f"{route} 反向影線過長，取消進場"
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
