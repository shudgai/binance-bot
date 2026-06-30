import logging
import asyncio
import time
import json
import os
from datetime import datetime

from core import ctx

from core.config import (PAPER_TRADING, TRADE_HISTORY_FILE, DUAL_SHOT_ORDER_TIMEOUT,
    DUAL_SHOT_LEVERAGE, COIN_PROFILE_CONFIG, HARD_STOP_LOSS_PCT, DUAL_SHOT_MAX_SLOTS,
    DEFAULT_REVERSAL_SETTINGS, SYMBOL_REVERSAL_SETTINGS,
    ENTRY_ORDER_MODE, ENTRY_PULLBACK_ATR_MULT, ENTRY_CHASE_OFFSET_PCT)
from core.exchange_client import exchange_futures, sanitize_order_qty, get_contract_precision, round_step, convert_to_ccxt_symbol
from core.balance import get_balance, compute_per_coin_margin, accrue_daily_realized_pnl, get_total_wallet_balance
import core.balance as _bal
from core.state_manager import mark_exit, reset_coin_state, build_symbol_state
from core.symbol_profile import apply_symbol_profile, SYMBOL_PROFILES
from core.config import get_symbol_leverage
from services.utils import paper_key
from services.update_paper_state import update_paper_state
from services.ai_manager import ai_engine

logger = logging.getLogger(__name__)


def _import_update_trailing_stop():
    from core.exits import update_trailing_stop
    return update_trailing_stop


def record_trade_result(symbol, entry_reason, exit_reason, profit_pct, current_atr, max_profit_reached=0.0,
                        expected_entry=0.0, expected_exit=0.0, actual_entry=0.0, actual_exit=0.0, fees=0.0, qty=0.0):
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

    # --- 新增：AI 經驗摘要生成邏輯 ---
    pnl_tag = "[大賺]" if profit_pct > 0.01 else "[微利]" if profit_pct > 0.002 else "[打平]" if profit_pct > -0.002 else "[小虧]" if profit_pct > -0.01 else "[大虧]"

    is_anomaly = False
    if "Layer_1" in exit_reason or "Breakout" in exit_reason:
        is_anomaly = True
    if friction_rate > 0.4:
        is_anomaly = True

    summary = f"{pnl_tag} {symbol} 透過 {exit_reason} 出場。獲利 {profit_pct*100:.2f}%，摩擦力 {friction_rate:.2f}%。"
    if is_anomaly:
        summary += " (⚠️ 異常交易，需重點關注)"

    trade_data = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "entry_reason": entry_reason or "UNKNOWN",
        "exit_reason": exit_reason,
        "profit_pct": round(profit_pct, 4),
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
        "theoretical_profit": round((expected_exit - expected_entry)/expected_entry if expected_entry > 0 else 0.0, 4),
        "ai_summary": summary
    }

    if os.path.exists(history_file):
        with open(history_file, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
                if not isinstance(history, list): history = []
            except: history = []
    else:
        history = []

    history.append(trade_data)

    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=4, ensure_ascii=False)
        logger.info(f"📝 [AI Memory] 已記錄 {symbol} 並產生摘要: {summary}")
    except Exception as e:
        logger.info(f"⚠️ [AI Memory] 紀錄失敗: {e}")


async def close_position(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = ctx.STATES[sym]
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

    if profit_pct <= 0 and reason != "[GLOBAL_MELTDOWN]":
        logger.info(f"⏳ [平倉攔截] {sym} 目前無利潤 ({profit_pct*100:.4f}%)，根據設定必須等到有利潤再平倉！已拒絕平倉 | 原因={reason}")
        return

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

    sanitized_qty = await sanitize_order_qty(sym, qty)
    if sanitized_qty <= 0.0:
        logger.info(f"⚠️ [平倉風控] {sym} 無法取得有效數量 ({qty:.6f})")
        return
    qty = sanitized_qty

    if PAPER_TRADING:
        real_avg = s["avg_price"] if s["avg_price"] > 0 else avg_price
        if s["qty"] > 0:
            pnl = (price - real_avg) * qty
        else:
            pnl = (real_avg - price) * qty
        update_paper_state(pk, close_side, price, qty, is_close=True, pnl=pnl)
    else:
        try:
            await exchange_futures.create_order(sym, type="market", side=close_side, amount=qty,
                                        params={"reduceOnly": True})
        except Exception as e:
            logger.info(f"🚨 [平倉錯誤] {sym}: {e}")
            return

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
        fees=0.0,
        qty=qty
    )

    from core.config import DAILY_LOSS_LIMIT_PCT
    try:
        accrue_daily_realized_pnl(profit_pct, real_avg * qty)
        if profit_pct < 0:
            logger.info(f"[每日熔斷追蹤] {sym} 虧損 {profit_pct*100:.2f}% | 今日累計: {_bal._DAILY_REALIZED_LOSS*100:.2f}% / {DAILY_LOSS_LIMIT_PCT*100:.1f}%")
    except Exception as _e:
        logger.info(f"[每日熔斷追蹤失敗] {_e}")

    remaining = abs(s["qty"]) - qty
    if remaining < 0.01:
        if remaining > 0.000001:
            logger.info(f"🧹 [塵埃清理] {sym} 剩餘 {remaining:.6f} 視為已清")
        if s.get("exchange_stop_order_id") and not PAPER_TRADING:
            try:
                await exchange_futures.cancel_order(s["exchange_stop_order_id"], sym)
                logger.info(f"✅ [止損單取消] {sym} 部位已全平，撤銷交易所止損單")
            except Exception as ce:
                logger.info(f"⚠️ [取消止損單失敗] {sym}: {ce}")

        mark_exit(sym, is_stop_loss=is_stop_loss, reason=full_reason, loss_pct=profit_pct)
        reset_coin_state(sym)
    else:
        prec = await get_contract_precision(sym)
        raw_qty = (abs(s["qty"]) - qty) * (1 if s["qty"] > 0 else -1)
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

        if s.get("exchange_stop_order_id") and not PAPER_TRADING:
            try:
                await exchange_futures.cancel_order(s["exchange_stop_order_id"], sym)
                stop_side = "sell" if s["qty"] > 0 else "buy"
                hard_sl_pct = s.get("hard_stop_loss_pct", 0.02)
                stop_price = s["avg_price"] * (1 - hard_sl_pct) if s["qty"] > 0 else s["avg_price"] * (1 + hard_sl_pct)
                stop_price = round_step(stop_price, prec["tick_size"])
                new_stop = await exchange_futures.create_order(
                    sym, type="STOP_MARKET", side=stop_side, amount=abs(s["qty"]),
                    params={"stopPrice": stop_price, "reduceOnly": True}
                )
                s["exchange_stop_order_id"] = new_stop["id"]
                logger.info(f"🛡️ [止損單更新] {sym} 部分平倉後更新止損單 @ {stop_price} (數量: {abs(s['qty'])})")
            except Exception as ce:
                logger.info(f"⚠️ [更新止損單失敗] {sym}: {ce}")


def should_recover_from_reversal(sym, is_long):
    s = ctx.STATES[sym]
    if abs(s["qty"]) < 0.000001:
        return False
    macd_reversal = (is_long and s["prev_macd_line"] > s["prev_macd_signal"] and s["macd_line"] < s["macd_signal"]) or \
                    (not is_long and s["prev_macd_line"] < s["prev_macd_signal"] and s["macd_line"] > s["macd_signal"])
    if not macd_reversal or not s.get("prev_close") or len(s["ohlcv"]) < 2:
        return False
    current_price = s["close_price"]
    from core.indicators import _get_atr
    atr_val = _get_atr(s, current_price)
    prev_bar_high = s["ohlcv"][-2][2]
    prev_bar_low = s["ohlcv"][-2][3]
    breakout_confirmed = False
    if is_long:
        breakout_confirmed = current_price < prev_bar_low and prev_bar_low - current_price > max(atr_val * 0.25, 0.001)
    else:
        breakout_confirmed = current_price > prev_bar_high and current_price - prev_bar_high > max(atr_val * 0.25, 0.001)
    reversal_settings = {**DEFAULT_REVERSAL_SETTINGS, **SYMBOL_REVERSAL_SETTINGS.get(sym, {})}
    volume_confirmed = s["current_vol"] > s["vol_ma20"] * reversal_settings["volume_multiplier"]
    trade_signal = s.get("trade_signal_strength", 0.0)
    trade_confirmed = trade_signal >= reversal_settings["trade_signal_threshold"]
    if macd_reversal and breakout_confirmed and volume_confirmed and trade_confirmed:
        return True
    return False


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


def _fill_paper_order(sym, fill_price):
    """處理 paper 模式的待成交限價單：成交後更新倉位狀態"""
    s = ctx.STATES[sym]
    pk = paper_key(sym)
    order = s.get("pending_paper_order")
    if not order:
        return
    if not fill_price or fill_price <= 0:
        logger.info(f"[REJECT_PAPER] {sym} _fill_paper_order fill_price=0，已攔截撤單")
        s["pending_paper_order"] = None
        return
    side = order["side"]
    base_amt = order["qty"]
    margin = order["margin"]
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
        s["last_buy_time"] = now
        s["last_entry_time"] = now
        s["last_entry_price"] = fill_price
        s["last_entry_direction"] = side
        s["entry_count"] += 1
        if s["entry_count"] == 1:
            s["is_breakeven_locked"] = False
            s["highest_profit_pct"] = 0.0
            s["first_entry_price"] = fill_price
        _import_update_trailing_stop()(sym, fill_price, side == 'buy')
        if s["entry_count"] >= 2:
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
    if elapsed > order["timeout"]:
        s["pending_paper_order"] = None
        logger.info(f"⌛ [Paper超時撤單] {sym} {side} @ {limit_price:.6f} 超過 {order['timeout']}秒未成交，已撤單")
        return
    filled = (side == 'buy' and p <= limit_price) or (side == 'sell' and p >= limit_price)
    if filled:
        _fill_paper_order(sym, limit_price)
        return


async def execute_order(sym, side, price, allocation_pct=0.33, is_rescue_dca=False):
    import numpy as np  # 強制防禦局部變量失效漏洞
    s = ctx.STATES[sym]

    # 進場方向與當前持倉衝突防護
    # 如果已有持倉，且新進場方向與舊持倉方向相反，除非是救援 DCA，否則直接攔截
    _existing_qty = s.get("qty", 0.0)
    _has_position = abs(_existing_qty) > 0.000001
    if _has_position:
        _current_direction = "buy" if _existing_qty > 0 else "sell"
        if side != _current_direction and not is_rescue_dca:
            logger.info(f"🛑 [Direction_Conflict] {sym} 已有 {_current_direction} 持倉 (qty={_existing_qty:.4f})，禁止發出 {side} 進場指令 (非救援模式)")
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

    if not is_rescue_dca:
        try:
            orderbook = await exchange_futures.fetch_order_book(sym, limit=20)
            bids = sum(x[1] for x in orderbook.get('bids', []))
            asks = sum(x[1] for x in orderbook.get('asks', []))
            _s = ctx.STATES.get(sym, {})
            _atr_hist_of = _s.get("atr_history", [])
            _atr_avg_of = float(np.mean(_atr_hist_of)) if len(_atr_hist_of) > 0 else 0.0
            _atr_cur_of = _s.get("current_atr", 0.0)
            _is_low_vol_of = (_atr_avg_of > 0 and _atr_cur_of <= _atr_avg_of)
            _flow_threshold = 0.75 if _is_low_vol_of else 0.80
            _flow_label = f"低波動放寬 {_flow_threshold}" if _is_low_vol_of else f"高波動嚴格 {_flow_threshold}"
            if side == 'buy':
                if asks == 0 or bids / asks < _flow_threshold:
                    logger.info(f"🛑 [Filter:OrderFlow] {sym} 買盤支撐不足 (BidVol: {bids:.2f} / AskVol: {asks:.2f} < {_flow_threshold} | {_flow_label})，疑似假突破，拒絕做多！")
                    return
            else:
                if bids == 0 or asks / bids < _flow_threshold:
                    logger.info(f"🛑 [Filter:OrderFlow] {sym} 賣盤壓力不足 (AskVol: {asks:.2f} / BidVol: {bids:.2f} < {_flow_threshold} | {_flow_label})，疑似假跌破，拒絕做空！")
                    return
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
        return

    try:
        ticker = await exchange_futures.fetch_ticker(sym)
        market_price = float(ticker.get('last') or 0)
    except Exception as e:
        market_price = 0.0
        logger.info(f"⚠️ [價格偏離檢查] {sym} fetch_ticker 失敗: {e}")

    if market_price <= 0:
        # fetch_ticker 失敗時回落到即時交易流價格（獨立數據源，不依賴 OHLCV）
        market_price = float(s.get("last_trade_price", 0.0) or 0)

    if market_price > 0:
        deviation = abs(price - market_price) / market_price
        if deviation > 0.05:
            logger.info(f"🚨 [風控] {sym} 訂單價格 {price:.6f} 偏離市場參照價 {market_price:.6f} ({deviation*100:.2f}%)，已攔截異常訂單！")
            # 順帶修正被污染的 close_price，避免後續繼續使用錯誤值
            s["close_price"] = market_price
            return
    else:
        # 完全無法取得市場價格，保守拒絕
        logger.info(f"🚨 [風控] {sym} 無法取得市場參照價 (ticker失敗且無即時交易紀錄)，為安全起見拒絕執行 (price={price:.6f})")
        return

    now = time.time()
    if s["entry_count"] > 0 and not is_rescue_dca:
        logger.info(f"🛑 [加倉停用] {sym} 金字塔順勢加碼功能已完全停用，拒絕加倉！")
        return

    base_notional = margin * DUAL_SHOT_LEVERAGE

    if base_notional < 10.0 and margin * DUAL_SHOT_LEVERAGE >= 10.0:
        base_notional = 10.0

    balance = get_balance()
    required_margin = base_notional / DUAL_SHOT_LEVERAGE

    if not PAPER_TRADING:
        try:
            bal = await exchange_futures.fetch_balance()
            total_usdt = float(bal.get("USDT", {}).get("total", balance))
            free_usdt = float(bal.get("USDT", {}).get("free", 0.0))
            logger.info(
                f"🔥 [重裝雙發進場] {sym} 倉位計算中...\n"
                f"   ➔ 當前錢包總權益 (total): {total_usdt:.4f} USDT\n"
                f"   ➔ 單筆核配保證金 (= total/2): {required_margin:.4f} USDT\n"
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
        return

    if PAPER_TRADING:
        try:
            current_market_price = s.get("close_price", price)
            if ENTRY_ORDER_MODE == 'market':
                _fill_paper_order(sym, current_market_price)
                logger.info(f"✅ [Paper市價成交] {sym} {side} {base_amt:.4f} @ {current_market_price:.6f}")
                return
            elif ENTRY_ORDER_MODE == 'chase':
                fill_price = current_market_price * (1 + ENTRY_CHASE_OFFSET_PCT) if side == 'buy' else current_market_price * (1 - ENTRY_CHASE_OFFSET_PCT)
                _fill_paper_order(sym, fill_price)
                logger.info(f"✅ [Paper追價成交] {sym} {side} {base_amt:.4f} @ {fill_price:.6f}")
                return
            elif ENTRY_ORDER_MODE == 'pullback':
                atr = s.get("current_atr", 0.0)
                if atr <= 0:
                    atr = current_market_price * 0.015
                # 高波動幣（ATR > 0.8%）用 1.5 倍回踩深度，確保買在更低點
                _atr_pct = atr / current_market_price if current_market_price > 0 else 0.015
                _pb_mult = ENTRY_PULLBACK_ATR_MULT * (1.5 if _atr_pct > 0.008 else 1.0)
                
                if side == 'buy':
                    limit_price = current_market_price - atr * _pb_mult
                    # 參考近期K線低點，確保買在相對低點
                    if len(s.get("ohlcv", [])) >= 2:
                        recent_low = min(s["ohlcv"][-1][3], s["ohlcv"][-2][3])
                        # 不要低得太誇張，最多抓到 atr 3倍的回踩深度
                        limit_price = max(recent_low, current_market_price - atr * (_pb_mult * 3))
                        limit_price = min(limit_price, current_market_price - atr * _pb_mult)
                else:
                    limit_price = current_market_price + atr * _pb_mult
                    # 參考近期K線高點，確保賣在相對高點
                    if len(s.get("ohlcv", [])) >= 2:
                        recent_high = max(s["ohlcv"][-1][2], s["ohlcv"][-2][2])
                        limit_price = min(recent_high, current_market_price + atr * (_pb_mult * 3))
                        limit_price = max(limit_price, current_market_price + atr * _pb_mult)
                s["pending_paper_order"] = {
                    "side": side, "limit_price": limit_price, "qty": base_amt,
                    "margin": margin, "placed_at": now, "timeout": DUAL_SHOT_ORDER_TIMEOUT,
                }
                direction = "做多" if side == 'buy' else "做空"
                logger.info(f"⏳ [Paper回踩掛單] {sym} {direction} {base_amt:.4f} @ {limit_price:.6f} (當前: {current_market_price:.6f}, ATR%:{_atr_pct*100:.2f}%, 深度:{_pb_mult:.2f}×ATR)")
                return
            else:
                spread_pct = 0.0003
                limit_price = current_market_price * (1 - spread_pct) if side == 'buy' else current_market_price * (1 + spread_pct)
                s["pending_paper_order"] = {
                    "side": side, "limit_price": limit_price, "qty": base_amt,
                    "margin": margin, "placed_at": now, "timeout": DUAL_SHOT_ORDER_TIMEOUT,
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
            try:
                ob = await exchange_futures.fetch_order_book(sym, limit=5)
                asks = ob.get('asks', [])
                bids = ob.get('bids', [])
                ask1 = float(asks[0][0]) if asks else price
                bid1 = float(bids[0][0]) if bids else price
                
                prec = await get_contract_precision(sym)
                tick_size = prec['tick_size']

                if ENTRY_ORDER_MODE == 'market':
                    order_type = 'market'
                    limit_price = None
                    logger.info(f"📌 [市價下單] {sym} 執行市價進場")
                elif ENTRY_ORDER_MODE == 'chase':
                    limit_price = ask1 if side == 'buy' else bid1
                    if side == 'buy':
                        limit_price = limit_price * (1 + ENTRY_CHASE_OFFSET_PCT)
                    else:
                        limit_price = limit_price * (1 - ENTRY_CHASE_OFFSET_PCT)
                    limit_price = round_step(limit_price, tick_size)
                    logger.info(f"📌 [追價掛單] {sym} 掛對手價 {limit_price:.6f} 確保成交")
                elif ENTRY_ORDER_MODE == 'pullback':
                    atr = s.get("current_atr", 0.0)
                    if atr <= 0:
                        atr = price * 0.015
                    # 高波動幣（ATR > 0.8%）用 1.5 倍回踩深度，確保買在更低點
                    _atr_pct = atr / price if price > 0 else 0.015
                    _pb_mult = ENTRY_PULLBACK_ATR_MULT * (1.5 if _atr_pct > 0.008 else 1.0)
                    
                    if side == 'buy':
                        limit_price = price - atr * _pb_mult
                        if len(s.get("ohlcv", [])) >= 2:
                            recent_low = min(s["ohlcv"][-1][3], s["ohlcv"][-2][3])
                            limit_price = max(recent_low, price - atr * (_pb_mult * 3))
                            limit_price = min(limit_price, price - atr * _pb_mult)
                    else:
                        limit_price = price + atr * _pb_mult
                        if len(s.get("ohlcv", [])) >= 2:
                            recent_high = max(s["ohlcv"][-1][2], s["ohlcv"][-2][2])
                            limit_price = min(recent_high, price + atr * (_pb_mult * 3))
                            limit_price = max(limit_price, price + atr * _pb_mult)
                    limit_price = round_step(limit_price, tick_size)
                    logger.info(f"📌 [回踩掛單] {sym} 掛單價 {limit_price:.6f} (信號價: {price:.6f}, ATR%:{_atr_pct*100:.2f}%, 深度:{_pb_mult:.2f}×ATR)")
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
                if ENTRY_ORDER_MODE == 'market':
                    order_type = 'market'
                    limit_price = None

            params = {'marginMode': 'isolated', 'timeInForce': 'GTC'}
            if order_type == 'market':
                params.pop('timeInForce', None)

            order = await exchange_futures.create_order(
                sym, type=order_type, side=side, amount=abs(base_amt), price=limit_price,
                params=params
            )
            order_id = order['id']
            order_ts = time.time()

            ctx.PENDING_LIMIT_ORDERS[order_id] = {
                "sym": sym, "side": side, "qty": base_amt,
                "price": limit_price or price, "timestamp": order_ts
            }
            logger.info(f"⏳ [限價單挂出] {sym} {side} {base_amt:.4f} @ {limit_price} (ID: {order_id}, 類型: {order_type})")

            await asyncio.sleep(3)
            try:
                fetched = await exchange_futures.fetch_order(order_id, sym)
                status = fetched.get('status', '')
                filled_qty = float(fetched.get('filled', 0.0))
            except Exception:
                status = 'unknown'
                filled_qty = 0.0

            if status == 'closed' or filled_qty >= base_amt * 0.99:
                ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)
                fill_price = float(fetched.get('average') or fetched.get('price') or limit_price)
                logger.info(f"✅ [限價成交] {sym} {side} {filled_qty:.4f} @ {fill_price:.6f}")
            elif filled_qty > 0:
                fill_price = float(fetched.get('average') or limit_price)
                base_amt = filled_qty
                logger.info(f"⚠️ [部分成交] {sym} 實際成交: {filled_qty:.4f} (OK率: {filled_qty/base_amt*100:.1f}%)")
            else:
                logger.info(f"⏳ [等待成交] {sym} 限價單 {order_id} 尚未成交，由逃期止單機制接管")
                return

            old_qty = s["qty"]
            try:
                positions = await exchange_futures.fetch_positions([sym])
                actual_pos = next((p for p in positions if p.get('symbol') == sym and abs(float(p.get('contracts', 0) or 0)) > 0), None)
                if actual_pos:
                    actual_qty = float(actual_pos.get('contracts', 0) or 0)
                    actual_side_sign = 1 if side == 'buy' else -1
                    s["qty"] = actual_qty * actual_side_sign
                    logger.info(f"📊 [持倉同步] {sym} 交易所實際持倉: {s['qty']:.4f}")
            except Exception as pe:
                logger.info(f"⚠️ [持倉同步失敗] {sym}: {pe}")
                s["qty"] = old_qty

            old_qty = s["qty"]
            if side == 'buy':
                s["qty"] += base_amt
            else:
                s["qty"] -= base_amt

            slippage = abs(fill_price - price) / price if price > 0 else 0
            logger.info(f"✅ [實盤開倉成功] {sym} {side} | 信號價: {price:.6f} | 限價: {limit_price:.6f} | 實際: {fill_price:.6f} | 滑價: {slippage*100:.3f}%")

            if s["avg_price"] <= 0:
                s["avg_price"] = fill_price
                s["entry_atr"] = max(s.get("current_atr", 0.0), fill_price * 0.005)
            else:
                old_abs_qty = abs(old_qty) if 'old_qty' in locals() else 0.0
                s["avg_price"] = ((s["avg_price"] * old_abs_qty) + (fill_price * base_amt)) / abs(s["qty"])

            if "entries" not in s:
                s["entries"] = []
            s["entries"].append({"price": fill_price, "qty": base_amt, "time": now, "side": side})

            s["open_time"] = now
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["last_entry_price"] = fill_price
            s["last_entry_direction"] = side
            s["entry_count"] += 1

            if s["entry_count"] == 1:
                s["is_breakeven_locked"] = False
                s["highest_profit_pct"] = 0.0
                s["first_entry_price"] = fill_price

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
                stop_side = 'sell' if s["qty"] > 0 else 'buy'
                hard_sl_pct = s.get("hard_stop_loss_pct", 0.02)
                stop_price = s["avg_price"] * (1 - hard_sl_pct) if s["qty"] > 0 else s["avg_price"] * (1 + hard_sl_pct)
                prec = await get_contract_precision(sym)
                stop_price = round_step(stop_price, prec['tick_size'])

                if s.get("exchange_stop_order_id"):
                    try:
                        await exchange_futures.cancel_order(s["exchange_stop_order_id"], sym)
                    except Exception as ce:
                        logger.info(f"⚠️ [取消舊止損單失敗] {sym}: {ce}")

                stop_order = await exchange_futures.create_order(
                    sym, type='STOP_MARKET', side=stop_side, amount=abs(s["qty"]),
                    params={'stopPrice': stop_price, 'reduceOnly': True}
                )
                s["exchange_stop_order_id"] = stop_order['id']
                logger.info(f"🛡️ [交易所挂單] {sym} 成功挂出 Stop Market 止損單 @ {stop_price} (數量: {abs(s['qty'])})")
            except Exception as se:
                logger.info(f"🚨 [交易所止損挂單失敗] {sym}: {se}")

        except Exception as e:
            logger.info(f"🚨 [開倉錯誤] {sym}: {e}")


async def check_stale_limit_orders():
    """
    超時撤單機制 (Order Timeout Canceller)
    每 30 秒檢查一次 PENDING_LIMIT_ORDERS。
    超過 MAX_WAIT_SECONDS 仍未撮合的限價進場單自動撤銷。
    """
    MAX_WAIT_SECONDS = DUAL_SHOT_ORDER_TIMEOUT

    while True:
        await asyncio.sleep(30)
        if PAPER_TRADING:
            continue
        for order_id in list(ctx.PENDING_LIMIT_ORDERS.keys()):
            info = ctx.PENDING_LIMIT_ORDERS.get(order_id)
            if not info:
                continue
            elapsed = time.time() - info["timestamp"]
            if elapsed <= MAX_WAIT_SECONDS:
                continue

            sym = info["sym"]
            side = info.get("side", "")
            original_qty = info.get("qty", 0.0)

            cancel_ok = False
            filled_qty = 0.0
            try:
                fetched = await exchange_futures.fetch_order(order_id, sym)
                order_status = fetched.get('status', '')
                filled_qty = float(fetched.get('filled', 0.0) or 0.0)

                if order_status in ('closed', 'canceled'):
                    ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)
                    logger.info(f"ℹ️ [超時撤單] {sym} 訂單 {order_id} 已為 {order_status} 狀態，跳過撤單。")
                    continue

                await exchange_futures.cancel_order(order_id, sym)
                cancel_ok = True
                logger.info(
                    f"⏳ [超時撤單] {sym} 限價單超時未成交 "
                    f"(已掛單 {elapsed:.1f} 秒 > {MAX_WAIT_SECONDS}s)。"
                    f"為防止穿價風險，執行自動撤單！ OrderID: {order_id} "
                    f"部分成交量: {filled_qty:.4f}/{original_qty:.4f}"
                )
            except Exception as ce:
                logger.info(f"⚠️ [超時撤單失敗] {sym} {order_id}: {ce}")

            ctx.PENDING_LIMIT_ORDERS.pop(order_id, None)

            try:
                positions = await exchange_futures.fetch_positions([sym])
                actual_pos = next(
                    (p for p in positions
                     if p.get('symbol') == sym and abs(float(p.get('contracts', 0) or 0)) > 0),
                    None
                )
                s = ctx.STATES.get(sym)
                if not s:
                    continue

                if actual_pos:
                    actual_qty = float(actual_pos.get('contracts', 0) or 0)
                    side_sign = 1 if actual_pos.get('side', '') == 'long' else -1
                    s["qty"] = actual_qty * side_sign
                    logger.info(
                        f"📊 [持倉同步] {sym} 撤銷後實際持倉: {s['qty']:.4f} "
                        f"(原始預期: {original_qty:.4f})"
                    )
                else:
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
