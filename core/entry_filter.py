import logging
import time

from core import ctx
from core.balance import is_daily_loss_halted
from core.config import (COIN_PROFILE_CONFIG, DEFAULT_NEW_COIN_PROFILE,
                         MA_CROSS_MIN_GAP_PCT, get_entry_strictness_profile)
from core.state_manager import is_symbol_locked

logger = logging.getLogger(__name__)

from core.config import DISABLE_MA25_PULLBACK

_ma_routes = ["MA_Cross", "MA_Breakout", "MA7_Simple"]
if not DISABLE_MA25_PULLBACK:
    _ma_routes.insert(2, "MA25_Pullback")
MA_ENTRY_ROUTES = tuple(_ma_routes)
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
        gap_confirmed = (ma7 - ma25) / closed_price >= MA_CROSS_MIN_GAP_PCT
        return (closed_price > ma99_buffer and ma25 > ma99_buffer
                and prev_ma7 <= prev_ma25 and ma7 > ma25
                and ma7 > prev_ma7 and ma25 >= prev_ma25 and gap_confirmed)
    if side == "sell" and normalized_route == "ma_cross":
        ma99_buffer = ma99 * 1.01
        gap_confirmed = (ma25 - ma7) / closed_price >= MA_CROSS_MIN_GAP_PCT
        return (closed_price < ma99_buffer and ma25 < ma99_buffer
                and prev_ma7 >= prev_ma25 and ma7 < ma25
                and ma7 < prev_ma7 and ma25 <= prev_ma25 and gap_confirmed)
    if normalized_route == "ma7_simple":
        # 跟 signal_engine.py 的 MA7_Simple 觸發條件對齊：MA7 轉折方向要對，
        # 且 MA25 中期趨勢不能是明顯逆勢（原本這裡完全沒檢查 MA25，等於允許
        # 在 MA25 走跌時只因 MA7 單根蠟燭翻頭向上就放行做多），ADX 也不能是
        # 剛暴衝上來的尖刺行情（同一套 ADX_SPIKE_GUARD_PCT 門檻）。
        adx = float(state.get("adx", 0.0) or 0.0)
        prev_adx = float(state.get("prev_adx", 0.0) or 0.0)
        adx_not_spiking = (adx - prev_adx) <= 20.0
        atr = float(state.get("current_atr", 0.0) or 0.0)
        extension_limit = max(closed_price * 0.0035, atr * 1.5)
        from core.config import PORT
        if side == "buy":
            # 做多：MA7 向上，且價格高於 MA99 趨勢護城河（大方向做多）
            if PORT == "8005":
                candle_green = len(candles) >= 2 and float(candles[-2][4]) >= float(candles[-2][1])
                return (ma7 > prev_ma7 and closed_price >= (ma99 * 0.985)
                        and closed_price >= (ma25 * 0.998) and candle_green and adx_not_spiking)
            return (ma7 > prev_ma7 and ma7 >= ma25 and ma25 >= prev_ma25
                    and closed_price >= (ma99 * 0.985)
                    and 0.0 <= closed_price - ma25 <= extension_limit
                    and adx_not_spiking)
        # 做空：MA7 向下，允許高達 MA99 +15% 的頂部空間（強勢趨勢後高位轉折做空）
        if PORT == "8005":
            candle_red = len(candles) >= 2 and float(candles[-2][4]) <= float(candles[-2][1])
            return (ma7 < prev_ma7 and closed_price <= (ma99 * 1.15)
                    and closed_price <= (ma25 * 1.002) and candle_red and adx_not_spiking)
        return (ma7 < prev_ma7 and ma7 <= ma25 and ma25 <= prev_ma25
                and closed_price <= (ma99 * 1.15)
                and 0.0 <= ma25 - closed_price <= extension_limit
                and adx_not_spiking)
    if side == "buy":
        # MA25_Pullback / MA_Breakout：不強求完整牛市排列（MA25>MA99），
        # 只要 MA7>MA25、斜率向上，且收盤與 MA25 均在 MA99 的 95% 緩衝帶以上即可。
        # 這樣允許剛突破 MA99、MA25 還未完全站上的情況進場，同時仍擋住深度跌破 MA99 的假訊號。
        ma99_buffer = ma99 * 0.95
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


def is_entry_volume_confirmed(sym, side, route=None):
    s = ctx.STATES[sym]
    candles = s.get("ohlcv", [])
    vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)
    if len(candles) < 2 or vol_ma20 <= 0:
        return False
    closed_volume = float(candles[-2][5])
    route = str(route or s.get("entry_reason", "") or "")
    from core.config import STRICT_ENTRY_SYMBOLS
    if sym not in STRICT_ENTRY_SYMBOLS:
        if route == "MA_Cross":
            required = 0.20
        elif route == "MA_Breakout":
            required = 0.30
        elif route in RANGE_ENTRY_ROUTES:
            required = 0.20
        else:
            required = 0.15
        return closed_volume >= vol_ma20 * required
    # 與 signal_engine.py 的路由門檻一致；送單前再以已收線量能複核，
    # 避免弱量轉折或突破只靠高基礎分數穿透最後一道品質檢查。
    if route == "MA_Cross":
        required = 0.35   # 與 MA_Cross 最低有效已收線量一致
    elif route == "MA_Breakout":
        required = 0.80   # 非核心幣突破量需達 0.8x
    elif route in RANGE_ENTRY_ROUTES:
        required = 0.20   # 大幅放寬區間模式門檻，允許量縮支撐開倉
    else:
        required = 0.50   # MA7_Simple / MA25_Pullback 不再用弱量豁免
    return closed_volume >= vol_ma20 * required


def _opposing_wick_ratio(sym, side):
    """Return the completed signal candle opposing-wick/body ratio."""
    candles = ctx.STATES[sym].get("ohlcv", [])
    if len(candles) < 2:
        return float("inf")
    candle_open, high, low, close = map(float, candles[-2][1:5])
    body = max(abs(close - candle_open), close * 0.0001)
    wick = high - max(candle_open, close) if side == "buy" else min(candle_open, close) - low
    return max(wick, 0.0) / body


def _entry_wick_multiplier(route):
    route_key = str(route or "").lower()
    if route_key == "ma7_simple":
        return 4.5
    if route_key == "ma25_pullback":
        return 3.5
    return 2.5


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
    # MA7_Simple 是轉折早期訊號，正常測試賣壓/買壓容易留下影線；1.8x 會讓一次性
    # 轉折被取消後再也追不上後續斜率，因此放寬至 3.5x，只拒絕極端否定。
    # MA25_Pullback 同樣容許回踩影線至 2.5x；Cross/Breakout 維持 1.8x。
    return is_valid_candle(sym, side, wick_multiplier=_entry_wick_multiplier(route))


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

    # 跟 signal_engine.py 的 RSI 動能過濾對齊：訊號產生後到這裡送單前，賣壓/
    # 買壓若還在快速惡化中，即使價格還沒跌破容忍帶，也不該放行（實測 ADAUSDT
    # 案例：RSI 不到一分鐘從 50 殺到 29，價格幾乎沒動但根本沒有真的止穩）。
    rsi = float(s.get("current_rsi", 50.0) or 50.0)
    prev_rsi = float(s.get("prev_rsi", 50.0) or 50.0)
    RANGE_RSI_MOMENTUM_GUARD_PCT = 8.0

    if route == "Range_Support_Long":
        if side != "buy":
            return False, "支撐路由方向必須為 buy"
        if price < support - breakout_tolerance:
            return False, f"現價 {price:.4f} 已跌穿支撐帶 {support:.4f} 過深，取消區間多單"
        if price > support + edge_tolerance:
            return False, f"現價已離開支撐邊界，偏離 {((price-support)/price)*100:.2f}%"
        if (prev_rsi - rsi) > RANGE_RSI_MOMENTUM_GUARD_PCT:
            return False, f"RSI 仍在快速下探（{prev_rsi:.1f}→{rsi:.1f}），賣壓未止穩"
        return True, "range_support_ok"

    if route == "Range_Resistance_Short":
        if side != "sell":
            return False, "壓力路由方向必須為 sell"
        if price > resistance + breakout_tolerance:
            return False, f"現價 {price:.4f} 已突破壓力帶 {resistance:.4f} 過深，取消區間空單"
        if price < resistance - edge_tolerance:
            return False, f"現價已離開壓力邊界，偏離 {((resistance-price)/price)*100:.2f}%"
        if (rsi - prev_rsi) > RANGE_RSI_MOMENTUM_GUARD_PCT:
            return False, f"RSI 仍在快速拉升（{prev_rsi:.1f}→{rsi:.1f}），買壓未止穩"
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
        if not is_entry_volume_confirmed(sym, side, route):
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

    if not is_entry_volume_confirmed(sym, side, route):
        logger.info(f"🛑 [MA_VOLUME] {sym} {route} 已收線成交量不足")
        return False
    if not is_entry_pin_safe(sym, side, route=route):
        wick_ratio = _opposing_wick_ratio(sym, side)
        wick_limit = _entry_wick_multiplier(route)
        logger.info(
            f"🛑 [MA_WICK] {sym} 反向影線 {wick_ratio:.2f}x > {wick_limit:.1f}x，取消 {route}"
        )
        s["entry_block_reason"] = (
            f"{route} 反向影線 {wick_ratio:.2f}x > {wick_limit:.1f}x，取消進場"
        )
        return False

    atr = float(s.get("current_atr", 0.0) or 0.0)
    if atr > 0 and len(candles) >= 3:
        reference = float(candles[-3][4])
        adverse = (reference - closed_price) if side == "buy" else (closed_price - reference)
        if adverse >= atr * 2.0:
            s["crash_cooldown_until"] = time.time() + 900
    # ── 方案 B：追高/追空過深防禦（防止急拉後衝高追單） ──
    # 當訊號 K 棒收盤價距離 MA7 拉開超過 0.60%，代表短線急拉過深，
    # 容易買在極短線頂部造成進場即浮虧，防護攔截並等待回落至 MA7 附近。
    if ma7 > 0:
        ma_dist_pct = (closed_price - ma7) / ma7 if side == "buy" else (ma7 - closed_price) / ma7
        if ma_dist_pct > 0.0060:
            logger.info(
                f"🛑 [MA_Overextension] {sym} {side} 收盤價 {closed_price:.6f} 距離 MA7 ({ma7:.6f}) "
                f"拉開 {ma_dist_pct*100:.2f}% > 0.60%，防追高不進場"
            )
            s["entry_block_reason"] = f"收盤價偏離 MA7 {ma_dist_pct*100:.2f}% > 0.60%，防衝高追單"
            return False

    return True
