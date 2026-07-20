import logging
import asyncio
import time
import json
import os
import fcntl
from datetime import datetime

from core import ctx
from core.peak_store import clear_peak

from core.config import (PAPER_TRADING, USE_TESTNET, TRADE_HISTORY_FILE, DUAL_SHOT_ORDER_TIMEOUT,
    DUAL_SHOT_LEVERAGE, COIN_PROFILE_CONFIG, HARD_STOP_LOSS_PCT, DUAL_SHOT_MAX_SLOTS,
ENTRY_ORDER_MODE, ENTRY_PULLBACK_ATR_MULT, ENTRY_CHASE_OFFSET_PCT,
    ENTRY_ORDER_MODE_AUTO_STRONG, ENTRY_ORDER_MODE_AUTO_MARKET, EXIT_RR_MULTIPLIER)
from core.exchange_client import (exchange_futures, exchange_market_data, sanitize_order_qty,
    get_contract_precision, round_step, convert_to_ccxt_symbol, get_reference_price,
    get_contract_openability)
from core.balance import get_balance, compute_per_coin_margin, accrue_daily_realized_pnl, get_total_wallet_balance
import core.balance as _bal
from core.state_manager import mark_exit, reset_coin_state, build_symbol_state
from core.symbol_profile import apply_symbol_profile, SYMBOL_PROFILES
from core.config import get_symbol_leverage
from core.indicators import _calc_sl_tp
from services.utils import paper_key
from services.update_paper_state import update_paper_state
from services.ai_manager import ai_engine

logger = logging.getLogger(__name__)

MA_ENTRY_ROUTES = {"ma_cross", "ma_breakout", "ma25_pullback", "ma7_simple", "ma_restored"}
RANGE_ENTRY_ROUTES = {"range_support_long", "range_resistance_short"}
MA_DISASTER_STOP_PCT = 0.015
MA_PENDING_MONITOR_INTERVAL_SEC = 3.0
MA7_SIMPLE_PENDING_MAX_SEC = 20.0
MA_PENDING_REPRICE_COOLDOWN_SEC = 6.0
MA_CROSS_MAX_EXTENSION_PCT = 0.0025
MA_CROSS_MAX_EXTENSION_ATR_MULT = 0.5
MA_CROSS_ANCHOR_BUFFER_PCT = 0.0003
# 實測 AVAXUSDT 案例：區間進場原本只留 0.02% 緩衝（幾乎貼著壓力/支撐線進場），
# 結構停損卻是相對「壓力/支撐位」抓 0.5x ATR，等於進場價到停損的實際距離只
# 比半個 ATR 多一點點——對正常波動的幣，一根雜訊影線就可能觸發，不需要真的
# 突破。緩衝拉大到 0.15%，讓進場價本身就多留一點空間，且因為停損仍是相對
# 壓力/支撐位計算，緩衝越大代表進場到停損的實際距離也越大，不會反而放大
# 單筆虧損上限。
RANGE_ENTRY_EDGE_BUFFER_PCT = 0.0015


def _clear_previous_position_peak(sym, state):
    """Start a new position lifecycle without a prior trade peak on disk or in memory."""
    state["highest_profit_pct"] = 0.0
    state["max_profit_reached"] = 0.0
    state["ma_peak_saved_pct"] = 0.0
    clear_peak(sym)


def should_block_order_flow(side, bids, asks, threshold, paper_trading):
    if side == "buy":
        imbalanced = asks == 0 or bids / asks < threshold
    else:
        imbalanced = bids == 0 or asks / bids < threshold
    return imbalanced


def _import_update_trailing_stop():
    from core.exits import update_trailing_stop
    return update_trailing_stop


async def _cancel_exchange_exit_order_id(sym, order_id, label):
    if not order_id or PAPER_TRADING:
        return True
    try:
        await exchange_futures.fapiPrivateDeleteAlgoOrder({
            "symbol": sym,
            "algoId": order_id,
        })
        logger.info(f"✅ [{label}取消] {sym} 已撤銷交易所 Algo {label}單 {order_id}")
        return True
    except Exception as algo_error:
        try:
            await exchange_futures.cancel_order(order_id, sym)
            logger.info(f"✅ [{label}取消] {sym} 已撤銷交易所{label}單 {order_id}")
            return True
        except Exception as order_error:
            logger.info(
                f"⚠️ [取消{label}單失敗] {sym} {order_id}: "
                f"algo={algo_error}; order={order_error}"
            )
            return False


async def _cancel_exchange_exit_order(sym, state_key, label):
    s = ctx.STATES[sym]
    order_id = s.get(state_key)
    if not order_id or PAPER_TRADING:
        return
    try:
        await _cancel_exchange_exit_order_id(sym, order_id, label)
    finally:
        s[state_key] = None


def _range_exit_bracket(state, avg, is_long, tick_size):
    """區間單固定在對側邊界停利、結構外側停損，不套用趨勢單的 R:R 延伸。"""
    avg = float(avg or 0.0)
    take_profit = float(state.get("range_tp_price", 0.0) or 0.0)
    stop = float(state.get("range_sl_price", 0.0) or 0.0)
    if avg <= 0 or take_profit <= 0 or stop <= 0:
        return None
    valid = stop < avg < take_profit if is_long else take_profit < avg < stop
    if not valid:
        return None
    return round_step(stop, tick_size), round_step(take_profit, tick_size)


def _enforce_bracket_rr(avg, stop_price, take_profit_price, is_long, tick_size, min_rr=EXIT_RR_MULTIPLIER):
    """以最終掛單價保證停利距離至少為停損距離的 min_rr 倍。"""
    avg = float(avg)
    stop_price = float(stop_price)
    take_profit_price = float(take_profit_price)
    tick_size = float(tick_size)
    stop_dist = (avg - stop_price) if is_long else (stop_price - avg)
    tp_dist = (take_profit_price - avg) if is_long else (avg - take_profit_price)
    if stop_dist <= 0:
        raise ValueError("stop price is on the wrong side of entry")
    required_tp_dist = stop_dist * float(min_rr)
    if tp_dist < required_tp_dist:
        target = avg + required_tp_dist if is_long else avg - required_tp_dist
        take_profit_price = round_step(target, tick_size)
        tp_dist = (take_profit_price - avg) if is_long else (avg - take_profit_price)
        if tp_dist + 1e-12 < required_tp_dist:
            take_profit_price += tick_size if is_long else -tick_size
            take_profit_price = round_step(take_profit_price, tick_size)
    return stop_price, take_profit_price


def _ma_exchange_stop_target(state, avg, is_long, hard_stop, current_price=0.0):
    """選出最強且尚未被行情穿越的 MA 交易所端保護價。"""
    candidates = []
    if state.get("ma_profit_floor_armed", False):
        candidates.append(float(state.get("ma_profit_floor_price", 0.0) or 0.0))
    if state.get("ma_peak_lock_armed", False):
        candidates.append(float(state.get("ma_peak_lock_price", 0.0) or 0.0))
    candidates = [price for price in candidates if price > 0]
    if not candidates:
        return float(hard_stop)
    target = max(candidates) if is_long else min(candidates)
    profit_side = target > avg if is_long else target < avg
    not_already_crossed = (
        current_price <= 0
        or (target < current_price if is_long else target > current_price)
    )
    return target if profit_side and not_already_crossed else float(hard_stop)


async def _replace_exchange_exit_orders(sym):
    if PAPER_TRADING:
        return

    s = ctx.STATES[sym]
    qty = abs(s.get("qty", 0.0))
    avg = s.get("avg_price", 0.0)
    if qty <= 0.000001 or avg <= 0:
        return

    await _cancel_exchange_exit_order(sym, "exchange_stop_order_id", "止損")
    await _cancel_exchange_exit_order(sym, "exchange_take_profit_order_id", "停利")

    prec = await get_contract_precision(sym)
    close_side = "sell" if s["qty"] > 0 else "buy"
    is_long = s["qty"] > 0

    hard_sl_pct = s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT)
    route = str(s.get("entry_reason", "a") or "a").lower()
    is_ma_route = route in MA_ENTRY_ROUTES
    if is_ma_route:
        hard_sl_pct = MA_DISASTER_STOP_PCT
    # Exchange-side disaster stop is anchored to market structure at entry.
    # Breakouts use MA7; pullback/trend entries use the more stable MA25.
    hard_stop = avg * (1 - hard_sl_pct) if is_long else avg * (1 + hard_sl_pct)
    ma_anchor = float(s.get("ma7" if route in ("breakout", "ma_breakout", "ma_cross") else "ma25", 0.0) or 0.0)
    atr = float(s.get("entry_atr", s.get("current_atr", 0.0)) or 0.0)
    structure_buffer = max(atr * 0.20, avg * 0.001)
    stop_price = hard_stop
    if not is_ma_route and is_long and 0 < ma_anchor < avg:
        stop_price = max(hard_stop, ma_anchor - structure_buffer)
    elif not is_ma_route and not is_long and ma_anchor > avg:
        stop_price = min(hard_stop, ma_anchor + structure_buffer)
    stop_price = round_step(stop_price, prec["tick_size"])
    stop_dist = (avg - stop_price) if is_long else (stop_price - avg)
    _, _, tp_dist, _ = _calc_sl_tp(sym, "buy" if is_long else "sell", s, avg, route)
    take_profit_price = avg + tp_dist if is_long else avg - tp_dist
    take_profit_price = round_step(take_profit_price, prec["tick_size"])
    _original_tp = take_profit_price

    # 確保停損價格不會大於停利價格 (在 RR 比例強制執行前先做初步檢查)
    # 如果 hard_sl_pct 導致的 stop_dist 大於 tp_dist，則強制縮減 stop_dist 或擴大 tp_dist
    current_tp_dist = (take_profit_price - avg) if is_long else (avg - take_profit_price)
    if not is_ma_route and stop_dist > current_tp_dist:
        logger.info(f"⚠️ [SL_GT_TP_Guard] {sym} 偵測到停損距離 ({stop_dist:.4f}) 大於停利距離 ({current_tp_dist:.4f})。正在自動校正...")
        # 優先縮減停損距離，確保其在合理的範圍內，同時保留 RR 比例檢查
        # 這裡簡單處理：將 stop_dist 設為 tp_dist 的 0.8 倍，確保停損距離較小
        new_stop_dist = current_tp_dist * 0.8
        stop_price = avg - new_stop_dist if is_long else avg + new_stop_dist
        stop_price = round_step(stop_price, prec["tick_size"])
        stop_dist = new_stop_dist

    bracket_min_rr = EXIT_RR_MULTIPLIER
    stop_price, take_profit_price = _enforce_bracket_rr(
        avg, stop_price, take_profit_price, is_long, prec["tick_size"], min_rr=bracket_min_rr
    )
    if not is_ma_route and take_profit_price != _original_tp:
        logger.info(
            f"⚠️ [Bracket_RR_Guard] {sym} 最終掛單盈虧比不足，"
            f"停利由 {_original_tp} 校正為 {take_profit_price}（最低 R:R={bracket_min_rr}）"
        )

    is_range_route = route in RANGE_ENTRY_ROUTES
    if is_range_route:
        range_bracket = _range_exit_bracket(s, avg, is_long, prec["tick_size"])
        if range_bracket is not None:
            stop_price, take_profit_price = range_bracket
            logger.info(
                f"🎯 [Range_Exchange_Bracket] {sym} 使用區間結構保護單："
                f"TP={take_profit_price} SL={stop_price}，不延伸到區間外"
            )
        else:
            logger.info(f"⚠️ [Range_Exchange_Bracket] {sym} 區間 TP/SL 遺失或方向錯誤，使用通用保護單")

    # MA 波段先保留 1.5% 災難止損；盈利底線形成後提升為交易所端鎖利。
    if is_ma_route:
        current_price = float(
            s.get("last_trade_price", 0.0) or s.get("close_price", 0.0) or 0.0
        )
        stop_price = _ma_exchange_stop_target(
            s, avg, is_long, hard_stop, current_price=current_price,
        )
        stop_price = round_step(stop_price, prec["tick_size"])

    # 防禦性保底：進場已經會把數量夾在 MARKET_LOT_SIZE 上限之內（見 execute_order），
    # 這裡理論上不該再超過，但攤平救援等會改變 qty 的路徑萬一漏夾，用同一個上限保底，
    # 避免掛單直接被 -4005 拒絕、部位變成完全沒有交易所端保護。
    _market_max_qty = prec.get('market_max_qty')
    if _market_max_qty and _market_max_qty > 0 and qty > _market_max_qty:
        logger.info(f"⚠️ [MARKET_MAX_QTY] {sym} 止損/停利數量 {qty:.4f} > 市價單上限 {_market_max_qty}，僅能為部分倉位掛單保護")
        qty = round_step(_market_max_qty, prec["step_size"])

    stop_order = await exchange_futures.create_order(
        sym, type="STOP_MARKET", side=close_side, amount=qty,
        params={"stopPrice": stop_price, "reduceOnly": True}
    )
    s["exchange_stop_order_id"] = stop_order["id"]
    logger.info(f"🛡️ [交易所挂單] {sym} 成功挂出 Stop Market 止損單 @ {stop_price} (數量: {qty})")

    if is_ma_route:
        s["exchange_take_profit_order_id"] = None
        logger.info(f"🎯 [MA波段掛單] {sym} 不掛固定價停利，使用高點鎖利或等待 MA7/MA25 反向交叉")
        return

    tp_order = await exchange_futures.create_order(
        sym, type="TAKE_PROFIT_MARKET", side=close_side, amount=qty,
        params={"stopPrice": take_profit_price, "reduceOnly": True}
    )
    s["exchange_take_profit_order_id"] = tp_order["id"]
    logger.info(f"🎯 [交易所挂單] {sym} 成功挂出 Take Profit Market 停利單 @ {take_profit_price} (數量: {qty})")


async def _sync_ma_exchange_profit_stop(sym):
    """先建立新單再撤舊單，將 MA 盈利底線同步成交易所端保護。"""
    if PAPER_TRADING:
        return
    s = ctx.STATES.get(sym)
    if not s:
        return
    qty = abs(float(s.get("qty", 0.0) or 0.0))
    avg = float(s.get("avg_price", 0.0) or 0.0)
    if qty <= 0.000001 or avg <= 0:
        return
    is_long = float(s.get("qty", 0.0)) > 0
    hard_stop = avg * (1.0 - MA_DISASTER_STOP_PCT if is_long else 1.0 + MA_DISASTER_STOP_PCT)
    current_price = float(
        s.get("last_trade_price", 0.0) or s.get("close_price", 0.0) or 0.0
    )
    target = _ma_exchange_stop_target(
        s, avg, is_long, hard_stop, current_price=current_price,
    )
    if abs(target - hard_stop) <= avg * 0.000001:
        return
    prec = await get_contract_precision(sym)
    target = round_step(target, prec["tick_size"])
    close_side = "sell" if is_long else "buy"
    old_order_id = s.get("exchange_stop_order_id")
    new_order = await exchange_futures.create_order(
        sym, type="STOP_MARKET", side=close_side, amount=qty,
        params={"stopPrice": target, "reduceOnly": True},
    )
    s["exchange_stop_order_id"] = new_order["id"]
    if old_order_id and str(old_order_id) != str(new_order["id"]):
        await _cancel_exchange_exit_order_id(sym, old_order_id, "舊MA保護止損")
    logger.info(
        f"🔒 [MA交易所鎖利] {sym} 已同步 STOP_MARKET @ {target}"
    )


async def _fetch_open_exchange_exit_orders(sym):
    try:
        return await exchange_futures.fapiPrivateGetOpenAlgoOrders({"symbol": sym})
    except Exception as algo_error:
        logger.info(f"⚠️ [Algo退出單查詢失敗] {sym}: {algo_error}，回退一般委託查詢")
        open_orders = await exchange_futures.fetch_open_orders(sym)
        normalized = []
        for order in open_orders or []:
            info = order.get("info", {})
            normalized.append({
                "algoId": order.get("id") or info.get("orderId"),
                "orderType": order.get("type") or info.get("type"),
                "quantity": order.get("amount") or info.get("origQty") or 0,
                "side": order.get("side") or info.get("side"),
                "reduceOnly": order.get("reduceOnly", info.get("reduceOnly", False)),
                "algoStatus": str(order.get("status", "")).upper(),
                "createTime": order.get("timestamp") or info.get("time") or 0,
                "triggerPrice": order.get("stopPrice") or info.get("stopPrice") or 0,
            })
        return normalized


async def _ensure_exchange_exit_orders(sym):
    if PAPER_TRADING:
        return

    s = ctx.STATES[sym]
    qty = abs(s.get("qty", 0.0))
    if qty <= 0.000001 or s.get("avg_price", 0.0) <= 0:
        return

    try:
        open_orders = await _fetch_open_exchange_exit_orders(sym)
    except Exception as exc:
        logger.info(f"⚠️ [交易所退出單檢查失敗] {sym}: {exc}")
        return

    close_side = "SELL" if s["qty"] > 0 else "BUY"
    route = str(s.get("entry_reason", "") or "").lower()
    is_ma_route = route in MA_ENTRY_ROUTES
    avg = float(s["avg_price"])
    hard_ma_stop = avg * (
        1.0 - MA_DISASTER_STOP_PCT if s["qty"] > 0 else 1.0 + MA_DISASTER_STOP_PCT
    )
    current_price = float(
        s.get("last_trade_price", 0.0) or s.get("close_price", 0.0) or 0.0
    )
    expected_ma_stop = _ma_exchange_stop_target(
        s, avg, s["qty"] > 0, hard_ma_stop, current_price=current_price,
    )
    candidates = {"stop": [], "take_profit": []}
    all_exit_orders = []
    for order in open_orders or []:
        order_type = str(order.get("orderType") or "").upper()
        reduce_only = str(order.get("reduceOnly", False)).lower() in ("true", "1")
        status = str(order.get("algoStatus") or "").upper()
        if not reduce_only or status not in ("NEW", "OPEN"):
            continue
        if order_type not in ("STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
            continue
        all_exit_orders.append(order)
        order_qty = float(order.get("quantity") or 0.0)
        side_matches = str(order.get("side") or "").upper() == close_side
        qty_matches = abs(order_qty - qty) <= max(0.000001, qty * 0.001)
        if not side_matches or not qty_matches:
            continue
        key = "stop" if order_type in ("STOP_MARKET", "STOP") else "take_profit"
        if is_ma_route and key == "take_profit":
            continue
        if is_ma_route and key == "stop":
            trigger_price = float(order.get("triggerPrice") or order.get("stopPrice") or 0.0)
            if trigger_price <= 0 or abs(trigger_price - expected_ma_stop) / float(s["avg_price"]) > 0.001:
                continue
        candidates[key].append(order)

    chosen = {}
    for key, orders in candidates.items():
        if orders:
            chosen[key] = max(orders, key=lambda order: int(order.get("createTime") or 0))

    chosen_ids = {str(order.get("algoId")) for order in chosen.values()}
    for order in all_exit_orders:
        order_id = str(order.get("algoId"))
        if order_id in chosen_ids:
            continue
        label = "止損" if str(order.get("orderType", "")).upper().startswith("STOP") else "停利"
        await _cancel_exchange_exit_order_id(sym, order_id, f"殘留{label}")

    s["exchange_stop_order_id"] = chosen.get("stop", {}).get("algoId")
    s["exchange_take_profit_order_id"] = chosen.get("take_profit", {}).get("algoId")
    exits_complete = bool(s.get("exchange_stop_order_id")) and (
        is_ma_route or bool(s.get("exchange_take_profit_order_id"))
    )
    if exits_complete:
        label = "1.5% 災難止損存在、高點鎖利由即時行情管理" if is_ma_route else "止損/停利單皆存在且數量正確"
        logger.info(f"✅ [交易所退出單確認] {sym} Algo {label}")
        return

    logger.info(f"🛡️ [交易所退出單修復] {sym} 退出掛單不符合目前波段規則，重新建立")
    await _replace_exchange_exit_orders(sym)


def _entry_direction_guard(sym, side, reference_price=None):
    s = ctx.STATES.get(sym, {})
    p = float(s.get("close_price", 0.0) or 0.0)
    if p <= 0:
        return True, "no_price"

    ref = float(reference_price or s.get("last_entry_signal_price", 0.0) or p)
    atr = float(s.get("current_atr", 0.0) or 0.0)
    adverse_limit = max((atr * 0.9) if atr > 0 else 0.0, ref * 0.003)
    adverse_move = (ref - p) if side == "buy" else (p - ref)
    if adverse_move > adverse_limit:
        return False, f"price moved adverse {adverse_move:.6f} > {adverse_limit:.6f}"

    ohlcv = s.get("ohlcv", [])
    vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)
    if len(ohlcv) >= 3 and vol_ma20 > 0:
        c1 = ohlcv[-1]
        c2 = ohlcv[-2]
        if side == "buy":
            opposite = c1[4] < c1[1] and c2[4] < c2[1] and p < c2[4]
        else:
            opposite = c1[4] > c1[1] and c2[4] > c2[1] and p > c2[4]
        if opposite and float(c1[5]) >= vol_ma20 * 0.6:
            return False, "two opposite candles with volume"

    return True, "ok"


def _entry_price_guard(sym, side, order_price, market_price, mode="", is_rescue_dca=False):
    if order_price is None or market_price is None or market_price <= 0:
        return True, "market_or_no_ref"
    s = ctx.STATES.get(sym, {})
    atr = float(s.get("current_atr", 0.0) or 0.0)
    atr_pct = atr / market_price if market_price > 0 else 0.0

    from core.config import ENTRY_STRICTNESS_MODE
    is_relaxed = (ENTRY_STRICTNESS_MODE == "relaxed")

    if is_relaxed:
        max_adverse_dev = 0.010  # 寬鬆模式下放寬至 1.0%
    else:
        max_adverse_dev = max(0.003, min(0.018, atr_pct * 1.2 if atr_pct > 0 else 0.006))
        if is_rescue_dca:
            max_adverse_dev = min(max_adverse_dev, 0.006)

    adverse_dev = (order_price - market_price) / market_price if side == "buy" else (market_price - order_price) / market_price
    if adverse_dev > max_adverse_dev:
        return False, f"adverse price deviation {adverse_dev*100:.2f}% > {max_adverse_dev*100:.2f}%"

    # 原本這裡只在 market/chase 模式檢查「總偏移」，pullback（弱訊號預設模式）完全
    # 跳過，只靠上面的 adverse_dev 擋。但 adverse_dev 只抓「往不利方向」的偏移——
    # 如果價格在訊號產生後往「有利」方向暴衝（例如訊號價 0.000893、實際牌價已經衝到
    # 0.000922，買方向來說不算 adverse），會被當成正常情況直接放行，實際上等於在
    # 追價格噴發後的高點進場（TAGUSDT 實測案例：3.25% 落差，pullback 模式完全沒被
    # 攔下）。總偏移檢查不分方向，只要訊號價跟目前牌價差太多就攔，不再限定 mode，
    # 才能同時擋住「往不利方向追」跟「追噴發高點」兩種情況。
    total_dev = abs(order_price - market_price) / market_price
    if is_relaxed:
        chase_limit = 0.008  # 寬鬆模式下追價極限放寬至 0.8%
    else:
        chase_limit = max(0.003, min(0.010, atr_pct * 0.8 if atr_pct > 0 else 0.004))
    if mode != "ma7_pullback" and total_dev > chase_limit:
        return False, f"chase price drift {total_dev*100:.2f}% > {chase_limit*100:.2f}%"

    return True, "ok"


def _reanchor_rejected_passive_price(sym, side, order_price, market_price, mode="pullback"):
    """將過度偏離的被動委託沿最新市價平移，保留回踩折價但不放寬價格保護。"""
    order_price = float(order_price or 0.0)
    market_price = float(market_price or 0.0)
    if order_price <= 0 or market_price <= 0:
        return order_price, False
    if (side == "buy" and order_price >= market_price) or (side == "sell" and order_price <= market_price):
        return order_price, False

    distance = order_price - market_price
    for retained_distance in (0.75, 0.50, 0.25, 0.10, 0.0):
        candidate = market_price + distance * retained_distance
        allowed, _ = _entry_price_guard(
            sym, side, candidate, market_price, mode=mode, is_rescue_dca=False
        )
        if allowed:
            return candidate, candidate != order_price
    return order_price, False


def _ma25_confirmed_pullback_price(side, structure_price, market_price, atr):
    """MA25 已確認回踩後只保留小幅被動價差，避免再等一次深層結構回踩。"""
    structure_price = float(structure_price or 0.0)
    market_price = float(market_price or 0.0)
    atr = float(atr or 0.0)
    if structure_price <= 0 or market_price <= 0:
        return structure_price
    max_distance = max(market_price * 0.0005, min(market_price * 0.0025, atr * 0.5))
    if str(side).lower() == "buy":
        return min(max(structure_price, market_price - max_distance), market_price * 0.9997)
    return max(min(structure_price, market_price + max_distance), market_price * 1.0003)


def _entry_signal_chase_guard(side, signal_price, order_price, is_first_entry=True,
                              is_rescue_dca=False):
    """Prevent a fresh position from chasing materially beyond its signal price."""
    if not is_first_entry or is_rescue_dca:
        return True, "not_first_entry"
    signal_price = float(signal_price or 0.0)
    order_price = float(order_price or 0.0)
    if signal_price <= 0 or order_price <= 0:
        return False, "missing signal or order price"
    side = str(side).lower()
    adverse_chase = (
        (order_price - signal_price) / signal_price
        if side == "buy"
        else (signal_price - order_price) / signal_price
    )

    # 首次 MA 進場不因全域 relaxed 模式放寬：避免突破後才追在短線高/低點。
    max_chase_pct = 0.0015

    if adverse_chase > max_chase_pct:
        return False, f"signal chase {adverse_chase*100:.3f}% > {max_chase_pct*100:.2f}%"
    return True, "ok"


def is_effective_rescue_dca(s, side, order_price, add_qty=None):
    """確認救援單確實能改善現有均價，而不是只放大曝險。"""
    avg_price = float(s.get("avg_price", 0.0) or 0.0)
    current_qty = float(s.get("qty", 0.0) or 0.0)
    order_price = float(order_price or 0.0)
    side = str(side).lower()
    if avg_price <= 0 or abs(current_qty) <= 0 or order_price <= 0:
        return False, "missing position or price"
    current_side = "buy" if current_qty > 0 else "sell"
    if side != current_side:
        return False, f"opposite side {side} is a reversal, not rescue DCA"

    hard_sl_pct = float(s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT) or HARD_STOP_LOSS_PCT)
    stop_price = avg_price * (1 - hard_sl_pct) if current_side == "buy" else avg_price * (1 + hard_sl_pct)
    stop_buffer = 0.001
    if current_side == "buy" and order_price <= stop_price * (1 + stop_buffer):
        return False, f"rescue price {order_price:.6f} is too close/below stop {stop_price:.6f}"
    if current_side == "sell" and order_price >= stop_price * (1 - stop_buffer):
        return False, f"rescue price {order_price:.6f} is too close/above stop {stop_price:.6f}"

    atr = float(s.get("current_atr", 0.0) or 0.0)
    # 門檻從 0.8% 降到 0.4%：實測 ENAUSDT 即將停損時價差只有 0.72%，差一點點就不到
    # 0.8%，救援攤平被判定無效、直接照計畫停損（-0.55%）。使用者要求讓救援更容易
    # 成功，降低門檻讓這類「已經有一定價差、只是差臨門一腳」的情況也能真的攤到平，
    # 換取更多機會等利潤回來；代價是攤平會在更小的逆勢幅度就出手，均價改善的
    # 幅度也會變小。
    min_gap_pct = max(0.004, (atr / avg_price) * 1.1)
    favorable_gap_pct = ((avg_price - order_price) / avg_price if side == "buy"
                          else (order_price - avg_price) / avg_price)
    if favorable_gap_pct < min_gap_pct:
        return False, (f"price gap {favorable_gap_pct*100:.2f}% < required "
                       f"{min_gap_pct*100:.2f}%")
    if add_qty is not None:
        add_qty = abs(float(add_qty or 0.0))
        if add_qty <= 0:
            return False, "rescue quantity is zero"
        old_qty = abs(current_qty)
        projected_avg = ((avg_price * old_qty) + (order_price * add_qty)) / (old_qty + add_qty)
        improvement_pct = abs(projected_avg - avg_price) / avg_price
        if improvement_pct < 0.0035:
            return False, (f"projected average improvement {improvement_pct*100:.2f}% "
                           f"< required 0.35%")
    return True, "ok"


def _entry_pending_adverse_guard(sym, side, reference_price, current_price, is_rescue_dca=False):
    """Return False when a pending entry has moved too far against the original signal.

    This is deliberately stricter than the generic order-price guard: once a limit
    order is sitting in the book, a fast adverse move often means the setup has
    changed from "patient entry" to "catching a falling/rising market".
    """
    if not reference_price or not current_price or reference_price <= 0 or current_price <= 0:
        return True, "no_ref"

    s = ctx.STATES.get(sym, {})
    atr = float(s.get("current_atr", 0.0) or 0.0)
    atr_pct = atr / reference_price if reference_price > 0 else 0.0
    max_adverse_dev = max(0.0025, min(0.012, atr_pct * 0.8 if atr_pct > 0 else 0.004))
    if is_rescue_dca:
        max_adverse_dev = min(max_adverse_dev, 0.005)

    adverse_dev = (reference_price - current_price) / reference_price if side == "buy" else (current_price - reference_price) / reference_price
    if adverse_dev > max_adverse_dev:
        return False, f"pending adverse move {adverse_dev*100:.2f}% > {max_adverse_dev*100:.2f}%"

    return True, "ok"


def _pending_entry_setup_valid(info, validator=None):
    """Revalidate a resting first-entry order against the latest full signal."""
    if info.get("is_rescue_dca", False):
        return True, "rescue_dca"
    route = info.get("entry_route")
    if not route:
        return True, "legacy_order"
    if validator is None:
        from core.check_entries import is_entry_candidate_still_valid
        validator = is_entry_candidate_still_valid
    # 掛單期間以最新市價驗證；方向、MA、RSI 或大盤結構失效時仍會撤單。
    state = ctx.STATES.get(info.get("sym"), {})
    latest_price = float(state.get("close_price", 0.0) or 0.0)
    validation_price = latest_price or float(info.get("signal_price") or info.get("price") or 0.0)
    return validator(
        info.get("sym"), info.get("side"), route,
        float(info.get("signal_strength", 0.0) or 0.0),
        validation_price,
    )


def _ma_cross_anti_chase_plan(sym, side, current_price, entry_route):
    """Return a MA7-anchored limit when a fresh MA cross is already extended."""
    if str(entry_route or "").lower() != "ma_cross":
        return False, 0.0, "not_ma_cross"
    current_price = float(current_price or 0.0)
    s = ctx.STATES.get(sym, {})
    ma7 = float(s.get("ma7", 0.0) or 0.0)
    atr = float(s.get("current_atr", 0.0) or 0.0)
    if current_price <= 0 or ma7 <= 0:
        return False, 0.0, "missing_price_or_ma7"

    extension = current_price - ma7 if side == "buy" else ma7 - current_price
    max_extension = max(
        current_price * MA_CROSS_MAX_EXTENSION_PCT,
        atr * MA_CROSS_MAX_EXTENSION_ATR_MULT,
    )
    anchor_price = (
        ma7 * (1 + MA_CROSS_ANCHOR_BUFFER_PCT)
        if side == "buy"
        else ma7 * (1 - MA_CROSS_ANCHOR_BUFFER_PCT)
    )
    if extension <= max_extension:
        return False, anchor_price, "price_near_ma7"

    return True, anchor_price, (
        f"price/MA7 extension {extension/current_price*100:.2f}% > "
        f"{max_extension/current_price*100:.2f}%"
    )


def _is_ma_pending_entry(info):
    return (str(info.get("entry_route") or "").lower() in MA_ENTRY_ROUTES
            and not info.get("is_rescue_dca", False))


def _is_dynamic_pending_entry(info):
    route = str(info.get("entry_route") or "").lower()
    return (route in MA_ENTRY_ROUTES or route in RANGE_ENTRY_ROUTES) and not info.get("is_rescue_dca", False)


def _pending_entry_reprice_needed(sym, info, current_price, now=None):
    """Detect a moved MA or range anchor without imposing a fixed order timeout."""
    if not _is_dynamic_pending_entry(info) or current_price <= 0:
        return False, "not_dynamic_strategy_order"
    now = float(time.time() if now is None else now)
    last_reprice_at = float(info.get("last_reprice_at",
        info.get("timestamp", info.get("placed_at", 0.0))) or 0.0)
    if now - last_reprice_at < MA_PENDING_REPRICE_COOLDOWN_SEC:
        return False, "reprice_cooldown"
    market_ref = float(info.get("market_reference_price")
                       or info.get("signal_price") or 0.0)
    if market_ref <= 0:
        return False, "missing_market_reference"
    s = ctx.STATES.get(sym, {})
    atr = float(s.get("current_atr", 0.0) or 0.0)
    route = str(info.get("entry_route") or "").lower()
    if route in RANGE_ENTRY_ROUTES:
        support = float(s.get("range_support_level", 0.0) or 0.0)
        resistance = float(s.get("range_resistance_level", 0.0) or 0.0)
        desired_price = (
            support * (1 + RANGE_ENTRY_EDGE_BUFFER_PCT) if route == "range_support_long"
            else resistance * (1 - RANGE_ENTRY_EDGE_BUFFER_PCT)
        )
        old_limit = float(info.get("price") or info.get("limit_price") or 0.0)
        if desired_price <= 0 or old_limit <= 0:
            return False, "missing_range_anchor"
        anchor_drift_limit = max(current_price * 0.0005, atr * 0.10)
        if abs(desired_price - old_limit) >= anchor_drift_limit:
            return True, "range anchor moved"
        return False, "range anchor unchanged"
    if info.get("ma_cross_anti_chase", False):
        _, desired_price, _ = _ma_cross_anti_chase_plan(
            sym, info.get("side"), current_price, info.get("entry_route"),
        )
        old_limit = float(info.get("price") or info.get("limit_price") or 0.0)
        if desired_price > 0 and old_limit > 0:
            anchor_drift_limit = max(current_price * 0.0005, atr * 0.10)
            if abs(desired_price - old_limit) >= anchor_drift_limit:
                return True, "MA7 anchor moved"
        # 防追價單的正確報價由 MA7 決定；只有市價移動、MA7 未動時不做無效撤掛，
        # 避免白白增加 API 權重。訊號有效性仍由每輪完整重驗負責。
        return False, "MA7 anchor unchanged"
    atr_pct = atr / market_ref
    # 這只決定舊報價何時重算，不增加開倉門檻。
    drift_limit = max(0.0025, min(0.008, atr_pct * 0.6 if atr_pct > 0 else 0.004))
    drift = abs(current_price - market_ref) / market_ref
    if drift >= drift_limit:
        return True, f"market drift {drift*100:.2f}% >= {drift_limit*100:.2f}%"
    return False, "price_still_near_reference"


def _translated_pending_limit_price(info, current_price):
    """Re-anchor a pending limit to current MA or horizontal range structure."""
    route = str(info.get("entry_route") or "").lower()
    if route in RANGE_ENTRY_ROUTES:
        s = ctx.STATES.get(info.get("sym"), {})
        if route == "range_support_long":
            support = float(s.get("range_support_level", 0.0) or 0.0)
            return support * (1 + RANGE_ENTRY_EDGE_BUFFER_PCT) if support > 0 else 0.0
        resistance = float(s.get("range_resistance_level", 0.0) or 0.0)
        return resistance * (1 - RANGE_ENTRY_EDGE_BUFFER_PCT) if resistance > 0 else 0.0
    if info.get("ma_cross_anti_chase", False):
        _, anchor_price, _ = _ma_cross_anti_chase_plan(
            info.get("sym"), info.get("side"), current_price,
            info.get("entry_route"),
        )
        if anchor_price > 0:
            return anchor_price
    old_limit = float(info.get("price") or info.get("limit_price") or 0.0)
    old_market = float(info.get("market_reference_price")
                       or info.get("signal_price") or 0.0)
    if old_limit <= 0 or old_market <= 0 or current_price <= 0:
        return 0.0
    translated = old_limit + (current_price - old_market)
    if str(info.get("side", "")).lower() == "buy":
        return min(translated, current_price * 0.9997)
    return max(translated, current_price * 1.0003)


def _find_exchange_position(positions, sym):
    for pos in positions or []:
        raw_symbol = str(pos.get("symbol", ""))
        pos_sym = raw_symbol.split(":")[0].replace("/", "")
        raw_amt = pos.get("info", {}).get("positionAmt")
        qty = float(raw_amt) if raw_amt is not None else float(pos.get("contracts", 0) or 0)
        if raw_amt is None and str(pos.get("side", "")).lower() == "short":
            qty = -abs(qty)
        if pos_sym == sym and abs(qty) > 0.000001:
            return pos, qty
    return None, 0.0


async def _recover_market_entry_fill(sym, side, prior_qty, requested_qty, fallback_price):
    try:
        positions = await exchange_futures.fetch_positions([sym])
    except Exception as exc:
        logger.info(f"⚠️ [市價成交回查失敗] {sym}: {exc}")
        return None

    pos, exchange_qty = _find_exchange_position(positions, sym)
    requested_sign = 1 if side == "buy" else -1
    if pos is None or exchange_qty * requested_sign <= 0:
        return None

    filled_qty = (exchange_qty - prior_qty) * requested_sign
    if filled_qty <= 0.000001:
        return None
    filled_qty = min(filled_qty, requested_qty)
    fill_price = float(
        pos.get("entryPrice")
        or pos.get("info", {}).get("entryPrice")
        or fallback_price
    )
    return {
        "status": "closed",
        "filled": filled_qty,
        "average": fill_price,
        "_recovered_from_position": True,
    }


async def _entry_exchange_direction_guard(sym, side):
    if PAPER_TRADING:
        return True, "paper"
    try:
        positions = await exchange_futures.fetch_positions([sym])
    except Exception as exc:
        return False, f"position preflight failed: {exc}"

    pos, exchange_qty = _find_exchange_position(positions, sym)
    if pos is None:
        return True, "flat"

    s = ctx.STATES[sym]
    local_qty = float(s.get("qty", 0.0) or 0.0)
    exchange_avg = float(pos.get("entryPrice") or pos.get("info", {}).get("entryPrice") or 0.0)
    if abs(local_qty) <= 0.000001 or local_qty * exchange_qty < 0:
        s["qty"] = exchange_qty
        if exchange_avg > 0:
            s["avg_price"] = exchange_avg
        return False, (
            f"local position stale: local={local_qty:.6f}, "
            f"exchange={exchange_qty:.6f}"
        )

    requested_sign = 1 if side == "buy" else -1
    if exchange_qty * requested_sign < 0:
        return False, f"exchange already has opposite position {exchange_qty:.6f}"
    return True, "ok"


def record_trade_result(symbol, entry_reason, exit_reason, profit_pct, current_atr, max_profit_reached=0.0,
                        expected_entry=0.0, expected_exit=0.0, actual_entry=0.0, actual_exit=0.0,
                        fees=0.0, qty=0.0, exchange_close_id=None,
                        realized_pnl_usdt=None, timestamp_ms=None, entry_timestamp_ms=None, side=""):
    """
    將每筆交易的結果記錄到 trade_history.json 中，並生成 AI 友好的經驗摘要。
    """
    history_file = TRADE_HISTORY_FILE

    # --- 原有摩擦力計算邏輯 ---
    entry_slippage = abs(actual_entry - expected_entry) if expected_entry > 0 else 0.0
    exit_slippage = abs(actual_exit - expected_exit) if expected_exit > 0 else 0.0
    total_slippage = entry_slippage + exit_slippage
    slippage_cost = total_slippage * qty if qty > 0 else 0.0
    total_friction = slippage_cost + fees
    total_value = actual_entry * qty if (actual_entry > 0 and qty > 0) else 1.0
    friction_rate = (total_friction / total_value) * 100 if total_value > 0 else 0.0
    # 交易所 realizedPnl 與價格報酬都不含 commission；歷史績效必須以扣除
    # 本次完整進出場手續費後的淨報酬為準。保留 gross 欄位供滑價分析。
    gross_profit_pct = float(profit_pct or 0.0)
    fee_rate = (float(fees or 0.0) / total_value) if total_value > 0 else 0.0
    profit_pct = gross_profit_pct - fee_rate

    # --- 新增：AI 經驗摘要生成邏輯 ---
    pnl_tag = "[大賺]" if profit_pct > 0.01 else "[微利]" if profit_pct > 0.002 else "[打平]" if profit_pct > -0.002 else "[小虧]" if profit_pct > -0.01 else "[大虧]"

    anomaly_tags = []
    if friction_rate > 0.4:
        anomaly_tags.append("HIGH_FRICTION")
    if max_profit_reached >= 0.005 and max_profit_reached - profit_pct >= 0.005:
        anomaly_tags.append("PEAK_GIVEBACK")
    if any(tag in str(exit_reason) for tag in (
        "MA_Wrong_Direction", "MA_Active_Risk_Stop", "MA_Early_Momentum_Flip"
    )):
        anomaly_tags.append("WRONG_DIRECTION")
    if "MA_Disaster_Stop" in str(exit_reason):
        anomaly_tags.append("DISASTER_STOP")
    if total_value > 0 and slippage_cost / total_value > 0.002:
        anomaly_tags.append("HIGH_SLIPPAGE")

    summary = f"{pnl_tag} {symbol} 透過 {exit_reason} 出場。獲利 {profit_pct*100:.2f}%，摩擦力 {friction_rate:.2f}%。"
    if anomaly_tags:
        summary += f" (⚠️ AI複盤標記：{' / '.join(anomaly_tags)})"

    trade_data = {
        "timestamp": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(timestamp_ms / 1000.0))
            if timestamp_ms else time.strftime("%Y-%m-%d %H:%M:%S")
        ),
        "entry_timestamp_ms": int(entry_timestamp_ms) if entry_timestamp_ms else None,
        "symbol": symbol,
        "entry_reason": entry_reason or "UNKNOWN",
        "exit_reason": exit_reason,
        "profit_pct": round(profit_pct, 4),
        "gross_profit_pct": round(gross_profit_pct, 4),
        "max_profit_reached": round(max_profit_reached, 4),
        "atr_at_exit": round(current_atr, 6),
        "market_mode": "High_Vol" if current_atr > 0.005 else "Low_Vol",
        "expected_entry": round(expected_entry, 6),
        "expected_exit": round(expected_exit, 6),
        "actual_entry": round(actual_entry, 6),
        "actual_exit": round(actual_exit, 6),
        "fees": round(fees, 4),
        "qty": round(qty, 4),
        "slippage": round(total_slippage, 6),
        "friction_rate": round(friction_rate, 4),
        "side": str(side or "").lower(),
        "theoretical_profit": round(((expected_exit - expected_entry)/expected_entry * (1.0 if str(side).lower() == "buy" else -1.0)) if expected_entry > 0 else 0.0, 4),
        "ai_summary": summary,
        "ai_anomaly_tags": anomaly_tags,
        "ai_review_priority": min(100, len(anomaly_tags) * 25 + (25 if profit_pct < -0.01 else 0))
    }
    if exchange_close_id is not None:
        trade_data["exchange_close_id"] = str(exchange_close_id)
    if realized_pnl_usdt is not None:
        gross_realized_pnl = float(realized_pnl_usdt)
        trade_data["realized_pnl_usdt"] = round(gross_realized_pnl, 8)
        trade_data["net_realized_pnl_usdt"] = round(gross_realized_pnl - float(fees or 0.0), 8)

    # API 與交易機器人是兩個獨立程序，原本同時「讀整份 JSON -> append -> 覆寫」
    # 會發生 lost update，後寫者會把前一筆剛寫好的交易蓋掉。鎖住完整的
    # read-modify-write，並用原子替換避免前端讀到只寫了一半的 JSON。
    history_dir = os.path.dirname(os.path.abspath(history_file))
    os.makedirs(history_dir, exist_ok=True)
    lock_file = f"{history_file}.lock"
    temp_file = f"{history_file}.tmp.{os.getpid()}"
    try:
        with open(lock_file, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            if os.path.exists(history_file):
                with open(history_file, "r", encoding="utf-8") as history_handle:
                    try:
                        history = json.load(history_handle)
                        if not isinstance(history, list):
                            history = []
                    except (json.JSONDecodeError, OSError):
                        history = []
            else:
                history = []

            if exchange_close_id is not None and any(
                str(item.get("exchange_close_id")) == str(exchange_close_id)
                for item in history
            ):
                logger.info(f"ℹ️ [ExternalClose] {symbol} 平倉成交 {exchange_close_id} 已記錄，略過重複寫入")
                return False

            history.append(trade_data)
            with open(temp_file, "w", encoding="utf-8") as temp_handle:
                json.dump(history, temp_handle, indent=4, ensure_ascii=False)
                temp_handle.flush()
                os.fsync(temp_handle.fileno())
            os.replace(temp_file, history_file)
        logger.info(f"📝 [AI Memory] 已記錄 {symbol} 並產生摘要: {summary}")
        try:
            ai_engine.schedule_auto_review_if_due(len(history))
        except Exception as ai_exc:
            logger.warning(f"🤖 [AI 自動複盤] 排程失敗但交易紀錄已保存：{ai_exc}")
        return True
    except Exception as e:
        logger.info(f"⚠️ [AI Memory] 紀錄失敗: {e}")
        try:
            if os.path.exists(temp_file):
                os.unlink(temp_file)
        except OSError:
            pass
        # None=真正寫入失敗；False 只代表同一 exchange_close_id 已存在。
        return None


async def _market_close_and_get_fill(sym, close_side, qty, fallback_price):
    """送出市價平倉單，並可靠地取得真實成交均價。create_order() 剛回傳的市價單
    結果，average/price 欄位常常還沒填（要過一下子交易所才處理完），如果直接信任
    這個回傳值，會退回去用呼叫端傳入的理論價格算獲利——這正是 PeakLock/RESCUE_TRAIL
    等鎖利機制「內部顯示賺錢、實際上虧損」的成因之一。這裡改成下單後主動再查一次
    訂單狀態，確保拿到的是交易所真正的成交均價。

    幣安期貨的 MARKET_LOT_SIZE 過濾器對市價單另外設有比一般 LOT_SIZE 更低的單筆數量
    上限（實測 KAITOUSDT：LOT_SIZE 上限 100 萬，MARKET_LOT_SIZE 卻只有 500）。用限價
    掛進場時不受這限制，倉位可能建到遠超過這個上限，但平倉時若整包數量一次送出就會被
    直接拒絕（-4005 Quantity greater than max quantity），導致部位卡住平不掉、完全沒有
    交易所端保護（真實發生過：KAITOUSDT 744 顆，止損/停利掛不上、市價平倉連續失敗）。
    這裡改成依交易所回報的上限自動切成多筆市價單分批送出，確保無論部位多大都平得掉。"""
    prec = await get_contract_precision(sym)
    max_qty = prec.get('market_max_qty')
    step = prec.get('step_size', 0.001)

    chunks = []
    if max_qty and max_qty > 0 and qty > max_qty:
        remaining = qty
        while remaining > 0.000001:
            chunk = min(remaining, max_qty)
            chunk = round_step(chunk, step) if step > 0 else chunk
            if chunk <= 0:
                break
            chunks.append(chunk)
            remaining -= chunk
    else:
        chunks = [qty]

    total_filled_qty = 0.0
    total_filled_notional = 0.0
    for i, chunk_qty in enumerate(chunks):
        market_order = await exchange_futures.create_order(
            sym, type="market", side=close_side, amount=chunk_qty,
            params={"reduceOnly": True}
        )
        chunk_fill_price = float(market_order.get('average') or market_order.get('price') or 0.0)
        if chunk_fill_price <= 0:
            await asyncio.sleep(0.5)
            try:
                fetched = await exchange_futures.fetch_order(market_order['id'], sym)
                chunk_fill_price = float(fetched.get('average') or fetched.get('price') or 0.0)
            except Exception as fe:
                logger.info(f"⚠️ [市價成交查詢失敗] {sym}: {fe}")
        if chunk_fill_price <= 0:
            chunk_fill_price = fallback_price
        total_filled_qty += chunk_qty
        total_filled_notional += chunk_qty * chunk_fill_price
        if len(chunks) > 1:
            logger.info(f"📦 [市價分批平倉] {sym} 第 {i+1}/{len(chunks)} 批 {chunk_qty:.4f} @ {chunk_fill_price:.6f}（單筆市價單上限 {max_qty}）")

    if total_filled_qty <= 0:
        return fallback_price
    return total_filled_notional / total_filled_qty


async def _exit_lock_profit_with_chase(sym, close_side, qty, price):
    """實盤鎖利出場：限價掛在理論價位，沒成交就追到當下最新買一/賣一價再試一次，
    還是掛不到才轉市價出清剩餘部位。比起單純「掛一個價位等到逾時才轉市價」，
    多一次貼近市場的追價機會，同時全程都用真實成交均價回填，不用理論價格。"""
    prec = await get_contract_precision(sym)
    filled_qty = 0.0
    filled_notional = 0.0
    remaining_qty = qty
    limit_price = round_step(price, prec['tick_size'])

    for attempt in (1, 2):
        try:
            # 原本用 GTC + 4 秒等待，兩輪加市價備援最長要等 9 秒左右——實測 SYNUSDT
            # 峰值 0.66% 理論鎖利 0.2%+，就是在這 4~9 秒的等待期間價格繼續走遠，
            # 最後不管哪一輪成交都已經比理論價差很多，倒虧收場。改用 IOC：能立刻
            # 成交的部分馬上成交，不能成交的部分立刻取消，不再讓部位在關鍵幾秒內
            # 曝險等一個不確定會不會成交的掛單。
            order = await exchange_futures.create_order(
                sym, type='limit', side=close_side, amount=remaining_qty,
                price=limit_price, params={'reduceOnly': True, 'timeInForce': 'IOC'}
            )
        except Exception as e:
            logger.info(f"🚨 [鎖利限價下單失敗] {sym} (第{attempt}次): {e}")
            break

        await asyncio.sleep(0.3)
        step_filled = 0.0
        try:
            fetched = await exchange_futures.fetch_order(order['id'], sym)
            step_filled = float(fetched.get('filled', 0) or 0)
            fill_avg = float(fetched.get('average') or fetched.get('price') or limit_price)
            filled_notional += step_filled * fill_avg
            filled_qty += step_filled
        except Exception as fe:
            logger.info(f"⚠️ [限價單查詢失敗] {sym}: {fe}")

        remaining_qty = round_step(qty - filled_qty, prec['step_size']) if filled_qty < qty else 0.0
        if remaining_qty <= 0.000001:
            logger.info(f"✅ [限價鎖利成交] {sym} 第{attempt}次掛單全數成交 @ {filled_notional/filled_qty:.6f}")
            return filled_notional / filled_qty

        try:
            await exchange_futures.cancel_order(order['id'], sym)
        except Exception:
            pass

        if attempt == 1:
            try:
                ob = await exchange_futures.fetch_order_book(sym, limit=5)
                asks = ob.get('asks', [])
                bids = ob.get('bids', [])
                best_bid = float(bids[0][0]) if bids else limit_price
                best_ask = float(asks[0][0]) if asks else limit_price
                reprice = best_bid if close_side == 'sell' else best_ask
                limit_price = round_step(reprice, prec['tick_size'])
                logger.info(f"🔁 [鎖利限價追價] {sym} IOC未完全成交（已成交 {filled_qty:.6f}/{qty:.6f}），改掛貼近市場價 {limit_price:.6f} 再試")
            except Exception as re_e:
                logger.info(f"⚠️ [追價報價失敗] {sym}: {re_e}，維持原價再試一次")

    if remaining_qty > 0.000001:
        logger.info(f"⏱️ [鎖利限價逾時] {sym} 追價後仍未完全成交（已成交 {filled_qty:.6f}/{qty:.6f}），剩餘 {remaining_qty:.6f} 改市價出場")
        market_fill = await _market_close_and_get_fill(sym, close_side, remaining_qty, price)
        filled_notional += remaining_qty * market_fill
        filled_qty += remaining_qty

    return (filled_notional / filled_qty) if filled_qty > 0 else limit_price


async def close_position(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = ctx.STATES[sym]
    if s.get("_external_close_record_pending", False):
        logger.info(f"⏳ [ExternalClose] {sym} 交易所已無倉位，正等待平倉成交寫入歷史，不重複送出平倉單")
        return
    await _close_position_inner(sym, close_side, qty, price, avg_price, reason, is_stop_loss)


async def _close_position_inner(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = ctx.STATES[sym]
    s["adjusted_this_tick"] = True

    # ── 防重複平倉鎖（Duplicate Close Guard）──
    # asyncio 雖然單執行緒，但 await 點會讓另一個 coroutine 插入執行，
    # 兩個呼叫者同時通過 qty 檢查後都去執行平倉 → 重複平倉。
    if s.get("_is_closing", False):
        logger.info(f"⚠️ [DuplicateClose] {sym} 已有平倉指令執行中，忽略重複呼叫 | reason={reason}")
        return
    s["_is_closing"] = True
    try:
        # Cancel resting entry orders before a stop/TP close; otherwise they can fill after
        # the close and silently reopen or reverse the position.
        if not PAPER_TRADING:
            for order_id, info in list(ctx.PENDING_LIMIT_ORDERS.items()):
                if info.get("sym") != sym:
                    continue
                try:
                    await exchange_futures.cancel_order(order_id, sym)
                    logger.info(f"✅ [平倉前撤單] {sym} 已撤銷殘留進場單 {order_id}")
                except Exception as exc:
                    logger.info(f"ℹ️ [平倉前撤單] {sym} 進場單 {order_id} 無法撤銷或已成交: {exc}")
                finally:
                    ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)
        await _close_position_inner_locked(sym, close_side, qty, price, avg_price, reason, is_stop_loss)
    finally:
        s["_is_closing"] = False


async def _close_position_inner_locked(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = ctx.STATES[sym]

    # 強化防禦：檢查實際持倉與指令方向是否衝突
    actual_qty = s.get("qty", 0)
    if abs(actual_qty) > 0.000001:
        expected_close_side = "sell" if actual_qty > 0 else "buy"
        if close_side != expected_close_side:
            logger.info(f"🚨 [CRITICAL_ERROR] {sym} 平倉方向衝突！持倉為 {'多' if actual_qty > 0 else '空'}，但指令要求 {close_side}。| reason={reason}")
            logger.info(f"🔄 [CRITICAL_ERROR] {sym} 正在自動修正指令為 {expected_close_side} 以確保正確平倉。")
            close_side = expected_close_side  # 強制修正方向

    if not price or price <= 0:
        price = s.get("close_price", 0.0) or s.get("avg_price", 0.0)
        if price <= 0:
            logger.info(f"[REJECT_ZERO_PRICE] {sym} 平倉價格為 0，已攔截！")
            return
        logger.info(f"[WARN_ZERO_PRICE] {sym} 平倉價格補救為 {price:.6f}")
    if abs(s["qty"]) < 0.000001:
        return
    pk = paper_key(sym)
    qty = min(abs(qty), abs(s["qty"]))
    if qty < 0.000001:
        return

    real_avg = s["avg_price"] if s["avg_price"] > 0 else avg_price
    profit_pct = (price - real_avg) / real_avg if s["qty"] > 0 else (real_avg - price) / real_avg

    # 根據幣種波動度動態決定最低利潤門檻，高波動幣種拉大獲利要求 (1.5%) 以優化盈虧比，主流幣維持 0.35%
    volatile_coins = ["ORDIUSDT", "INJUSDT", "SUIUSDT", "APTUSDT", "GUAUSDT", "SIRENUSDT"]
    fee_buffer = 0.015 if sym.replace(":", "") in volatile_coins else 0.0035

    # 允許任何型態的止損（is_stop_loss=True）、全域熔斷、或策略主動平倉原因，以避免時間停滯等優化退場機制被攔截
    # Peak_Giveback 是動態鎖利／回撤保護，不能再被一般 0.35% 或高波動幣
    # 1.5% 的固定獲利門檻擋掉，否則會出現「觸發停利卻拒絕平倉」。
    # [Trend_Follow]/[Breakeven_Stop] 加入白名單：Universal SL 在 sl 仍位於獲利側時
    # （PeakLock 鎖利被回吐、但還沒真正虧損）會改用 is_stop_loss=False 走鎖利追價流程，
    # 此時理論利潤可能低於一般 0.35%/1.5% 門檻，若被這裡攔下會讓部位卡在保護線之下
    # 卻遲遲不出場，比直接放行更危險。
    # [Dynamic_Exit_Manager] 自己內建 0.15% 啟動門檻 + 回撤/耐心逾時/盤整三選一的判斷才會
    # 決定出場，不是隨便一點點獲利就賣——不加進白名單的話，這裡的 0.35% 固定門檻會蓋掉
    # 它自己已經做過的判斷，等於它的 0.15% 設定形同虛設，永遠要等到 0.35% 才放行。
    # （[Opportunity_Rotation] 機會成本輪替功能已依使用者要求移除，不會再產生這個 reason。）
    # [MA7_Closed_Break]/[MA_Peak_Lock] 補進白名單：這兩個是 MA_Lifecycle_Exit 家族
    # 跟已經在白名單裡的 [MA7_MA25_Death_Cross]/[MA7_MA25_Golden_Cross] 同一組訊號，
    # 只是「MA7 單純跌破/站上」而非「MA7/25 交叉反轉」。實測 TAOUSDT 案例：MA7 已確認
    # 兩次收線跌破（信號正確），但當下利潤只有 0.02%~0.04%，被這裡的 0.35% 門檻連續
    # 攔截了 4 次、每次都不放行，一直拖到價格真的轉負才被迫用 is_stop_loss=True 補放行
    # ——本來該在轉折剛確認時就平倉了結的單子，硬生生被拖成真正的停損虧損出場。
    # [Sell_Pressure_Exit]/[Buy_Pressure_Exit] 補進白名單：實測 XRPUSDT 案例——賣壓
    # 機制在浮盈 0.29%~0.30% 時正確偵測到逆勢量能主導、想出場，卻被這裡的 0.35%
    # 門檻連續攔截 4 次，2~3 秒後才改由反應更慢的交易所端鎖利止損單接手，成交在
    # 更差的價位，本來能是小賺的單子變成 -0.05% 虧損出場。這兩個出場理由本來就是
    # 為了比鎖利線更早示警才設計的，不能被同一道門檻卡住、逼它退化成鎖利線的備胎。
    allowed_exit_reasons = ["[MA_Wrong_Direction_Confirmed]", "[MA_Disaster_Stop]", "[MA7_MA25_Death_Cross]", "[MA7_MA25_Golden_Cross]", "[MA7_Closed_Break]", "[MA7_Profit_Turn_Partial]", "[MA7_Profit_Turn_Confirmed]", "[MA_Peak_Lock]", "[MA_Profit_Floor]", "[Sell_Pressure_Exit]", "[Buy_Pressure_Exit]", "[Range_Mid_Target]", "[GLOBAL_MELTDOWN]", "[Peak_Giveback]", "[TrailTP_Peak]", "[Dynamic_Trailing]", "[Range_Trailing_Closed_Confirm]", "[Momentum_Tracker]", "[Hard_Profit_Cap]", "[Stagnation_Stop]", "[Stagnation_Timeout]", "[Trend_Follow]", "[Breakeven_Stop]", "[High_Point_Stagnation]", "[Dynamic_Exit_Manager]", "[Peak_Volume_Contraction]"]
    if profit_pct < fee_buffer and not is_stop_loss and reason not in allowed_exit_reasons:
        logger.info(f"⏳ [平倉攔截] {sym} 目前利潤 ({profit_pct*100:.4f}%) 未達最低利潤門檻 ({fee_buffer*100:.2f}%)，已拒絕平倉 | 原因={reason}")
        return




    sanitized_qty = await sanitize_order_qty(sym, qty)
    if sanitized_qty <= 0.0:
        logger.info(f"⚠️ [平倉風控] {sym} 無法取得有效數量 ({qty:.6f})")
        return
    qty = sanitized_qty
    # 從送單到查成交、手續費期間會有多個 await。剩餘數量必須以本次平倉開始時
    # 的本地數量為基準，不能讀取可能被其他背景流程更新過的 s["qty"]。
    position_qty_before_close = float(s["qty"])

    if PAPER_TRADING:
        # 紙上交易沒有真實委託簿，成交價就是模擬假設的理論價，不會有滑價落差。
        real_avg = s["avg_price"] if s["avg_price"] > 0 else avg_price
        if s["qty"] > 0:
            pnl = (price - real_avg) * qty
        else:
            pnl = (real_avg - price) * qty
        update_paper_state(pk, close_side, price, qty, is_close=True, pnl=pnl)
        final_price = price
    else:
        # 實盤平倉：獲利中（PeakLock/停利鎖利）用限價單掛在理論價位，試著真的鎖住
        # 這個價位的獲利；虧損中（真正停損）維持市價單，優先保證一定出場，
        # 不能讓限價單沒成交而讓虧損繼續擴大。
        # 之前的做法不管市價單實際成交在哪裡，一律用「理論價格」算獲利/貼標籤，
        # 導致 PeakLock 明明鎖利 1.10%，交易所實際卻用市價成交在 -0.10%，
        # 系統內部紀錄卻還是顯示賺錢——這裡改成用真實成交均價回填後續所有計算。
        # Peak_Giveback（利潤正在回吐，要求「不再等待，立即鎖利」）這類時間敏感的
        # 出場理由，不能用限價追價——追價機制掛限價、等 4 秒沒成交才追一次價、再等
        # 4 秒才轉市價，前後要 8~11 秒。實際發生過 HBARUSDT 觸發回吐停利當下利潤還有
        # +0.31%，追價這 11 秒內價格繼續反著走，最後市價成交時已經變成 -0.16%，
        # 「不再等待」的出場反而等了最久、虧最多。這類理由直接用市價出場搶時效，
        # 不要為了多鎖一點點價差去冒繼續等待的風險。
        # [MA_Peak_Lock]/[MA7_Closed_Break]/[MA7_MA25_Death_Cross]/[MA7_MA25_Golden_Cross]
        # 補進來：這幾個都是「MA 波段偵測到反轉、峰值正在回吐」性質的訊號，跟
        # Peak_Giveback 是同一類時間敏感出場，之前漏掉沒加，實測 LINKUSDT 案例：
        # MA_Peak_Lock 觸發時打算鎖利 +1.2%，因為走限價追價流程等了幾秒，價格加速
        # 下殺，最後市價成交時已經變成 -0.42% 虧損——跟 HBARUSDT 一模一樣的病根。
        # [Sell_Pressure_Exit]/[Buy_Pressure_Exit] 補進來：這兩個是即時成交流判斷
        # 出反轉才觸發，時效性跟 Peak_Giveback 同一等級，同樣不能為了多鎖一點價差
        # 去走限價追價流程，否則反而讓「該早點出場」的機制變成出場最慢的那個。
        _urgent_exit_reasons = (
            "Peak_Giveback", "Stagnation_Stop", "Dynamic_Trailing", "Range_Trailing_Closed_Confirm", "TrailTP_Peak",
            "Peak_Volume_Contraction", "MA_Peak_Lock", "MA_Profit_Floor", "MA7_Closed_Break",
            "MA7_MA25_Death_Cross", "MA7_MA25_Golden_Cross",
            "MA7_Profit_Turn_Partial", "MA7_Profit_Turn_Confirmed",
            "Sell_Pressure_Exit", "Buy_Pressure_Exit",
        )
        _is_urgent_exit = any(r in reason for r in _urgent_exit_reasons)
        try:
            if profit_pct > 0 and not is_stop_loss and not _is_urgent_exit:
                final_price = await _exit_lock_profit_with_chase(sym, close_side, qty, price)
            else:
                final_price = await _market_close_and_get_fill(sym, close_side, qty, price)
            s["_close_fail_count"] = 0
        except Exception as e:
            logger.info(f"🚨 [平倉錯誤] {sym}: {e}")
            s["_close_fail_count"] = s.get("_close_fail_count", 0) + 1
            # ReduceOnly 被拒絕通常代表交易所那邊這筆倉位數量已經對不上（或已經不存在），
            # 繼續拿同一組舊資料重試只會無限失敗，卡住主迴圈不去管其他倉位——曾經發生
            # LINKUSDT 平倉後 14 秒內又被反手重新進場，出場單卡在 ReduceOnly 被拒絕，
            # 連續重試 17 分鐘，害同一時段的 BCHUSDT 整整 10 分鐘沒被檢查、虧損擴大。
            # 遇到這類「明確代表倉位對不上」的錯誤，或連續失敗達上限，直接跟交易所核對
            # 真實倉位並修正本地狀態，而不是無條件一直重試。
            _reduce_only_rejected = "-2022" in str(e) or "ReduceOnly" in str(e)
            if not PAPER_TRADING and (_reduce_only_rejected or s["_close_fail_count"] >= 3):
                try:
                    positions = await exchange_futures.fetch_positions([sym])
                    _, real_qty = _find_exchange_position(positions, sym)
                except Exception as sync_err:
                    logger.info(f"⚠️ [平倉失敗後對帳失敗] {sym}: {sync_err}")
                    return
                if abs(real_qty) < 0.000001:
                    # 市價/交易所保護單可能已成交，但本次送單回報 ReduceOnly。此時不能
                    # 直接 reset：1000PEPE 曾因此清掉數量、均價與進場原因，下一輪對帳
                    # 已無資料可把真實平倉補進 trade_history。保留快照交由 60 秒對帳
                    # 取得成交、寫入歷史後再統一清理。
                    logger.info(f"🔄 [平倉失敗後對帳] {sym} 交易所實際已無倉位，保留本地快照等待成交同步")
                    await _cancel_exchange_exit_order(sym, "exchange_stop_order_id", "止損")
                    await _cancel_exchange_exit_order(sym, "exchange_take_profit_order_id", "停利")
                    s["_external_close_record_pending"] = True
                else:
                    logger.info(f"🔄 [平倉失敗後對帳] {sym} 交易所實際倉位為 {real_qty}，同步本地數量後待下次重新評估")
                    s["qty"] = real_qty
                    s["_close_fail_count"] = 0
            return

    # 用真實成交價（實盤）或模擬價（紙上）重新計算最終獲利，取代呼叫端傳入的理論價格
    profit_pct = (final_price - real_avg) / real_avg if s["qty"] > 0 else (real_avg - final_price) / real_avg
    price = final_price

    atr_val = s.get("entry_atr", s.get("current_atr", price * 0.01))
    sl_mult = s.get("sl_atr_multiplier", 1.5)
    initial_risk_pct = (sl_mult * atr_val) / real_avg if real_avg > 0 else 0.01

    if profit_pct > 0 and initial_risk_pct > 0 and (profit_pct / initial_risk_pct) >= 2.0:
        pnl_tag = "[Big_Win]"
    elif profit_pct > 0.01:
        pnl_tag = "[大賺]"
    elif profit_pct > 0.002:
        pnl_tag = "[微利]"
    elif profit_pct > -0.002:
        pnl_tag = "[打平]"
    elif profit_pct > -0.01:
        pnl_tag = "[小虧]"
    else:
        pnl_tag = "[大虧]"

    if profit_pct < -0.001 or is_stop_loss:
        if close_side == "sell":
            s["last_loss_time_long"] = time.time()
        else:
            s["last_loss_time_short"] = time.time()
        s["consecutive_losses"] = s.get("consecutive_losses", 0) + 1
    else:
        s["consecutive_losses"] = 0

    full_reason = f"{pnl_tag} {reason}".strip()
    s["last_exit_time"] = time.time()
    s["last_exit_reason"] = full_reason

    # 交易紀錄的手續費原本無條件寫死 0.0，導致交易列表「已平倉」的單子手續費永遠
    # 顯示 0，即使交易所其實已經扣款（開放中的持倉另外有查真實手續費的邏輯，見
    # services/api.py 的 /api/trades/ALL，但那段涵蓋不到已經平倉、只存在
    # trade_history.json 裡的紀錄）。這裡在寫入紀錄前，用這筆倉位的開倉時間當
    # 起點，查一次這段期間交易所真實成交的手續費加總（含進場、攤平、出場所有筆），
    # 查不到就退回 0（不讓查詢失敗擋住正常記錄）。
    _real_fees = 0.0
    if not PAPER_TRADING:
        try:
            _since_ms = int(s.get("open_time", 0.0) * 1000) or None
            _fee_trades = await exchange_futures.fetch_my_trades(sym, since=_since_ms, limit=50)
            if _since_ms is None:
                # 恢復倉位沒有 open_time 時，不能把該幣最近 50 筆歷史手續費全算進來。
                # 由最新成交往回，只取本次平倉到前一次已實現損益之間的一個倉位週期。
                _cycle = []
                for _trade in reversed(_fee_trades):
                    _realized = float(_trade.get("realizedPnl", 0.0) or (_trade.get("info") or {}).get("realizedPnl", 0.0) or 0.0)
                    if _cycle and _realized != 0.0:
                        break
                    _cycle.append(_trade)
                _fee_trades = list(reversed(_cycle))
            _real_fees = sum(float((t.get("fee") or {}).get("cost", 0.0) or 0.0) for t in _fee_trades)
        except Exception as _fee_e:
            logger.info(f"⚠️ [手續費查詢失敗] {sym}: {_fee_e}")
            _real_fees = 0.0

    _position_value = real_avg * qty
    _net_profit_pct = profit_pct - ((_real_fees / _position_value) if _position_value > 0 else 0.0)
    if _net_profit_pct > 0.01:
        _net_pnl_tag = "[大賺]"
    elif _net_profit_pct > 0.002:
        _net_pnl_tag = "[微利]"
    elif _net_profit_pct > -0.002:
        _net_pnl_tag = "[打平]"
    elif _net_profit_pct > -0.01:
        _net_pnl_tag = "[小虧]"
    else:
        _net_pnl_tag = "[大虧]"
    full_reason = f"{_net_pnl_tag} {reason}".strip()
    s["last_exit_reason"] = full_reason
    if not is_stop_loss and profit_pct >= -0.001 and _net_profit_pct < -0.001:
        if close_side == "sell":
            s["last_loss_time_long"] = time.time()
        else:
            s["last_loss_time_short"] = time.time()
        s["consecutive_losses"] = s.get("consecutive_losses", 0) + 1

    record_trade_result(
        symbol=sym,
        entry_reason=s.get("entry_reason", "UNKNOWN"),
        exit_reason=full_reason,
        profit_pct=profit_pct,
        current_atr=s.get("current_atr", 0.0),
        max_profit_reached=s.get("highest_profit_pct", 0.0),
        expected_entry=real_avg,
        expected_exit=price,
        actual_entry=real_avg,
        actual_exit=price,
        fees=_real_fees,
        qty=qty,
        entry_timestamp_ms=int(s.get("open_time", 0.0) * 1000) if s.get("open_time", 0.0) else None,
        side="buy" if s["qty"] > 0 else "sell",
    )

    from core.config import DAILY_LOSS_LIMIT_PCT
    try:
        accrue_daily_realized_pnl(_net_profit_pct, real_avg * qty)
        if _net_profit_pct < 0:
            logger.info(f"[每日熔斷追蹤] {sym} 淨虧損 {_net_profit_pct*100:.2f}% | 今日累計: {_bal._DAILY_REALIZED_LOSS*100:.2f}% / {DAILY_LOSS_LIMIT_PCT*100:.1f}%")
    except Exception as _e:
        logger.info(f"[每日熔斷追蹤失敗] {_e}")

    remaining = abs(position_qty_before_close) - qty
    if remaining < 0.01:
        if remaining > 0.000001:
            logger.info(f"🧹 [塵埃清理] {sym} 剩餘 {remaining:.6f} 視為已清")
        await _cancel_exchange_exit_order(sym, "exchange_stop_order_id", "止損")
        await _cancel_exchange_exit_order(sym, "exchange_take_profit_order_id", "停利")

        mark_exit(sym, is_stop_loss=is_stop_loss, reason=full_reason, loss_pct=_net_profit_pct)
        clear_peak(sym)
        reset_coin_state(sym)
    else:
        prec = await get_contract_precision(sym)
        raw_qty = remaining * (1 if position_qty_before_close > 0 else -1)
        s["qty"] = round_step(raw_qty, prec["step_size"])

        qty_to_remove = qty
        if "entries" in s:
            while qty_to_remove > 0.000001 and len(s["entries"]) > 0:
                first_entry = s["entries"][0]
                if first_entry["qty"] <= qty_to_remove + 0.000001:
                    qty_to_remove -= first_entry["qty"]
                    s["entries"].pop(0)
                else:
                    first_entry["qty"] -= qty_to_remove
                    qty_to_remove = 0

        logger.info(f"✅ [部分平] {sym} 平{qty} 剩{abs(s['qty']):.4f} {full_reason}")

        try:
            await _replace_exchange_exit_orders(sym)
            logger.info(f"🛡️ [交易所退出單更新] {sym} 部分平倉後已同步更新止損/停利單 (數量: {abs(s['qty'])})")
        except Exception as ce:
            logger.info(f"⚠️ [更新交易所退出單失敗] {sym}: {ce}")


async def execute_panic_sell_all_positions():
    logger.info("🚨🚨 [緊急清倉] 開始強制平掉虧損倉位（有利潤者保留）！")
    for sym in ctx.ALL_SYMBOLS:
        s = ctx.STATES[sym]
        qty = abs(s.get("qty", 0.0))
        if qty < 0.000001:
            continue
        is_long = s["qty"] > 0
        cs = 'sell' if is_long else 'buy'
        p = s.get("close_price", s["avg_price"])
        avg = s.get("avg_price", 0.0)
        unrealized = (p - avg) * qty if is_long else (avg - p) * qty
        if unrealized >= 0:
            logger.info(f"✅ [緊急清倉] {sym} 未虧損 (未實現={unrealized:.4f} USDT)，保留持倉")
            continue
        logger.info(f"🚨 [緊急清倉] 平倉虧損倉位 {sym} (未實現={unrealized:.4f} USDT)...")
        try:
            await close_position(sym, cs, qty, p, s["avg_price"], reason="[GLOBAL_MELTDOWN]", is_stop_loss=True)
        except Exception as e:
            logger.info(f"⚠️ [緊急清倉失敗] {sym}: {e}")




def check_total_equity_protection():
    total_unrealized_pnl = 0.0
    has_positions = False

    for sym in ctx.ALL_SYMBOLS:
        s = ctx.STATES[sym]
        qty = s.get("qty", 0.0)
        if abs(qty) > 0.000001:
            has_positions = True
            p = s.get("close_price", 0.0)
            avg = s.get("avg_price", 0.0)
            if p <= 0.0:
                p = avg
            if qty > 0:
                pnl = (p - avg) * abs(qty)
            else:
                pnl = (avg - p) * abs(qty)
            total_unrealized_pnl += pnl

    if not has_positions:
        return True

    total_balance = get_total_wallet_balance()
    if total_balance <= 0:
        return True

    loss_percentage = (total_unrealized_pnl / total_balance) * 100
    GLOBAL_LOSS_THRESHOLD = -15.0

    if loss_percentage <= GLOBAL_LOSS_THRESHOLD:
        logger.info(f"\n🚨🚨🚨 [全局風控熔斷] 警告！當前總未實現虧損已達 {loss_percentage:.2f}%")
        logger.info(f"🛑 超過安全防線 {GLOBAL_LOSS_THRESHOLD}%！觸發系統緊急黑天鵝熔斷機制...")
        return False
    return True


def _fill_paper_order(sym, fill_price, side=None, qty=None, margin=0.0, is_rescue_dca=False):
    """處理 paper 模式的待成交限價單：成交後更新倉位狀態"""
    s = ctx.STATES[sym]
    pk = paper_key(sym)

    if side is not None and qty is not None:
        order = {
            "side": side,
            "qty": qty,
            "margin": margin,
            "placed_at": time.time(),
            "timeout": 0,
            "is_rescue_dca": is_rescue_dca,
        }
    else:
        order = s.get("pending_paper_order")
        if not order:
            return
        is_rescue_dca = order.get("is_rescue_dca", False)
            
    if not fill_price or fill_price <= 0:
        logger.info(f"[REJECT_PAPER] {sym} _fill_paper_order fill_price=0，已攔截撤單")
        s["pending_paper_order"] = None
        return
    side = order["side"]
    base_amt = order["qty"]
    margin = order["margin"]
    if is_rescue_dca:
        rescue_ok, rescue_reason = is_effective_rescue_dca(s, side, fill_price, add_qty=base_amt)
        if not rescue_ok:
            logger.info(f"🛑 [RescueDCAIneffective] {sym} 模擬攤平成交取消：{rescue_reason}")
            s["pending_paper_order"] = None
            return
    now = time.time()
    try:
        update_paper_state(pk, side, fill_price, base_amt)
        if side == 'buy':
            prev_qty = abs(s["qty"])
            s["qty"] += base_amt
        else:
            prev_qty = abs(s["qty"])
            s["qty"] -= base_amt
        if s["avg_price"] <= 0:
            s["avg_price"] = fill_price
            s["entry_atr"] = max(s.get("current_atr", 0.0), fill_price * 0.005)
        else:
            s["avg_price"] = ((s["avg_price"] * prev_qty) + (fill_price * base_amt)) / abs(s["qty"])
        if "entries" not in s:
            s["entries"] = []
        s["entries"].append({"price": fill_price, "qty": base_amt, "time": now, "side": side})
        s["open_time"] = now
        from core.entry_time_store import save_entry_time
        save_entry_time(sym, now)
        s["last_buy_time"] = now
        s["last_entry_time"] = now
        s["last_entry_price"] = fill_price
        s["restored_from_exchange"] = False
        s["last_entry_direction"] = side
        s["entry_count"] += 1
        if s["entry_count"] == 1:
            s["is_breakeven_locked"] = False
            s["highest_profit_pct"] = 0.0
            s["max_profit_reached"] = 0.0
            clear_peak(sym)
            s["first_entry_price"] = fill_price
            s["entry_strength"] = signal_strength if signal_strength is not None else 0.0
            s["_lin_trail_armed"] = False
            s["_lin_trail_activation_price"] = 0.0
            s["_lin_trail_activation_stop"] = 0.0
        _import_update_trailing_stop()(sym, fill_price, side == 'buy')
        # 金字塔加碼（同方向、更好價位）才鎖定在首筆進場價保本；
        # 救援攤平 (Rescue DCA) 是在更差價位補倉攤低成本，鎖在首筆價格等於讓新均價毫無喘息空間，
        # 因此救援攤平後改用新均價重新計算停損，不做這個保本鎖定。
        if s["entry_count"] >= 2 and not is_rescue_dca:
            first_ep = s["entries"][0]["price"]
            if side == 'buy':
                s["trailing_stop_price"] = max(s["trailing_stop_price"], first_ep)
            else:
                s["trailing_stop_price"] = min(s["trailing_stop_price"], first_ep) if s["trailing_stop_price"] > 0 else first_ep
            s["is_breakeven_locked"] = True
        direction = "做多" if side == 'buy' else "做空"
        logger.info(f"✅ [Paper成交] {sym} {direction} {base_amt:.4f} @ {fill_price:.6f} (保證金:{margin:.2f} USDT)")
    except Exception as e:
        logger.info(f"🛑 [Paper成交失敗] {sym}: {e}")
    finally:
        s["pending_paper_order"] = None


async def check_paper_pending_order(sym):
    """每個 tick 檢查 paper 掛單是否觸發或超時"""
    s = ctx.STATES[sym]
    order = s.get("pending_paper_order")
    if not order:
        return
    p = s["close_price"]
    side = order["side"]
    limit_price = order["limit_price"]
    elapsed = time.time() - order["placed_at"]
    if elapsed > order["timeout"] and not _is_dynamic_pending_entry(order):
        s["pending_paper_order"] = None
        logger.info(f"⌛ [Paper超時撤單] {sym} {side} @ {limit_price:.6f} 超過 {order['timeout']}秒未成交，已撤單")
        return
    setup_ok, setup_reason = _pending_entry_setup_valid(order)
    if not setup_ok:
        s["pending_paper_order"] = None
        logger.info(f"🛑 [Paper訊號失效撤單] {sym} {side}：{setup_reason}")
        return
    filled = (side == 'buy' and p <= limit_price) or (side == 'sell' and p >= limit_price)
    if filled:
        reference_price = order.get("signal_price") or order.get("reference_price") or limit_price
        adverse_ok, adverse_reason = _entry_pending_adverse_guard(
            sym, side, reference_price, p, is_rescue_dca=order.get("is_rescue_dca", False)
        )
        if not adverse_ok:
            s["pending_paper_order"] = None
            logger.info(f"🛑 [Paper逆向撤單] {sym} {side} 觸價前行情反向偏離：{adverse_reason}，取消掛單")
            return
        actual_fill_price = min(limit_price, p) if side == 'buy' else max(limit_price, p)
        _fill_paper_order(sym, actual_fill_price)
        return

    reprice, reprice_reason = _pending_entry_reprice_needed(sym, order, p)
    if reprice:
        new_limit = _translated_pending_limit_price(order, p)
        price_ok, price_reason = _entry_price_guard(
            sym, side, new_limit, p, mode=("ma7_pullback" if order.get("ma_cross_anti_chase", False) else "range_limit" if str(order.get("entry_route") or "").lower() in RANGE_ENTRY_ROUTES else "pullback"),
            is_rescue_dca=False,
        )
        if new_limit > 0 and price_ok:
            old_limit = limit_price
            order["limit_price"] = new_limit
            order["price"] = new_limit
            order["signal_price"] = p
            order["market_reference_price"] = p
            order["placed_at"] = time.time()
            order["last_reprice_at"] = time.time()
            order["reprice_count"] = int(order.get("reprice_count", 0)) + 1
            logger.info(
                f"🔄 [Paper動態重掛] {sym} {side} 市價已偏離（{reprice_reason}），"
                f"撤 {old_limit:.6f}、改掛 {new_limit:.6f}"
            )
        else:
            s["pending_paper_order"] = None
            logger.info(f"🛑 [Paper重掛取消] {sym} 最新價格不適合重掛：{price_reason}")


def _resolve_entry_order_mode(entry_mode, signal_strength=None, entry_route=None):
    # MA25 回調使用被動限價；已收線交叉與帶量突破使用 IOC 限價追蹤，
    # 在限制滑點的同時避免把有效突破掛到行情後方。
    if entry_route in ("MA_Cross", "MA_Breakout"):
        return "chase"
    if entry_route == "MA25_Pullback":
        return "pullback"
    # 區間模式：直接用支撐/壓力位精確掛限價，不走 pullback 的 ATR 偏移邏輯
    if entry_route in ("Range_Support_Long", "Range_Resistance_Short"):
        return "range_limit"
    return "pullback"


async def execute_order(sym, side, price, allocation_pct=0.33, is_rescue_dca=False,
                        signal_strength=None, entry_route=None, entry_mode_override=None):
    import numpy as np  # 強制防禦局部變量失效漏洞
    side = str(side).lower()
    if side not in ("buy", "sell"):
        logger.info(f"🛑 [InvalidEntrySide] {sym} 收到無效開倉方向 {side!r}，拒絕下單")
        return
    s = ctx.STATES[sym]
    if not PAPER_TRADING and not is_rescue_dca:
        openable, status_reason = await get_contract_openability(sym, exchange_futures)
        if not openable:
            s["order_fail_cooldown_until"] = time.time() + 300.0
            logger.info(
                f"🛑 [SymbolStatusGuard] {sym} 執行市場目前不可新增倉位"
                f"（{status_reason}），取消送單並於 5 分鐘後重新確認"
            )
            return
    route_key = str(entry_route or "").lower()
    if not is_rescue_dca and route_key in MA_ENTRY_ROUTES:
        from core.entry_filter import btc_macro_entry_guard, is_ma_direction_aligned
        macro_ok, macro_reason, _ = btc_macro_entry_guard(sym, side)
        if not macro_ok:
            logger.info(f"🛑 [Final_BTC_Macro_Guard] {sym} {side}：{macro_reason}")
            return
        if not is_ma_direction_aligned(s, side, entry_route):
            logger.info(f"🛑 [Final_MA_Direction_Guard] {sym} {side} 未通過 MA7/MA25/MA99 完整排列與斜率，拒絕送單")
            return
    elif not is_rescue_dca and route_key in RANGE_ENTRY_ROUTES:
        from core.entry_filter import is_range_direction_valid
        range_ok, range_reason = is_range_direction_valid(sym, side, entry_route)
        if not range_ok:
            logger.info(f"🛑 [Final_Range_Guard] {sym} {side}：{range_reason}")
            return
    if s.get("_is_closing", False):
        logger.info(f"🛑 [CloseInProgress] {sym} 正在平倉，拒絕新的 {side} 進場單")
        return
    if not is_rescue_dca and any(info.get("sym") == sym for info in ctx.PENDING_LIMIT_ORDERS.values()):
        logger.info(f"⏳ [PendingEntryGuard] {sym} 已有待成交進場單，拒絕重複送出 {side} 單")
        return
    entry_mode = entry_mode_override if entry_mode_override is not None else ENTRY_ORDER_MODE
    _force_pullback = bool(s.pop("force_pullback_entry", False)) and not is_rescue_dca
    actual_entry_mode = "pullback" if _force_pullback else _resolve_entry_order_mode(entry_mode, signal_strength, entry_route)
    if _force_pullback:
        logger.info(f"🧲 [EntryModeOverride] {sym} 套用高波動上/下緣防追價，強制使用 pullback 限價")
    
    is_first_entry = (s.get("entry_count", 0) == 0)

    # 「分批入場」（先 60% 試探倉、訊號確認後再補到 100%）已經失效：金字塔加碼規則
    # 在後面的 `if s["entry_count"] > 0 and not is_rescue_dca: return` 對任何加碼
    # 一律無條件擋下（見本函式後段），導致「加倉確認」那一步每次都送出去、每次都
    # 被自己擋掉，從來沒有真的補到 100% 過——所有倉位實際上永遠卡在 60% 大小。
    # 既然補倉這條路已經走不通，直接第一筆（也是唯一一筆）就用完整計算金額進場，
    # 不再假裝之後會有第二筆補上。
    logger.info(f"🛒 [ORDER_ATTEMPT] {sym} 開始執行 {side} 進場 | price={price:.6f} allocation={allocation_pct:.2f}")

    # 進場方向與當前持倉衝突防護
    # 如果已有持倉，且新進場方向與舊持倉方向相反，除非是救援 DCA，否則直接攔截
    _existing_qty = s.get("qty", 0.0)
    _has_position = abs(_existing_qty) > 0.000001
    if _has_position:
        _current_direction = "buy" if _existing_qty > 0 else "sell"
        if side != _current_direction and not is_rescue_dca:
            logger.info(f"🛑 [Direction_Conflict] {sym} 已有 {_current_direction} 持倉 (qty={_existing_qty:.4f})，禁止發出 {side} 進場指令 (非救援模式)")
            logger.info(f"🧱 [ORDER_BLOCK] {sym} 被方向衝突攔截，未進入下單）")
            logger.info(f"🛑 [Direction_Conflict] {sym} 若要反手，請先透過 close_position 平倉後再進場，避免方向衝突！")
            return

    if not price or price <= 0:
        fallback = s.get("close_price", 0.0) or s.get("avg_price", 0.0)
        if fallback <= 0:
            logger.info(f"[REJECT_ZERO_PRICE] {sym} execute_order price=0 且無法補救，已攔截！")
            return
        logger.info(f"[WARN_ZERO_PRICE] {sym} execute_order price=0，補救為 {fallback:.6f}")
        price = fallback
    pk = paper_key(sym)
    lev = get_symbol_leverage(sym)
    s["leverage"] = lev
    logger.info(f"@@LEVERAGE@@{lev}")

    # 改用 exchange_market_data（永遠讀真實市場，不受 Demo Trading 影響）查掛單簿：
    # Demo Trading 自己的掛單簿是獨立模擬撮合的資料，跟真實市場的買賣盤量級跟比例都對不上
    # （實測 BTC/USDT 真實 bid/ask 比 0.33，Demo 卻是 1.91；量級更是差了 55~2000 倍），
    # 拿真實市場的掛單簿判斷才有意義，兩邊環境都適用，不用再看 USE_TESTNET 決定要不要跳過。
    if not is_rescue_dca:
        try:
            orderbook = await exchange_market_data.fetch_order_book(sym, limit=20)
            bids = sum(x[1] for x in orderbook.get('bids', []))
            asks = sum(x[1] for x in orderbook.get('asks', []))
            _s = ctx.STATES.get(sym, {})
            _atr_hist_of = _s.get("atr_history", [])
            _atr_avg_of = float(np.mean(_atr_hist_of)) if len(_atr_hist_of) > 0 else 0.0
            _atr_cur_of = _s.get("current_atr", 0.0)
            _is_low_vol_of = (_atr_avg_of > 0 and _atr_cur_of <= _atr_avg_of)
            # 強訊號（強度 >= 20）直接豁免 OrderFlow 過濾，避免封鎖高品質進場訊號
            _signal_str_of = signal_strength or 0.0
            _flow_bypass = _signal_str_of >= 20.0
            _flow_threshold = 0.45 if _is_low_vol_of else 0.50
            _flow_label = f"低波動放寬 {_flow_threshold}" if _is_low_vol_of else f"高波動嚴格 {_flow_threshold}"
            if not _flow_bypass:
                if side == 'buy':
                    if asks == 0 or bids / asks < _flow_threshold:
                        if should_block_order_flow(side, bids, asks, _flow_threshold, PAPER_TRADING or USE_TESTNET):
                            logger.info(f"🛑 [Filter:OrderFlow] {sym} 買盤支撐不足 (BidVol: {bids:.2f} / AskVol: {asks:.2f} < {_flow_threshold} | {_flow_label})，疑似假突破，拒絕做多！")
                            logger.info(f"🧱 [ORDER_BLOCK] {sym} 被 OrderFlow 攔截，未進入下單")
                            return
                else:
                    if bids == 0 or asks / bids < _flow_threshold:
                        if should_block_order_flow(side, bids, asks, _flow_threshold, PAPER_TRADING or USE_TESTNET):
                            logger.info(f"🛑 [Filter:OrderFlow] {sym} 賣盤壓力不足 (AskVol: {asks:.2f} / BidVol: {bids:.2f} < {_flow_threshold} | {_flow_label})，疑似假跌破，拒絕做空！")
                            logger.info(f"🧱 [ORDER_BLOCK] {sym} 被 OrderFlow 攔截，未進入下單")
                            return
            else:
                logger.info(f"⚡ [OrderFlow_Bypass] {sym} 強訊號 ({_signal_str_of:.1f} >= 20)，豁免 OrderFlow 過濾直接進場")
        except Exception as e:
            logger.info(f"⚠️ [OrderFlow] 讀取掛單簿失敗 {sym}: {e}")
    if not PAPER_TRADING:
        try:
            await exchange_futures.set_leverage(lev, convert_to_ccxt_symbol(sym))
        except Exception as e:
            logger.info(f"⚠️ [槓桿設定失敗] {sym}: {e}")

    margin = compute_per_coin_margin(sym, allocation_pct)

    if margin <= 0:
        logger.info(f"⚠️ [風控] {sym} 無可用保證金")
        logger.info(f"🧱 [ORDER_BLOCK] {sym} 被保證金不足攔截，未進入下單")
        return

    try:
        market_price = await get_reference_price(sym, exchange_futures)
    except Exception as e:
        market_price = 0.0
        logger.info(f"⚠️ [價格偏離檢查] {sym} 取得參考價失敗: {e}")

    if market_price <= 0:
        # fetch_ticker 失敗時回落到即時交易流價格（獨立數據源，不依賴 OHLCV）
        market_price = float(s.get("last_trade_price", 0.0) or 0)

    if market_price > 0:
        # --- Slippage Filter (Layer 1) ---
        # Compare signal_price (price) with current market_price
        # We use 0.5% as default, but check if it's a high volatility coin
        # High volatility is defined by ATR > 0.8% (as seen in other guards)
        atr = float(s.get("current_atr", 0.0) or 0.0)
        atr_pct = atr / market_price if market_price > 0 else 0.0
        slippage_threshold = 0.010 if atr_pct > 0.008 else 0.005
        
        slippage = abs(price - market_price) / market_price
        if slippage > slippage_threshold:
            logger.info(f"🛑 [Slippage Filter] {sym} {side} 訊號價 {price:.6f} 與市場價 {market_price:.6f} 偏離 {slippage*100:.2f}% > 門檻 {slippage_threshold*100:.1f}%")
            logger.info(f"🧱 [ORDER_BLOCK] {sym} 被滑點過大攔截，拒絕下單以避免追高/追低")
            return

        deviation = abs(price - market_price) / market_price
        if deviation > 0.05:
            logger.info(f"🚨 [風控] {sym} 訂單價格 {price:.6f} 偏離市場參照價 {market_price:.6f} ({deviation*100:.2f}%)，已攔截異常訂單！")
            logger.info(f"🧱 [ORDER_BLOCK] {sym} 被價格偏離風控攔截，未進入下單")
            # 順帶修正被污染的 close_price，避免後續繼續使用錯誤值
            s["close_price"] = market_price
            return
    else:
        # 完全無法取得市場價格，保守拒絕
        logger.info(f"🚨 [風控] {sym} 無法取得市場參照價 (ticker失敗且無即時交易紀錄)，為安全起見拒絕執行 (price={price:.6f})")
        logger.info(f"🧱 [ORDER_BLOCK] {sym} 被市場價缺失風控攔截，未進入下單")
        return

    # MA_Cross 若已離開 MA7 太遠，不再用高分 chase 追在延伸段；改掛回 MA7 反抽位。
    # 這項保護只作用在首倉，救援 DCA 維持原本的價格改善規則。
    _ma_cross_anti_chase = False
    _ma_cross_anchor = 0.0
    if not is_rescue_dca:
        _ma_cross_anti_chase, _ma_cross_anchor, _ma_cross_reason = _ma_cross_anti_chase_plan(
            sym, side, market_price, entry_route,
        )
        if _ma_cross_anti_chase:
            actual_entry_mode = "pullback"
            logger.info(
                f"🧲 [MA_Cross_AntiChase] {sym} {side} {_ma_cross_reason}；"
                f"不追價，等待 MA7 反抽位 {_ma_cross_anchor:.6f}"
            )


    s["last_entry_signal_price"] = price

    # 連續被 EntryDirectionGuard 擋下太多次後直接放棄這波訊號、進入短暫冷卻。
    # 原本每次被擋只是這一輪不成交，下一輪訊號重新評估又會再試一次，實測 AVAXUSDT
    # 連續擋了 5 次（07:33~07:38）才在第 6 次通過，但那時候價格已經跑掉，追價進場
    # 反而滑價 0.44%（比正常 <0.1% 大了近 10 倍），變成用比較差的價格硬擠進場。
    # 與其一直試到終於通過、卻是在價格已經跑掉之後才通過，不如連續失敗夠多次就
    # 承認這波訊號已經追不上，暫停一段時間等下一個獨立訊號，不要為了「終於通過」
    # 而追在相對高點/低點。
    _dg_cooldown_until = s.get("_direction_guard_cooldown_until", 0)
    _current_ma_signal_ts = int(s.get("ma_signal_candle_ts", 0) or 0)
    _cooldown_signal_ts = int(s.get("_direction_guard_cooldown_signal_candle_ts", 0) or 0)
    if _dg_cooldown_until and _current_ma_signal_ts > _cooldown_signal_ts:
        s["_direction_guard_cooldown_until"] = 0
        _dg_cooldown_until = 0
        logger.info(f"✅ [EntryDirectionGuard_新訊號] {sym} 新的收線 MA 訊號已形成，解除舊波段冷卻並重新評估")
    if time.time() < _dg_cooldown_until:
        logger.info(f"⏳ [EntryDirectionGuard_冷卻] {sym} 先前連續方向守門失敗已放棄本波訊號，剩餘 {_dg_cooldown_until - time.time():.0f} 秒冷卻中，暫不進場")
        return

    direction_ok, direction_reason = _entry_direction_guard(sym, side, reference_price=price)
    if not direction_ok:
        _dg_reject_count = s.get("_direction_guard_reject_count", 0) + 1
        s["_direction_guard_reject_count"] = _dg_reject_count
        logger.info(f"🛑 [EntryDirectionGuard] {sym} {side} 訊號到執行期間方向變差：{direction_reason}，取消開倉 (連續第 {_dg_reject_count} 次)")
        logger.info(f"🧱 [ORDER_BLOCK] {sym} 被方向守門攔截，未進入下單")
        _DG_MAX_REJECTS = 3
        if _dg_reject_count >= _DG_MAX_REJECTS:
            s["_direction_guard_cooldown_until"] = time.time() + 300
            s["_direction_guard_cooldown_signal_candle_ts"] = int(s.get("ma_signal_candle_ts", 0) or 0)
            s["_direction_guard_reject_count"] = 0
            logger.info(f"🚫 [EntryDirectionGuard_放棄] {sym} 連續 {_DG_MAX_REJECTS} 次方向守門失敗，放棄這波訊號，暫停 5 分鐘避免追價進場")
        return
    s["_direction_guard_reject_count"] = 0

    pending_ok, pending_reason = _entry_pending_adverse_guard(sym, side, price, market_price, is_rescue_dca=is_rescue_dca)
    if not pending_ok:
        logger.info(f"🛑 [EntryAdverseGuard] {sym} {side} 當前價已逆向偏離訊號價：{pending_reason}，取消開倉")
        logger.info(f"🧱 [ORDER_BLOCK] {sym} 被逆向偏離攔截，未進入下單")
        return

    price_ok, price_reason = _entry_price_guard(
        sym, side, price, market_price,
        mode=actual_entry_mode, is_rescue_dca=is_rescue_dca,
    )
    if not price_ok:
        logger.info(f"🛑 [EntryPriceGuard] {sym} {side} 訊號價與即時牌價偏離：{price_reason}，取消開倉")
        logger.info(f"🧱 [ORDER_BLOCK] {sym} 被開倉價格偏離攔截，未進入下單")
        return

    if is_rescue_dca:
        rescue_ok, rescue_reason = is_effective_rescue_dca(s, side, price)
        if not rescue_ok:
            logger.info(f"🛑 [RescueDCAIneffective] {sym} 取消攤平：{rescue_reason}")
            return

    if entry_route:
        from core.check_entries import is_entry_candidate_still_valid
        still_valid, invalid_reason = is_entry_candidate_still_valid(
            sym, side, entry_route, signal_strength or 0.0, price,
        )
        if not still_valid:
            logger.info(f"🛑 [FinalDirectionGuard] {sym} {side} 下單前方向重驗失敗：{invalid_reason}")
            return

    now = time.time()
    if s["entry_count"] > 0 and not is_rescue_dca:
        logger.info(f"🛑 [加倉停用] {sym} 金字塔順勢加碼功能已完全停用，拒絕加倉！")
        return

    base_notional = margin * DUAL_SHOT_LEVERAGE

    if base_notional < 10.0 and margin * DUAL_SHOT_LEVERAGE >= 10.0:
        base_notional = 10.0

    balance = get_balance()
    # Position sizing hard cap: a full hard-stop loss may consume at most 2% of strategy capital.
    _entry_sl_pct = max(float(s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT) or HARD_STOP_LOSS_PCT), 0.01)
    _risk_notional_cap = balance * 0.02 / _entry_sl_pct
    if base_notional > _risk_notional_cap:
        logger.info(f"🛡️ [Risk_2Pct_Cap] {sym} 名義倉位 {base_notional:.2f} 縮減至 {_risk_notional_cap:.2f} USDT，確保硬停損風險不超過本金 2%")
        base_notional = _risk_notional_cap
    required_margin = base_notional / DUAL_SHOT_LEVERAGE

    if not PAPER_TRADING:
        try:
            bal = await exchange_futures.fetch_balance()
            total_usdt = float(bal.get("USDT", {}).get("total", balance))
            free_usdt = float(bal.get("USDT", {}).get("free", 0.0))
            # 注意：required_margin 不是從這裡的 total_usdt 算出來的，它來自呼叫端更早
            # 就用內部風控餘額（本金上限＋累計已實現損益）算好的 margin，跟這裡即時查詢
            # 的交易所真實總權益 total_usdt 是兩個獨立數字。之前這行 log 把兩者印在一起、
            # 又寫成「(= total/2)」，看起來像同一組計算，容易誤導成「total 被除錯了」，
            # 這裡拆開講清楚，避免下次又被誤判成計算 bug。
            logger.info(
                f"🔥 [重裝雙發進場] {sym} 倉位計算中...\n"
                f"   ➔ 交易所真實總權益 (僅供參考，不用於本次倉位計算): {total_usdt:.4f} USDT\n"
                f"   ➔ 單筆核配保證金 (依內部風控餘額計算): {required_margin:.4f} USDT\n"
                f"   ➔ {DUAL_SHOT_LEVERAGE}倍槓桿發射價值: {base_notional:.2f} USDT (名義合約大小)\n"
                f"   ➔ 當前可用餘額 (free): {free_usdt:.4f} USDT"
            )
            if required_margin > free_usdt and free_usdt > 0:
                logger.info(f"⚠️ [資金關卡] {sym} 可用餘額 {free_usdt:.2f} < 所需保證金 {required_margin:.2f}，調整為可用餘額下單！")
                base_notional = free_usdt * DUAL_SHOT_LEVERAGE
        except Exception as e:
            logger.info(f"⚠️ [餘額檢查失敗] {e}")
    else:
        logger.info(
            f"🔥 [重裝雙發進場-Paper] {sym}\n"
            f"   ➔ 模擬錢包總權益: {balance:.4f} USDT\n"
            f"   ➔ 單筆核配保證金: {required_margin:.4f} USDT (= total/2)\n"
            f"   ➔ {DUAL_SHOT_LEVERAGE}倍槓桿發射價值: {base_notional:.2f} USDT"
        )
        if required_margin > balance * 0.98:
            base_notional = (balance * 0.98) * DUAL_SHOT_LEVERAGE

    # ── MAX_NOTIONAL 硬上限（防止大餘額×高槓桿買入大量低價幣）──
    # 單筆名義倉位上限：避免帳戶余額大時，買入 DOGE/XRP 等低單價幣產生天文數字的合約量
    MAX_NOTIONAL_PER_TRADE = 500.0  # USDT 名義上限
    if base_notional > MAX_NOTIONAL_PER_TRADE:
        logger.info(f"⚠️ [MAX_NOTIONAL] {sym} 名義倉位 {base_notional:.2f} USDT > 上限 {MAX_NOTIONAL_PER_TRADE} USDT，已自動縮減")
        base_notional = MAX_NOTIONAL_PER_TRADE

    base_amt = base_notional / price
    base_amt = await sanitize_order_qty(sym, base_amt)

    # 幣安期貨的 MARKET_LOT_SIZE 過濾器對市價單另外設有比一般 LOT_SIZE 更低的單筆數量
    # 上限（實測 KAITOUSDT：LOT_SIZE 上限 100 萬，MARKET_LOT_SIZE 卻只有 500）。進場走
    # 限價/追價單不受這個限制，倉位可能建到遠超過這個上限，但之後市價平倉、或掛市價型
    # 止損/停利單都會直接被拒絕（-4005 Quantity greater than max quantity），導致部位
    # 卡住平不掉、完全沒有交易所端保護（真實發生過：KAITOUSDT 744 顆）。在源頭把進場
    # 數量也一併夾在這個上限之內，確保建立的倉位永遠平得掉、掛得上止損/停利單。
    _market_max_qty = (await get_contract_precision(sym)).get('market_max_qty')
    if _market_max_qty and _market_max_qty > 0 and base_amt > _market_max_qty:
        logger.info(f"⚠️ [MARKET_MAX_QTY] {sym} 計算數量 {base_amt:.4f} > 市價單上限 {_market_max_qty}，已自動縮減，避免日後平倉/止損掛單被拒")
        base_amt = await sanitize_order_qty(sym, _market_max_qty)

    actual_notional = base_amt * price
    if actual_notional < 6.0 and actual_notional > 0:
        min_qty = 6.0 / price
        min_qty = await sanitize_order_qty(sym, min_qty)
        if (min_qty * price) / lev > balance * 0.98:
            logger.info(f"⚠️ [風控] {sym} 資金不足以達到最小開倉額度 6 USDT (餘額: {balance:.2f})")
            return
        base_amt = min_qty
        actual_notional = base_amt * price

    if base_amt <= 0.0:
        logger.info(f"⚠️ [風控] {sym} 計算後開倉數量為 0")
        logger.info(f"🧱 [ORDER_BLOCK] {sym} 被數量計算為 0 攔截，未進入下單")
        return

    if is_rescue_dca:
        rescue_ok, rescue_reason = is_effective_rescue_dca(s, side, price, add_qty=base_amt)
        if not rescue_ok:
            logger.info(f"🛑 [RescueDCAIneffective] {sym} 取消攤平：{rescue_reason}")
            return

    if PAPER_TRADING:
        try:
            # market_price 已在上方用 mark price / 委託簿中位數取得，比 OHLCV 收盤價更貼近牌價
            current_market_price = market_price if market_price > 0 else s.get("close_price", price)
            logger.info(f"🧭 [EntryMode] {sym} 選擇 paper 進場模式: {actual_entry_mode}")
            if actual_entry_mode == 'market':
                chase_ok, chase_reason = _entry_signal_chase_guard(
                    side, price, current_market_price, is_first_entry, is_rescue_dca,
                )
                if not chase_ok:
                    logger.info(f"🛑 [SignalChaseGuard] {sym} 首倉市價取消：{chase_reason}")
                    return
                _fill_paper_order(sym, current_market_price, side=side, qty=base_amt, margin=margin, is_rescue_dca=is_rescue_dca)
                logger.info(f"✅ [Paper市價成交] {sym} {side} {base_amt:.4f} @ {current_market_price:.6f}")
                return
            elif actual_entry_mode == 'chase':
                fill_price = current_market_price * (1 + ENTRY_CHASE_OFFSET_PCT) if side == 'buy' else current_market_price * (1 - ENTRY_CHASE_OFFSET_PCT)
                chase_ok, chase_reason = _entry_signal_chase_guard(
                    side, price, fill_price, is_first_entry, is_rescue_dca,
                )
                if not chase_ok:
                    logger.info(f"🛑 [SignalChaseGuard] {sym} 首倉追價取消：{chase_reason}")
                    return
                _fill_paper_order(sym, fill_price, side=side, qty=base_amt, margin=margin, is_rescue_dca=is_rescue_dca)
                logger.info(f"✅ [Paper追價成交] {sym} {side} {base_amt:.4f} @ {fill_price:.6f}")
                return
            elif actual_entry_mode == 'range_limit':
                # 區間模式精確限價：直接掛在支撐/壓力位，不使用 pullback 的 ATR 偏移
                _range_support    = float(s.get("range_support_level",    0.0) or 0.0)
                _range_resistance = float(s.get("range_resistance_level", 0.0) or 0.0)
                if side == 'buy' and _range_support > 0:
                    # 支撐帶內側留緩衝：避免貼著支撐正上方進場、正常雜訊就碰到結構停損
                    limit_price = _range_support * (1 + RANGE_ENTRY_EDGE_BUFFER_PCT)
                    logger.info(f"🎯 [Range支撐掛單-Paper] {sym} 掛买在支撐位 {_range_support:.6f} +{RANGE_ENTRY_EDGE_BUFFER_PCT*100:.2f}% = {limit_price:.6f}")
                elif side == 'sell' and _range_resistance > 0:
                    # 壓力帶內側留緩衝：避免貼著壓力正下方進場、正常雜訊就碰到結構停損
                    limit_price = _range_resistance * (1 - RANGE_ENTRY_EDGE_BUFFER_PCT)
                    logger.info(f"🎯 [Range壓力掛單-Paper] {sym} 掛賣在壓力位 {_range_resistance:.6f} -{RANGE_ENTRY_EDGE_BUFFER_PCT*100:.2f}% = {limit_price:.6f}")
                else:
                    # 區間位資料遺失，降級為當前小幁適度高於市價 (buy) / 低於市價 (sell)的限價
                    _fallback_offset = 0.9998 if side == 'buy' else 1.0002
                    limit_price = current_market_price * _fallback_offset
                    logger.info(f"⚠️ [Range掛單降級-Paper] {sym} 區間位資料遺失，降級小幁限價 {limit_price:.6f}")
                chase_ok, chase_reason = _entry_signal_chase_guard(
                    side, price, limit_price, is_first_entry, is_rescue_dca,
                )
                if not chase_ok:
                    logger.info(f"🛑 [SignalChaseGuard] {sym} 區間模式掛單取消：{chase_reason}")
                    return
                s["pending_paper_order"] = {
                    "side": side, "limit_price": limit_price, "qty": base_amt,
                    "margin": margin, "placed_at": now, "timeout": DUAL_SHOT_ORDER_TIMEOUT,
                    "is_rescue_dca": is_rescue_dca, "signal_price": price,
                    "sym": sym, "entry_route": entry_route, "signal_strength": signal_strength,
                    "signal_candle_ts": s.get("ma_signal_candle_ts", 0),
                    "price": limit_price, "market_reference_price": current_market_price,
                    "last_reprice_at": now, "reprice_count": 0,
                }
                direction = "做多" if side == 'buy' else "做空"
                logger.info(f"⏳ [Paper區間限價掛單] {sym} {direction} {base_amt:.4f} @ {limit_price:.6f} (當前: {current_market_price:.6f})")
                return
            elif actual_entry_mode == 'pullback':
                atr = s.get("current_atr", 0.0)
                if atr <= 0:
                    atr = current_market_price * 0.015
                # 高波動幣（ATR > 0.8%）用 1.5 倍回踩深度，確保買在更低點
                _atr_pct = atr / current_market_price if current_market_price > 0 else 0.015
                _pb_mult = ENTRY_PULLBACK_ATR_MULT * (1.5 if _atr_pct > 0.008 else 1.0)

                if side == 'buy':
                    target_pb = current_market_price - atr * _pb_mult
                    if len(s.get("ohlcv", [])) >= 2:
                        recent_low = min(s["ohlcv"][-1][3], s["ohlcv"][-2][3])
                        # 買在更划算價位：取回踩價與近期K線低點中較低者，且不超過 3x ATR 最大回踩深度
                        limit_price = min(target_pb, recent_low)
                        limit_price = max(limit_price, current_market_price - atr * (_pb_mult * 3))
                    else:
                        limit_price = target_pb
                else:
                    target_pb = current_market_price + atr * _pb_mult
                    if len(s.get("ohlcv", [])) >= 2:
                        recent_high = max(s["ohlcv"][-1][2], s["ohlcv"][-2][2])
                        # 賣在更划算價位：取回踩價與近期K線高點中較高者，且不超過 3x ATR 最大回踩深度
                        limit_price = max(target_pb, recent_high)
                        limit_price = min(limit_price, current_market_price + atr * (_pb_mult * 3))
                    else:
                        limit_price = target_pb
                if _ma_cross_anti_chase and _ma_cross_anchor > 0:
                    limit_price = _ma_cross_anchor
                    logger.info(f"🧲 [MA7反抽掛單-Paper] {sym} 等待 MA7 結構價 {limit_price:.6f}，不追延伸段")
                chase_ok, chase_reason = _entry_signal_chase_guard(
                    side, price, limit_price, is_first_entry, is_rescue_dca,
                )
                if not chase_ok:
                    logger.info(f"🛑 [SignalChaseGuard] {sym} 首倉回踩委託取消：{chase_reason}")
                    return
                s["pending_paper_order"] = {
                    "side": side, "limit_price": limit_price, "qty": base_amt,
                    "margin": margin, "placed_at": now, "timeout": DUAL_SHOT_ORDER_TIMEOUT,
                    "is_rescue_dca": is_rescue_dca, "signal_price": price,
                    "sym": sym, "entry_route": entry_route, "signal_strength": signal_strength,
                    "signal_candle_ts": s.get("ma_signal_candle_ts", 0),
                    "price": limit_price, "market_reference_price": current_market_price,
                    "last_reprice_at": now, "reprice_count": 0,
                    "ma_cross_anti_chase": _ma_cross_anti_chase,
                }
                direction = "做多" if side == 'buy' else "做空"
                logger.info(f"⏳ [Paper回踩掛單] {sym} {direction} {base_amt:.4f} @ {limit_price:.6f} (當前: {current_market_price:.6f}, ATR%:{_atr_pct*100:.2f}%, 深度:{_pb_mult:.2f}×ATR)")
                return
            else:
                spread_pct = 0.0003
                limit_price = current_market_price * (1 - spread_pct) if side == 'buy' else current_market_price * (1 + spread_pct)
                chase_ok, chase_reason = _entry_signal_chase_guard(
                    side, price, limit_price, is_first_entry, is_rescue_dca,
                )
                if not chase_ok:
                    logger.info(f"🛑 [SignalChaseGuard] {sym} 首倉被動委託取消：{chase_reason}")
                    return
                s["pending_paper_order"] = {
                    "side": side, "limit_price": limit_price, "qty": base_amt,
                    "margin": margin, "placed_at": now, "timeout": DUAL_SHOT_ORDER_TIMEOUT,
                    "is_rescue_dca": is_rescue_dca, "signal_price": price,
                    "sym": sym, "entry_route": entry_route, "signal_strength": signal_strength,
                    "signal_candle_ts": s.get("ma_signal_candle_ts", 0),
                    "price": limit_price, "market_reference_price": current_market_price,
                    "last_reprice_at": now, "reprice_count": 0,
                }
                direction = "做多" if side == 'buy' else "做空"
                logger.info(f"⏳ [Paper被動掛單] {sym} {direction} {base_amt:.4f} @ {limit_price:.6f} (等待成交)")
                return
        except Exception as e:
            logger.info(f"🛑 [模擬掛單失敗] {sym}: {e}")
            return
    else:
        try:
            order_type = 'limit'
            limit_price = price
            _is_structure_anchored = False
            try:
                ob = await exchange_futures.fetch_order_book(sym, limit=5)
                asks = ob.get('asks', [])
                bids = ob.get('bids', [])
                ask1 = float(asks[0][0]) if asks else price
                bid1 = float(bids[0][0]) if bids else price

                prec = await get_contract_precision(sym)
                tick_size = prec['tick_size']

                if actual_entry_mode == 'market':
                    order_type = 'market'
                    limit_price = None
                    logger.info(f"📌 [市價下單] {sym} 執行市價進場")
                elif actual_entry_mode == 'range_limit':
                    # 區間模式精確限價：支撐/壓力位上下各留 RANGE_ENTRY_EDGE_BUFFER_PCT
                    # 緩衝，避免貼著邊界進場、實際到結構停損的距離只剩半個 ATR 多一點。
                    _range_support    = float(s.get("range_support_level",    0.0) or 0.0)
                    _range_resistance = float(s.get("range_resistance_level", 0.0) or 0.0)
                    if side == 'buy' and _range_support > 0:
                        limit_price = round_step(_range_support * (1 + RANGE_ENTRY_EDGE_BUFFER_PCT), tick_size)
                        logger.info(f"🎯 [Range支撐掛單] {sym} 掛買在支撐位 {_range_support:.6f} +{RANGE_ENTRY_EDGE_BUFFER_PCT*100:.2f}% = {limit_price:.6f}")
                    elif side == 'sell' and _range_resistance > 0:
                        limit_price = round_step(_range_resistance * (1 - RANGE_ENTRY_EDGE_BUFFER_PCT), tick_size)
                        logger.info(f"🎯 [Range壓力掛單] {sym} 掛賣在壓力位 {_range_resistance:.6f} -{RANGE_ENTRY_EDGE_BUFFER_PCT*100:.2f}% = {limit_price:.6f}")
                    else:
                        # 區間位資料遺失，降級為實時市價小幁限價
                        _fallback_offset = 0.9998 if side == 'buy' else 1.0002
                        limit_price = round_step(market_price * _fallback_offset, tick_size)
                        logger.info(f"⚠️ [Range掛單降級] {sym} 區間位資料遺失，降級小幁限價 {limit_price:.6f}")
                elif actual_entry_mode == 'chase':
                    limit_price = ask1 if side == 'buy' else bid1
                    if side == 'buy':
                        limit_price = limit_price * (1 + ENTRY_CHASE_OFFSET_PCT)
                    else:
                        limit_price = limit_price * (1 - ENTRY_CHASE_OFFSET_PCT)
                    # 攤平救援封頂：追價模式本來是為了確保成交而主動加價，一般進場
                    # 這樣做沒問題，但攤平的目的是要比現有均價更低（多單）/更高（空單）
                    # 才有意義。實際案例（WLDUSDT）：訊號價明明比均價低，追價模式卻用
                    # 下單當下已經回彈的最新賣一價再加碼，兩次攤平最後都成交在跟原始
                    # 成本幾乎一樣的價位，均價完全沒被拉低，只是把倉位放大到 3 倍、
                    # 曝險跟著放大 3 倍卻沒有換到任何攤平效果。這裡限制攤平單的價格
                    # 不能比目前均價差，寧可這次掛不到、等下一輪再評估。
                    if is_rescue_dca and s.get("avg_price", 0) > 0:
                        if side == 'buy':
                            limit_price = min(limit_price, s["avg_price"])
                        else:
                            limit_price = max(limit_price, s["avg_price"])
                    limit_price = round_step(limit_price, tick_size)
                    logger.info(f"📌 [追價掛單] {sym} 掛對手價 {limit_price:.6f} 確保成交")
                elif actual_entry_mode == 'pullback':
                    # 如果是由 entry_filter 觸發的 SUPPORT_ZONE_LIMIT_CONVERT 或 RESISTANCE_ZONE_LIMIT_CONVERT，
                    # 則直接以當時覆寫的 price (即支撐位上限或阻力位下限) 作為掛單價，保證掛在完美的精準阻力/支撐區上。
                    _is_zone_convert = s.get("force_pullback_entry", False)
                    _atr_pct = 0.0
                    _pb_mult = 0.0

                    _is_structure_anchored = False
                    if _is_zone_convert and price > 0:
                        limit_price = price
                        _is_structure_anchored = True
                        logger.info(f"📌 [邊界精準限價單] {sym} 觸發阻力/支撐精算轉換，直接掛單在臨界價 {limit_price:.6f}")
                    else:
                        atr = s.get("current_atr", 0.0)
                        if atr <= 0:
                            atr = price * 0.015
                        _atr_pct = atr / price if price > 0 else 0.015
                        _pb_mult = ENTRY_PULLBACK_ATR_MULT * (1.6 if _atr_pct > 0.008 else 1.2)
                        
                        # 動態計算價格緩衝：極強信號(>=22)下沉 0.05% 確保吃單；普通信號下沉 0.1% (原 0.3% 太遠無法成交)
                        _sig_str = signal_strength or 0.0
                        _offset_ratio = 0.9995 if _sig_str >= 22.0 else 0.999
                        _opp_offset_ratio = 1.0005 if _sig_str >= 22.0 else 1.001

                        # MA7_Simple 只有 20 秒有效期（見 MA7_SIMPLE_PENDING_MAX_SEC），若再往
                        # 近期K線低/高點延伸掛單，經常掛在 20 秒內碰不到的價位，訊號一直觸發卻
                        # 從未成交。這條路線只用 ATR 回踩距離本身，不額外追近期K線極值。
                        _use_recent_extreme = route_key != "ma7_simple"
                        if side == 'buy':
                            target_pb = price - atr * _pb_mult
                            if _use_recent_extreme and len(s.get("ohlcv", [])) >= 2:
                                recent_low = min(s["ohlcv"][-1][3], s["ohlcv"][-2][3])
                                limit_price = min(target_pb, recent_low * _offset_ratio)
                                limit_price = max(limit_price, price - atr * (_pb_mult * 3.5))
                            else:
                                limit_price = target_pb
                        else:
                            target_pb = price + atr * _pb_mult
                            if _use_recent_extreme and len(s.get("ohlcv", [])) >= 2:
                                recent_high = max(s["ohlcv"][-1][2], s["ohlcv"][-2][2])
                                limit_price = max(target_pb, recent_high * _opp_offset_ratio)
                                limit_price = min(limit_price, price + atr * (_pb_mult * 3.5))
                            else:
                                limit_price = target_pb
                    if _ma_cross_anti_chase and _ma_cross_anchor > 0:
                        limit_price = _ma_cross_anchor
                        _is_structure_anchored = True
                        logger.info(f"🧲 [MA7反抽掛單] {sym} 等待 MA7 結構價 {limit_price:.6f}，不追延伸段")
                    limit_price = round_step(limit_price, tick_size)
                    logger.info(f"📌 [回踩限價掛單] {sym} 限價掛單價 {limit_price:.6f} (信號市價: {price:.6f}, ATR%:{_atr_pct*100:.2f}%, 追低乘數:{_pb_mult:.2f})")
                else:
                    if side == 'buy':
                        limit_price = bid1
                        logger.info(f"📌 [被動掛單] {sym} 掛買一 {limit_price:.6f} 等成交")
                    else:
                        limit_price = ask1
                        logger.info(f"📌 [被動掛單] {sym} 掛賣一 {limit_price:.6f} 等成交")
            except Exception as e:
                logger.info(f"⚠️ [計算掛單價失敗] 降級使用信號價: {e}")
                limit_price = price
                if actual_entry_mode == 'market':
                    order_type = 'market'
                    limit_price = None

            params = {'marginMode': 'isolated', 'timeInForce': 'GTC'}
            if actual_entry_mode == 'chase':
                params['timeInForce'] = 'IOC'
            if order_type == 'market':
                params.pop('timeInForce', None)

            order_price_ok, order_price_reason = _entry_price_guard(
                sym, side, limit_price, market_price,
                mode=actual_entry_mode,
                is_rescue_dca=is_rescue_dca,
            )
            if not order_price_ok and _is_structure_anchored and limit_price is not None:
                old_limit_price = limit_price
                refreshed_price, refreshed = _reanchor_rejected_passive_price(
                    sym, side, limit_price, market_price, mode=actual_entry_mode
                )
                if refreshed:
                    limit_price = round_step(refreshed_price, tick_size)
                    order_price_ok, order_price_reason = _entry_price_guard(
                        sym, side, limit_price, market_price, mode=actual_entry_mode,
                        is_rescue_dca=is_rescue_dca,
                    )
                    if order_price_ok:
                        logger.info(
                            f"🔄 [初次動態重掛] {sym} {side} 結構價 {old_limit_price:.6f} 已偏離，"
                            f"依最新市價 {market_price:.6f} 改掛 {limit_price:.6f}"
                        )
            if not order_price_ok:
                logger.info(f"🛑 [EntryPriceGuard] {sym} {side} 委託價偏離即時牌價：{order_price_reason}，取消開倉")
                logger.info(f"🧱 [ORDER_BLOCK] {sym} 被委託價格偏離攔截，未送單")
                return

            if is_rescue_dca:
                final_rescue_price = limit_price if limit_price is not None else market_price
                rescue_ok, rescue_reason = is_effective_rescue_dca(
                    s, side, final_rescue_price, add_qty=base_amt,
                )
                if not rescue_ok:
                    logger.info(f"🛑 [RescueDCAIneffective] {sym} 最終委託取消：{rescue_reason}")
                    return

            final_order_price = limit_price if limit_price is not None else market_price
            chase_ok, chase_reason = _entry_signal_chase_guard(
                side, price, final_order_price, is_first_entry, is_rescue_dca,
            )
            if not chase_ok:
                logger.info(f"🛑 [SignalChaseGuard] {sym} 首倉最終委託取消：{chase_reason}")
                return

            if not is_rescue_dca and str(entry_route or "").lower() in MA_ENTRY_ROUTES:
                from core.check_entries import _entry_structure_quality
                structure_ok, structure_reason, _ = _entry_structure_quality(
                    sym, side, entry_route, final_order_price
                )
                if not structure_ok:
                    logger.info(f"🛑 [Final_Structure_Price_Guard] {sym} 最終委託價不合格：{structure_reason}")
                    return
                from core.entry_filter import btc_macro_entry_guard
                final_macro_ok, final_macro_reason, _ = btc_macro_entry_guard(sym, side)
                if not final_macro_ok:
                    logger.info(f"🛑 [Final_BTC_Macro_Guard] {sym} 送單前方向已變化：{final_macro_reason}")
                    return

            exchange_direction_ok, exchange_direction_reason = await _entry_exchange_direction_guard(sym, side)
            if not exchange_direction_ok:
                logger.info(f"🛑 [ExchangeDirectionGuard] {sym} {side} 實盤持倉方向預檢失敗：{exchange_direction_reason}")
                return

            # API 延遲量測：只量「送出委託→交易所回應」這通 API 呼叫本身的耗時，
            # 不含後面故意等待成交的 sleep(3)——那是設計上刻意的等待，混進去量會讓
            # 每一筆都必然超過門檻，量不出真正的網路/交易所處理延遲。這裡量到的才是
            # 判斷 chase 這種要求快速掛單/追價的高動態模式，實際環境是否跟得上的依據。
            entry_prior_qty = float(s.get("qty", 0.0) or 0.0)
            _api_call_start = time.time()
            order = await exchange_futures.create_order(
                sym, type=order_type, side=side, amount=abs(base_amt), price=limit_price,
                params=params
            )
            _api_latency_ms = (time.time() - _api_call_start) * 1000
            if _api_latency_ms > 500:
                logger.info(f"⚠️ [API延遲警報] {sym} 下單 API 耗時 {_api_latency_ms:.0f}ms > 500ms，chase 高動態模式可能不適合目前環境，建議改用趨勢型/被動掛單策略")
            else:
                logger.info(f"⏱️ [API延遲] {sym} 下單 API 耗時 {_api_latency_ms:.0f}ms")
            order_id = order['id']
            order_ts = time.time()

            # 結構錨定掛單（等支撐/阻力回踩）給更長的等待時間——回踩本來就可能要
            # 好幾分鐘才會發生，120 秒太容易在真正回踩前就被判定「完全未成交」放棄。
            # 逾時後也不是直接放棄，而是交給 check_stale_limit_orders 判斷訊號是否
            # 還有效、價格有沒有跑掉，沒問題的話改追對手價再試一次（見該函式）。
            _order_timeout = (
                DUAL_SHOT_ORDER_TIMEOUT if is_rescue_dca
                else 150 if _is_structure_anchored
                else min(DUAL_SHOT_ORDER_TIMEOUT, 120)
            )
            ctx.PENDING_LIMIT_ORDERS[order_id] = {
                "sym": sym, "side": side, "qty": base_amt,
                "price": limit_price or price, "signal_price": price,
                "timestamp": order_ts, "is_rescue_dca": is_rescue_dca,
                "entry_route": entry_route, "signal_strength": signal_strength,
                "signal_candle_ts": s.get("ma_signal_candle_ts", 0),
                "market_reference_price": market_price,
                "last_reprice_at": order_ts, "reprice_count": 0,
                "timeout": _order_timeout,
                "allow_chase_on_timeout": (_is_structure_anchored and not is_rescue_dca
                                           and not _ma_cross_anti_chase),
                "ma_cross_anti_chase": _ma_cross_anti_chase,
                "chased_once": False,
            }
            logger.info(f"⏳ [限價單挂出] {sym} {side} {base_amt:.4f} @ {limit_price} (ID: {order_id}, 類型: {order_type}, 逾時: {_order_timeout:.0f}s)")

            await asyncio.sleep(3)
            fetched = order
            try:
                fetched = await exchange_futures.fetch_order(order_id, sym)
            except Exception as fetch_error:
                logger.info(f"⚠️ [成交查詢失敗] {sym} {order_id}: {fetch_error}")

            status = fetched.get("status", "")
            filled_qty = float(fetched.get("filled", 0.0) or 0.0)
            if order_type == "market" and filled_qty <= 0.000001:
                recovered_fill = await _recover_market_entry_fill(
                    sym, side, entry_prior_qty, base_amt, market_price,
                )
                if recovered_fill:
                    fetched = recovered_fill
                    status = "closed"
                    filled_qty = float(recovered_fill["filled"])
                    recovered_avg = float(recovered_fill.get("average", market_price))
                    logger.info(
                        f"✅ [市價成交回復] {sym} 從交易所持倉回查成交 "
                        f"{filled_qty:.4f} @ {recovered_avg:.6f}"
                    )

            requested_amt = base_amt
            if status not in ('closed', 'canceled') and filled_qty < base_amt * 0.99:
                latest_ref_price = 0.0
                try:
                    latest_ref_price = await get_reference_price(sym, exchange_futures)
                except Exception as pe:
                    logger.info(f"⚠️ [掛單逆向檢查] {sym} 取得最新參考價失敗: {pe}")
                if latest_ref_price <= 0:
                    latest_ref_price = float(s.get("last_trade_price", 0.0) or s.get("close_price", 0.0) or 0.0)
                adverse_ok, adverse_reason = _entry_pending_adverse_guard(
                    sym, side, price, latest_ref_price, is_rescue_dca=is_rescue_dca
                )
                if not adverse_ok and not _is_dynamic_pending_entry(ctx.PENDING_LIMIT_ORDERS.get(order_id, {})):
                    try:
                        await exchange_futures.cancel_order(order_id, sym)
                    except Exception as ce:
                        logger.info(f"⚠️ [逆向撤單失敗] {sym} {order_id}: {ce}")
                    ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)
                    if filled_qty <= 0.000001:
                        logger.info(f"🛑 [逆向撤單] {sym} {side} 掛單後價格反向偏離：{adverse_reason}，未成交部分已撤，放棄本次進場")
                        return
                    logger.info(f"🛑 [逆向撤單] {sym} {side} 掛單後價格反向偏離：{adverse_reason}，撤銷剩餘數量，保留已成交 {filled_qty:.4f}")

            if status == 'closed' or filled_qty >= base_amt * 0.99:
                ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)
                fill_price = float(fetched.get('average') or fetched.get('price') or limit_price or market_price)
                logger.info(f"✅ [限價成交] {sym} {side} {filled_qty:.4f} @ {fill_price:.6f}")
            elif actual_entry_mode == 'chase':
                # 原本這裡不管成交多少（包含完全沒成交），都直接放棄剩餘數量，
                # 只留下「由逾期止單機制接管」這句話，但實際上從來沒有其他地方
                # 真的去監控/處理 ctx.PENDING_LIMIT_ORDERS，等於剩餘部位就這樣
                # 被默默放棄，倉位比訊號原本要求的還小、甚至完全沒進場。
                # chase 模式本身用 IOC，未成交部分交易所會自動取消，不會留下孤兒
                # 掛單，所以在這裡追一次新的對手價再試一次是安全的，能讓進場更
                # 接近原本訊號要求的完整倉位。
                ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)
                remaining_amt = base_amt - filled_qty
                fill_notional = filled_qty * float(fetched.get('average') or limit_price) if filled_qty > 0 else 0.0
                if filled_qty > 0:
                    logger.info(f"⚠️ [部分成交] {sym} 第1次掛單成交 {filled_qty:.4f}/{base_amt:.4f}，剩餘 {remaining_amt:.4f} 追價再試")
                else:
                    logger.info(f"⏳ [未成交] {sym} 第1次掛單 3 秒未成交，追價再試一次")
                try:
                    ob2 = await exchange_futures.fetch_order_book(sym, limit=5)
                    asks2 = ob2.get('asks', [])
                    bids2 = ob2.get('bids', [])
                    ask1_2 = float(asks2[0][0]) if asks2 else limit_price
                    bid1_2 = float(bids2[0][0]) if bids2 else limit_price
                    reprice = ask1_2 if side == 'buy' else bid1_2
                    reprice = reprice * (1 + ENTRY_CHASE_OFFSET_PCT) if side == 'buy' else reprice * (1 - ENTRY_CHASE_OFFSET_PCT)
                    # 攤平救援封頂：跟第一次掛單同一套規則，第二次追價也不能追到比
                    # 均價差的位置，不然重試機制反而更容易把攤平買在比均價還差的價位。
                    if is_rescue_dca and s.get("avg_price", 0) > 0:
                        if side == 'buy':
                            reprice = min(reprice, s["avg_price"])
                        else:
                            reprice = max(reprice, s["avg_price"])
                    prec2 = await get_contract_precision(sym)
                    reprice = round_step(reprice, prec2['tick_size'])
                    remaining_amt = round_step(remaining_amt, prec2['step_size'])
                except Exception as re_e:
                    logger.info(f"⚠️ [追價報價失敗] {sym}: {re_e}")
                    reprice = limit_price

                reprice_ok, reprice_reason = _entry_price_guard(
                    sym, side, reprice, market_price,
                    mode="chase", is_rescue_dca=is_rescue_dca,
                )
                if not reprice_ok:
                    logger.info(f"🛑 [EntryPriceGuard] {sym} 二次追價偏離即時牌價：{reprice_reason}，放棄剩餘進場量")
                    remaining_amt = 0.0
                if is_rescue_dca and remaining_amt > 0.000001:
                    rescue_ok, rescue_reason = is_effective_rescue_dca(
                        s, side, reprice, add_qty=remaining_amt,
                    )
                    if not rescue_ok:
                        logger.info(f"🛑 [RescueDCAIneffective] {sym} 二次追價取消：{rescue_reason}")
                        remaining_amt = 0.0
                if remaining_amt > 0.000001:
                    chase_ok, chase_reason = _entry_signal_chase_guard(
                        side, price, reprice, is_first_entry, is_rescue_dca,
                    )
                    if not chase_ok:
                        logger.info(f"🛑 [SignalChaseGuard] {sym} 首倉二次追價取消：{chase_reason}")
                        remaining_amt = 0.0

                if remaining_amt > 0.000001:
                    try:
                        order2 = await exchange_futures.create_order(
                            sym, type='limit', side=side, amount=remaining_amt, price=reprice,
                            params={'marginMode': 'isolated', 'timeInForce': 'IOC'}
                        )
                        await asyncio.sleep(3)
                        fetched2 = await exchange_futures.fetch_order(order2['id'], sym)
                        filled_qty2 = float(fetched2.get('filled', 0.0))
                        if filled_qty2 > 0:
                            fill_notional += filled_qty2 * float(fetched2.get('average') or reprice)
                            filled_qty += filled_qty2
                            logger.info(f"✅ [追價成交] {sym} 第2次掛單成交 {filled_qty2:.4f} @ {reprice:.6f}")
                    except Exception as ce:
                        logger.info(f"🚨 [追價下單失敗] {sym}: {ce}")

                if filled_qty <= 0.000001:
                    logger.info(f"⏳ [進場放棄] {sym} 追價後仍未成交，放棄本次進場")
                    return
                fill_price = fill_notional / filled_qty
                base_amt = filled_qty
                if filled_qty < requested_amt * 0.99:
                    logger.info(f"⚠️ [進場部分完成] {sym} 最終成交 {filled_qty:.4f}/{requested_amt:.4f}")
            elif filled_qty > 0:
                fill_price = float(fetched.get('average') or limit_price)
                base_amt = filled_qty
                logger.info(f"⚠️ [部分成交] {sym} 實際成交: {filled_qty:.4f} (OK率: {filled_qty/base_amt*100:.1f}%)")
            else:
                logger.info(f"⏳ [等待成交] {sym} 限價單 {order_id} 尚未成交，由逃期止單機制接管")
                return

            old_qty = s["qty"]
            expected_qty = old_qty + (base_amt if side == "buy" else -base_amt)
            exchange_entry_price = 0.0
            position_synced = False
            try:
                positions = await exchange_futures.fetch_positions([sym])
                actual_pos, actual_qty = _find_exchange_position(positions, sym)
                if actual_pos:
                    s["qty"] = actual_qty
                    position_synced = True
                    exchange_entry_price = float(
                        actual_pos.get("entryPrice")
                        or actual_pos.get("info", {}).get("entryPrice")
                        or 0.0
                    )
                    logger.info(f"📊 [持倉同步] {sym} 交易所實際持倉: {actual_qty:.4f}")
            except Exception as pe:
                logger.info(f"⚠️ [持倉同步失敗] {sym}: {pe}")

            if not position_synced:
                s["qty"] = expected_qty

            slippage = abs(fill_price - price) / price if price > 0 else 0
            limit_price_str = f"{limit_price:.6f}" if limit_price is not None else "Market"
            logger.info(f"✅ [實盤開倉成功] {sym} {side} | 信號價: {price:.6f} | 限價: {limit_price_str} | 實際: {fill_price:.6f} | 滑價: {slippage*100:.3f}%")

            if exchange_entry_price > 0:
                s["avg_price"] = exchange_entry_price
                s["entry_atr"] = max(s.get("current_atr", 0.0), exchange_entry_price * 0.005)
            elif s["avg_price"] <= 0:
                s["avg_price"] = fill_price
                s["entry_atr"] = max(s.get("current_atr", 0.0), fill_price * 0.005)
            else:
                old_abs_qty = abs(old_qty)
                s["avg_price"] = ((s["avg_price"] * old_abs_qty) + (fill_price * base_amt)) / abs(s["qty"])

            if "entries" not in s:
                s["entries"] = []
            s["entries"].append({"price": fill_price, "qty": base_amt, "time": now, "side": side})

            s["open_time"] = now
            from core.entry_time_store import save_entry_time
            save_entry_time(sym, now)
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["last_entry_price"] = fill_price
            s["restored_from_exchange"] = False
            s["last_entry_direction"] = side
            s["entry_count"] += 1

            if s["entry_count"] == 1:
                s["is_breakeven_locked"] = False
                # 新持倉必須先刪除上一個生命週期的磁碟峰值；否則重啟會誤載舊高點。
                _clear_previous_position_peak(sym, s)
                s["first_entry_price"] = fill_price
                s["entry_strength"] = signal_strength if signal_strength is not None else 0.0
                s["_lin_trail_armed"] = False
                s["_lin_trail_activation_price"] = 0.0
                s["_lin_trail_activation_stop"] = 0.0

            _import_update_trailing_stop()(sym, fill_price, side == 'buy')

            if s["entry_count"] >= 2:
                first_entry_price = s["entries"][0]["price"]
                if side == 'buy':
                    s["trailing_stop_price"] = max(s["trailing_stop_price"], first_entry_price)
                else:
                    s["trailing_stop_price"] = min(s["trailing_stop_price"], first_entry_price) if s["trailing_stop_price"] > 0 else first_entry_price
                s["is_breakeven_locked"] = True

            s["last_flip_time"] = now

            try:
                await _replace_exchange_exit_orders(sym)
            except Exception as se:
                logger.info(f"🚨 [交易所退出單挂單失敗] {sym}: {se}")

        except Exception as e:
            err_str = str(e)
            logger.info(f"🚨 [開倉錯誤] {sym}: {e}")
            # -1007 代表「送出狀態未知」，連交易所自己都不確定訂單有沒有成交。直接放著
            # 讓下一輪重試，等於在不知道上一筆有沒有成交的情況下又送一筆新單，萬一上一筆
            # 其實悄悄成交了，會意外多開一倍倉位；就算沒成交，也會一直反覆撞在同一個卡住
            # 的幣種上。這裡先跟交易所核對真實持倉：真的有意外成交就明確示警（不靜默吞掉），
            # 確認沒有才進入短暫冷卻，不要立刻對同一個幣種重試。
            if not PAPER_TRADING and ("-1007" in err_str or "Timeout waiting for response" in err_str):
                try:
                    positions = await exchange_futures.fetch_positions([sym])
                    _, real_qty = _find_exchange_position(positions, sym)
                except Exception as sync_err:
                    real_qty = 0.0
                    logger.info(f"⚠️ [開倉錯誤後核對失敗] {sym}: {sync_err}")
                _prior_qty_ref = entry_prior_qty if 'entry_prior_qty' in locals() else float(s.get("qty", 0.0) or 0.0)
                if abs(real_qty) > 0.000001 and abs(real_qty - _prior_qty_ref) > 0.000001:
                    logger.info(f"🚨🚨 [開倉錯誤後核對] {sym} 逾時錯誤但交易所實際已有新倉位 {real_qty}（先前 {_prior_qty_ref}），並非真的沒送出！機器人現在自動接管此倉位監控並同步狀態。")
                    s["qty"] = real_qty
                    entry_p = 0.0
                    for p_info in positions:
                        p_sym = p_info.get('symbol', '').split(':')[0].replace('/', '')
                        if p_sym == sym:
                            entry_p = float(p_info.get('entryPrice', p_info.get('avg_price', 0.0)) or 0.0)
                            break
                    if entry_p <= 0.0:
                        entry_p = float(s.get("close_price", 0.0))
                    
                    s["avg_price"] = entry_p
                    s["first_entry_price"] = entry_p
                    s["last_entry_price"] = entry_p
                    s["last_entry_direction"] = "buy" if real_qty > 0 else "sell"
                    s["restored_from_exchange"] = True
                    s["open_time"] = time.time()
                    s["entry_count"] = max(s.get("entry_count", 0) + 1, 1)
                    s["is_breakeven_locked"] = False
                    s["highest_profit_pct"] = 0.0
                    
                    # 隨即設定交易所退出掛單
                    try:
                        await _replace_exchange_exit_orders(sym)
                    except Exception as se:
                        logger.info(f"🚨 [交易所退出單掛單失敗] {sym}: {se}")
                else:
                    s["order_fail_cooldown_until"] = time.time() + 60
                    logger.info(f"⏳ [開倉錯誤冷卻] {sym} 確認交易所端真的沒有新倉位，暫停 60 秒後才會再考慮進場，避免重複撞同一個逾時問題")


async def _chase_retry_pending_entry(info, sym, side, qty):
    """結構錨定掛單等不到回踩、逾時了，但訊號還有效、價格也沒反向跑掉時，
    不要直接放棄，改追當下的對手價再掛一張限價單試一次——維持 Maker 身份（比
    直接轉市價便宜），又比死守原來那個支撐/阻力價更容易成交。只追一次
    （chased_once=True），避免變成無限期反覆等待。

    實測 LINKUSDT 案例：外層的 chase_eligible 判斷通過後，到這裡真正送出委託前
    還有一段空窗，剛好撞上瞬間爆量尖峰（4.23x 均量），等於在最不穩定的瞬間強行
    進場，之後很容易被巴。這裡在真正下單前，用剛抓到的最新報價再做一次「開倉價
    是否已經跑掉」與「當下是不是異常爆量尖峰」的最終確認，只要有一項不通過就
    放棄這次追價（不是報錯，是判斷現在不適合強行進場），留給下一輪(30秒後)重新
    評估，而不是不管三七二十一都硬追進去。"""
    try:
        ob = await exchange_futures.fetch_order_book(sym, limit=5)
        asks = ob.get('asks', [])
        bids = ob.get('bids', [])
        ask1 = float(asks[0][0]) if asks else 0.0
        bid1 = float(bids[0][0]) if bids else 0.0
        reprice = ask1 if side == 'buy' else bid1
        if reprice <= 0:
            return False

        # 最終確認一：開倉價有沒有已經跑掉（用最新報價 vs 原始訊號價重新檢查一次）
        signal_price = info.get("signal_price") or info.get("price")
        adverse_ok, adverse_reason = _entry_pending_adverse_guard(
            sym, side, signal_price, reprice, is_rescue_dca=info.get("is_rescue_dca", False),
        )
        if not adverse_ok:
            logger.info(f"🛑 [追價前檢查] {sym} 開倉價已跑掉，放棄本次追價：{adverse_reason}")
            return False

        # 最終確認二：當下是不是異常爆量尖峰（流動性瞬間變薄、容易是插針/軋空），
        # 這種瞬間強行進場風險極高，寧可跳過這輪、等尖峰退去後再評估。
        s_chase = ctx.STATES.get(sym, {})
        current_vol = float(s_chase.get("current_vol", 0.0) or 0.0)
        vol_ma20 = float(s_chase.get("vol_ma20", 0.0) or 0.0)
        vol_spike_ratio = current_vol / vol_ma20 if vol_ma20 > 0 else 0.0
        if vol_spike_ratio > 3.0:
            logger.info(
                f"🛑 [追價前檢查] {sym} 當下量能異常爆發 ({vol_spike_ratio:.2f}x 均量)，"
                f"疑似插針/流動性瞬間變薄，放棄本次追價"
            )
            return False

        reprice = reprice * (1 + ENTRY_CHASE_OFFSET_PCT) if side == 'buy' else reprice * (1 - ENTRY_CHASE_OFFSET_PCT)
        prec = await get_contract_precision(sym)
        reprice = round_step(reprice, prec['tick_size'])
        qty = round_step(qty, prec['step_size'])
        if qty <= 0:
            return False

        params = {'marginMode': 'isolated', 'timeInForce': 'GTC'}
        order = await exchange_futures.create_order(
            sym, type='limit', side=side, amount=qty, price=reprice, params=params
        )
        order_id = order['id']
        ctx.PENDING_LIMIT_ORDERS[order_id] = {
            "sym": sym, "side": side, "qty": qty,
            "price": reprice, "signal_price": info.get("signal_price") or reprice,
            "timestamp": time.time(), "is_rescue_dca": info.get("is_rescue_dca", False),
            "entry_route": info.get("entry_route"), "signal_strength": info.get("signal_strength"),
            "signal_candle_ts": info.get("signal_candle_ts", 0),
            "timeout": min(DUAL_SHOT_ORDER_TIMEOUT, 90),
            "allow_chase_on_timeout": False,
            "chased_once": True,
        }
        logger.info(
            f"📌 [逾時追價] {sym} {side} 結構錨定掛單逾時但訊號仍有效，"
            f"改追對手價 {reprice:.6f} 再試一次 (ID: {order_id})"
        )
        return True
    except Exception as e:
        logger.info(f"⚠️ [逾時追價失敗] {sym}: {e}")
        return False


async def _relist_pending_entry(info, sym, side, qty, current_price):
    """Cancel/relist path for a still-valid MA entry whose quote reference moved."""
    try:
        new_price = _translated_pending_limit_price(info, current_price)
        if new_price <= 0:
            return False
        prec = await get_contract_precision(sym)
        new_price = round_step(new_price, prec["tick_size"])
        qty = round_step(qty, prec["step_size"])
        if qty <= 0:
            return False
        price_ok, price_reason = _entry_price_guard(
            sym, side, new_price, current_price, mode=("ma7_pullback" if info.get("ma_cross_anti_chase", False) else "range_limit" if str(info.get("entry_route") or "").lower() in RANGE_ENTRY_ROUTES else "pullback"),
            is_rescue_dca=False,
        )
        if not price_ok:
            logger.info(f"🛑 [動態重掛取消] {sym} 最新委託價不合格：{price_reason}")
            return False
        chase_ok, chase_reason = _entry_signal_chase_guard(
            side, current_price, new_price, is_first_entry=True, is_rescue_dca=False,
        )
        if not chase_ok:
            logger.info(f"🛑 [動態重掛取消] {sym} 最新委託價形成追價：{chase_reason}")
            return False
        order = await exchange_futures.create_order(
            sym, type="limit", side=side, amount=qty, price=new_price,
            params={"marginMode": "isolated", "timeInForce": "GTC"},
        )
        now = time.time()
        new_info = dict(info)
        reprice_count = int(info.get("reprice_count", 0)) + 1
        new_info.update({
            "sym": sym, "side": side, "qty": qty, "price": new_price,
            "signal_price": current_price, "market_reference_price": current_price,
            "timestamp": now, "last_reprice_at": now,
            "reprice_count": reprice_count,
            "allow_chase_on_timeout": False, "chased_once": False,
        })
        ctx.PENDING_LIMIT_ORDERS[order["id"]] = new_info
        logger.info(
            f"🔄 [動態重掛] {sym} {side} 撤舊單後依最新市價 {current_price:.6f}，"
            f"改掛被動限價 {new_price:.6f} (第 {reprice_count} 次)"
        )
        return True
    except Exception as exc:
        logger.info(f"⚠️ [動態重掛失敗] {sym}: {exc}")
        return False


async def _record_missed_limit_fill(sym, side, order_id, info, fetched, positions=None):
    """訂單在 check_stale_limit_orders 判定逾時、真正呼叫撤單前已經成交
    （包含 closed 全成與 canceled 部分成交）。原本這裡把成交後的 canceled
    當成完全未成交，直接丟棄
    追蹤，導致本地 qty/avg_price 從未更新、交易所也沒掛出保護單，一筆真實倉位
    在最多 60 秒內完全沒人管（直到 periodic_position_reconciliation 才發現），
    而且那條路徑會把這筆「其實是自己剛成交的單」誤判成重啟接管的舊倉位。
    這裡補上正式的成交記錄與保護單，讓漏接的成交立刻被納管。"""
    s = ctx.STATES.get(sym)
    if s is None:
        return
    if positions is None:
        try:
            positions = await exchange_futures.fetch_positions([sym])
        except Exception as exc:
            logger.info(f"⚠️ [漏接成交同步失敗] {sym} {order_id}: {exc}")
            return
    actual_pos, actual_qty = _find_exchange_position(positions, sym)
    if actual_pos is None:
        logger.info(f"⚠️ [漏接成交] {sym} 訂單 {order_id} 顯示已成交，但交易所目前查無持倉，略過")
        return

    now = time.time()
    entry_price = float(
        actual_pos.get("entryPrice")
        or actual_pos.get("info", {}).get("entryPrice")
        or fetched.get("average") or fetched.get("price") or 0.0
    )
    fill_price = float(fetched.get("average") or fetched.get("price") or entry_price or 0.0)
    is_first_entry = s.get("entry_count", 0) == 0 or s.get("open_time", 0) <= 0

    s["qty"] = actual_qty
    s["avg_price"] = entry_price or s.get("avg_price", 0.0) or fill_price
    if is_first_entry:
        s["open_time"] = now
        from core.entry_time_store import save_entry_time
        save_entry_time(sym, now)
        s["highest_profit_pct"] = 0.0
        s["max_profit_reached"] = 0.0
        s["ma_peak_saved_pct"] = 0.0
        s["ma_peak_lock_armed"] = False
        s["ma_peak_lock_price"] = 0.0
        s["ma_profit_floor_armed"] = False
        s["ma_profit_floor_price"] = 0.0
        s["ma_profit_floor_cross_count"] = 0
        s["ma_profit_floor_cross_since"] = 0.0
        s["realtime_peak_candidate_price"] = 0.0
        s["realtime_peak_candidate_profit"] = 0.0
        s["realtime_peak_candidate_time"] = 0.0
        s["is_breakeven_locked"] = False
        s.pop("dynamic_exit_manager", None)
        clear_peak(sym)
    s["last_buy_time"] = now
    s["last_entry_time"] = now
    s["last_entry_price"] = fill_price
    s["last_entry_direction"] = side
    s["restored_from_exchange"] = False
    s["entry_count"] = max(s.get("entry_count", 0), 1)
    s["first_entry_price"] = fill_price if is_first_entry else s.get("first_entry_price", fill_price)
    s["entry_strength"] = float(info.get("signal_strength", 0.0) or 0.0)
    s["entry_atr"] = max(float(s.get("current_atr", 0.0) or 0.0), float(s["avg_price"]) * 0.005)
    s.setdefault("entries", []).append({
        "price": fill_price, "qty": abs(actual_qty), "time": now, "side": side,
    })
    s["pending_side"] = None
    s["pending_time"] = 0
    s["status"] = "ACTIVE"
    route = info.get("entry_route")
    if route:
        s["entry_reason"] = route
        from core.entry_reason_store import save_entry_reason
        save_entry_reason(sym, route)

    logger.info(
        f"⚠️ [漏接成交] {sym} 訂單 {order_id} 已有成交 @ {fill_price:.6f}"
        f"（qty={actual_qty:.4f}），補記錄為正式倉位並立即掛出保護單"
    )
    try:
        await _ensure_exchange_exit_orders(sym)
    except Exception as se:
        logger.info(f"🚨 [漏接成交後掛單失敗] {sym}: {se}")


def _pending_entry_time_expired(info, elapsed, max_wait_seconds):
    """MA7 轉折掛單短效；其他動態結構掛單仍可持續重掛。"""
    route_key = str(info.get("entry_route", "") or "").lower()
    if route_key == "ma7_simple":
        return elapsed > MA7_SIMPLE_PENDING_MAX_SEC
    return elapsed > max_wait_seconds and not _is_dynamic_pending_entry(info)


async def check_stale_limit_orders():
    """
    超時撤單機制 (Order Timeout Canceller)
    每 3 秒檢查一次 PENDING_LIMIT_ORDERS；MA 掛單偏離時依最新價格重掛。
    超過 MAX_WAIT_SECONDS 仍未撮合的限價進場單自動撤銷。
    """
    while True:
        await asyncio.sleep(MA_PENDING_MONITOR_INTERVAL_SEC)
        if PAPER_TRADING:
            continue
        for order_id in list(ctx.PENDING_LIMIT_ORDERS.keys()):
            info = ctx.PENDING_LIMIT_ORDERS.get(order_id)
            if not info:
                continue
            elapsed = time.time() - info["timestamp"]

            sym = info["sym"]
            side = info.get("side", "")
            original_qty = info.get("qty", 0.0)
            max_wait_seconds = float(info.get("timeout", DUAL_SHOT_ORDER_TIMEOUT))
            dynamic_strategy_order = _is_dynamic_pending_entry(info)
            route_key = str(info.get("entry_route", "") or "").lower()
            ma7_simple_expired = (
                route_key == "ma7_simple" and elapsed > MA7_SIMPLE_PENDING_MAX_SEC
            )
            # MA7_Simple 是當下轉折，不能像 MA25 結構價一樣永久等待。
            # 舊掛單曾在訊號反轉 2 分半後才成交，因此僅保留短暫回踩窗口。
            should_cancel = _pending_entry_time_expired(
                info, elapsed, max_wait_seconds,
            )
            chase_eligible = should_cancel
            reprice_eligible = False
            latest_ref_price = 0.0
            cancel_reason = (
                f"已掛單 {elapsed:.1f} 秒 > {max_wait_seconds:.0f}s"
                if should_cancel else ""
            )
            if ma7_simple_expired:
                cancel_reason = f"MA7 轉折掛單已超過 {MA7_SIMPLE_PENDING_MAX_SEC:.0f}s 有效期"
                chase_eligible = False

            setup_ok, setup_reason = _pending_entry_setup_valid(info)
            if not setup_ok:
                should_cancel = True
                chase_eligible = False
                cancel_reason = f"進場訊號已失效: {setup_reason}"

            if setup_ok and not should_cancel:
                try:
                    latest_ref_price = await get_reference_price(sym, exchange_futures)
                except Exception as pe:
                    logger.info(f"⚠️ [掛單價格掃描] {sym} 取得最新參考價失敗: {pe}")
                s_check = ctx.STATES.get(sym, {})
                if latest_ref_price <= 0:
                    latest_ref_price = float(s_check.get("last_trade_price", 0.0)
                        or s_check.get("close_price", 0.0) or 0.0)
                if dynamic_strategy_order:
                    reprice_eligible, reprice_reason = _pending_entry_reprice_needed(
                        sym, info, latest_ref_price,
                    )
                    if reprice_eligible:
                        should_cancel = True
                        chase_eligible = False
                        cancel_reason = f"價格偏離，重新掛單: {reprice_reason}"
                else:
                    adverse_ok, adverse_reason = _entry_pending_adverse_guard(
                        sym, side, info.get("signal_price") or info.get("price"),
                        latest_ref_price, is_rescue_dca=info.get("is_rescue_dca", False),
                    )
                    if not adverse_ok:
                        should_cancel = True
                        chase_eligible = False
                        cancel_reason = adverse_reason

            if not should_cancel:
                continue

            chase_eligible = (
                chase_eligible
                and info.get("allow_chase_on_timeout", False)
                and not info.get("chased_once", False)
            )

            cancel_ok = False
            filled_qty = 0.0
            try:
                fetched = await exchange_futures.fetch_order(order_id, sym)
                order_status = fetched.get('status', '')
                filled_qty = float(fetched.get('filled', 0.0) or 0.0)

                if order_status == 'canceled':
                    if filled_qty > 0.000001:
                        await _record_missed_limit_fill(sym, side, order_id, info, fetched)
                    ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)
                    logger.info(
                        f"ℹ️ [超時撤單] {sym} 訂單 {order_id} 已為 canceled 狀態"
                        f"（成交 {filled_qty:.4f}/{original_qty:.4f}），跳過重複撤單。"
                    )
                    continue

                if order_status == 'closed':
                    # 'closed' 在 ccxt/幣安語意上代表「完全成交」，跟 'canceled' 完全不同，
                    # 不能同一分支直接丟棄追蹤，否則這筆真實成交會變成沒人管的孤兒倉位。
                    await _record_missed_limit_fill(sym, side, order_id, info, fetched)
                    ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)
                    continue

                await exchange_futures.cancel_order(order_id, sym)
                cancel_ok = True
                logger.info(
                    f"⏳ [限價撤單] {sym} 取消待成交進場單 "
                    f"({cancel_reason})。"
                    f"為防止穿價/反向偏離風險，執行自動撤單！ OrderID: {order_id} "
                    f"部分成交量: {filled_qty:.4f}/{original_qty:.4f}"
                )
            except Exception as ce:
                logger.info(f"⚠️ [超時撤單失敗] {sym} {order_id}: {ce}")

            try:
                positions = await exchange_futures.fetch_positions([sym])
                actual_pos, _ = _find_exchange_position(positions, sym)
                s = ctx.STATES.get(sym)
                if not s:
                    ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)
                    continue

                if actual_pos:
                    await _record_missed_limit_fill(
                        sym, side, order_id, info, fetched, positions=positions,
                    )
                else:
                    chased = False
                    if reprice_eligible and filled_qty <= 0.000001 and s.get("entry_count", 0) == 0 and abs(s.get("qty", 0.0)) < 1e-6:
                        chased = await _relist_pending_entry(
                            info, sym, side, original_qty, latest_ref_price,
                        )
                    if not chased and chase_eligible and s.get('entry_count', 0) == 0 and abs(s.get('qty', 0.0)) < 1e-6:
                        chased = await _chase_retry_pending_entry(info, sym, side, original_qty)
                    if not chased:
                        logger.info(
                            f"🔄 [狀態重置] {sym} 限價單完全未成交 (filled=0)，"
                            f"撤單後清除追蹤狀態，機器人重回 ACTIVE 掃描模式。"
                        )
                        if s.get('entry_count', 0) == 0 and abs(s.get('qty', 0.0)) < 1e-6:
                            s["pending_side"] = None
                            s["pending_time"] = 0
                            s["last_entry_time"] = 0.0
                            s["status"] = "ACTIVE"

            except Exception as pe:
                logger.info(f"⚠️ [持倉同步失敗] {sym}: {pe}")

            finally:
                # 保留追蹤直到成交接管完成；校準程序看見它時就不會把這筆新成交
                # 誤判為重啟前的舊倉位，載入上一筆交易的歷史盈利峰值。
                ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)
