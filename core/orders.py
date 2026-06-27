import asyncio
import time
import json
import os
from datetime import datetime

from core import ctx
from core.config import (PAPER_TRADING, TRADE_HISTORY_FILE, DUAL_SHOT_ORDER_TIMEOUT,
    DUAL_SHOT_LEVERAGE, COIN_PROFILE_CONFIG, HARD_STOP_LOSS_PCT, DUAL_SHOT_MAX_SLOTS,
    DEFAULT_REVERSAL_SETTINGS, SYMBOL_REVERSAL_SETTINGS)
from core.exchange_client import exchange_futures, sanitize_order_qty, get_contract_precision, round_step, convert_to_ccxt_symbol
from core.balance import get_balance, compute_per_coin_margin, accrue_daily_realized_pnl
import core.balance as _bal
from core.state_manager import mark_exit, reset_coin_state, build_symbol_state
from core.symbol_profile import apply_symbol_profile, SYMBOL_PROFILES
from core.config import get_symbol_leverage
from services.utils import paper_key
from update_paper_state import update_paper_state
from ai_manager import ai_engine


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
        print(f"📝 [AI Memory] 已記錄 {symbol} 並產生摘要: {summary}")
    except Exception as e:
        print(f"⚠️ [AI Memory] 紀錄失敗: {e}")


async def close_position(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = ctx.STATES[sym]
    await _close_position_inner(sym, close_side, qty, price, avg_price, reason, is_stop_loss)


async def _close_position_inner(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = ctx.STATES[sym]
    s["adjusted_this_tick"] = True

    # 強化防禦：檢查實際持倉與指令方向是否衝突
    actual_qty = s.get("qty", 0)
    if abs(actual_qty) > 0.000001:
        expected_close_side = "sell" if actual_qty > 0 else "buy"
        if close_side != expected_close_side:
            print(f"🚨 [CRITICAL_ERROR] {sym} 平倉方向衝突！持倉為 {'多' if actual_qty > 0 else '空'}，但指令要求 {close_side}。| reason={reason}")
            print(f"🔄 [CRITICAL_ERROR] {sym} 正在自動修正指令為 {expected_close_side} 以確保正確平倉。")
            close_side = expected_close_side  # 強制修正方向

    if not price or price <= 0:
        price = s.get("close_price", 0.0) or s.get("avg_price", 0.0)
        if price <= 0:
            print(f"[REJECT_ZERO_PRICE] {sym} 平倉價格為 0，已攔截！")
            return
        print(f"[WARN_ZERO_PRICE] {sym} 平倉價格補救為 {price:.6f}")
    if abs(s["qty"]) < 0.000001:
        return
    pk = paper_key(sym)
    qty = min(abs(qty), abs(s["qty"]))
    if qty < 0.000001:
        return

    real_avg = s["avg_price"] if s["avg_price"] > 0 else avg_price
    profit_pct = (price - real_avg) / real_avg if s["qty"] > 0 else (real_avg - price) / real_avg

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

    if profit_pct < -0.002:
        if close_side == "sell":
            s["last_loss_time_long"] = time.time()
        else:
            s["last_loss_time_short"] = time.time()

    full_reason = f"{pnl_tag} {reason}".strip()
    s["last_exit_time"] = time.time()

    sanitized_qty = await sanitize_order_qty(sym, qty)
    if sanitized_qty <= 0.0:
        print(f"⚠️ [平倉風控] {sym} 無法取得有效數量 ({qty:.6f})")
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
            await exchange_futures.create_order(sym, type="limit", side=close_side, amount=qty,
                                        params={"reduceOnly": True, "marginMode": "isolated"})
        except Exception as e:
            print(f"🚨 [平倉錯誤] {sym}: {e}")
            return

    record_trade_result(
        symbol=sym,
        entry_reason=s.get("entry_reason", "UNKNOWN"),
        exit_reason=full_reason,
        profit_pct=profit_pct,
        current_atr=s.get("current_atr", 0.0),
        max_profit_reached=s.get("max_profit", 0.0),
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
            print(f"[每日熔斷追蹤] {sym} 虧損 {profit_pct*100:.2f}% | 今日累計: {_bal._DAILY_REALIZED_LOSS*100:.2f}% / {DAILY_LOSS_LIMIT_PCT*100:.1f}%")
    except Exception as _e:
        print(f"[每日熔斷追蹤失敗] {_e}")

    remaining = abs(s["qty"]) - qty
    if remaining < 0.01:
        if remaining > 0.000001:
            print(f"🧹 [塵埃清理] {sym} 剩餘 {remaining:.6f} 視為已清")
        if s.get("exchange_stop_order_id") and not PAPER_TRADING:
            try:
                await exchange_futures.cancel_order(s["exchange_stop_order_id"], sym)
                print(f"✅ [止損單取消] {sym} 部位已全平，撤銷交易所止損單")
            except Exception as ce:
                print(f"⚠️ [取消止損單失敗] {sym}: {ce}")

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

        print(f"✅ [部分平] {sym} 平{qty} 剩{abs(s['qty']):.4f} {full_reason}")

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
                print(f"🛡️ [止損單更新] {sym} 部分平倉後更新止損單 @ {stop_price} (數量: {abs(s['qty'])})")
            except Exception as ce:
                print(f"⚠️ [更新止損單失敗] {sym}: {ce}")


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
    print("🚨🚨 [緊急清倉] 開始強制市價平掉所有倉位！")
    for sym in ctx.ALL_SYMBOLS:
        s = ctx.STATES[sym]
        if abs(s["qty"]) > 0.000001:
            is_long = s["qty"] > 0
            cs = 'sell' if is_long else 'buy'
            p = s.get("close_price", s["avg_price"])
            print(f"🚨 [緊急清倉] 正在平倉 {sym}...")
            try:
                await close_position(sym, cs, abs(s["qty"]), p, s["avg_price"], reason="[GLOBAL_MELTDOWN]", is_stop_loss=True)
            except Exception as e:
                print(f"⚠️ [緊急清倉失敗] {sym}: {e}")


def get_total_wallet_balance():
    if PAPER_TRADING:
        try:
            with open("paper_state.json", 'r') as f:
                st = json.load(f)
                realized = sum(v.get('realized_pnl', 0.0) for v in st.get('positions', {}).values())
                return 1500.0 + realized
        except:
            return 1500.0
    else:
        return 1500.0


def check_total_equity_protection():
    total_unrealized_pnl = 0.0
    has_positions = False

    for sym in ctx.ALL_SYMBOLS:
        s = ctx.STATES[sym]
        qty = s.get("qty", 0.0)
        if abs(qty) > 0.000001:
            has_positions = True
            p = s.get("close_price", s.get("avg_price", 0.0))
            avg = s.get("avg_price", 0.0)
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
    GLOBAL_LOSS_THRESHOLD = -4.0

    if loss_percentage <= GLOBAL_LOSS_THRESHOLD:
        print(f"\n🚨🚨🚨 [全局風控熔斷] 警告！當前總未實現虧損已達 {loss_percentage:.2f}%")
        print(f"🛑 超過安全防線 {GLOBAL_LOSS_THRESHOLD}%！觸發系統緊急黑天鵝熔斷機制...")
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
        print(f"[REJECT_PAPER] {sym} _fill_paper_order fill_price=0，已攔截撤單")
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
        print(f"✅ [Paper成交] {sym} {direction} {base_amt:.4f} @ {fill_price:.6f} (保證金:{margin:.2f} USDT)")
    except Exception as e:
        print(f"🛑 [Paper成交失敗] {sym}: {e}")
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
        print(f"⌛ [Paper超時撤單] {sym} {side} @ {limit_price:.6f} 超過 {order['timeout']}秒未成交，已撤單")
        return
    filled = (side == 'buy' and p >= limit_price) or (side == 'sell' and p <= limit_price)
    if filled:
        _fill_paper_order(sym, limit_price)


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
            print(f"🛑 [Direction_Conflict] {sym} 已有 {_current_direction} 持倉 (qty={_existing_qty:.4f})，禁止發出 {side} 進場指令 (非救援模式)")
            print(f"🛑 [Direction_Conflict] {sym} 若要反手，請先透過 close_position 平倉後再進場，避免方向衝突！")
            return

    if not price or price <= 0:
        fallback = s.get("close_price", 0.0) or s.get("avg_price", 0.0)
        if fallback <= 0:
            print(f"[REJECT_ZERO_PRICE] {sym} execute_order price=0 且無法補救，已攔截！")
            return
        print(f"[WARN_ZERO_PRICE] {sym} execute_order price=0，補救為 {fallback:.6f}")
        price = fallback
    pk = paper_key(sym)
    lev = get_symbol_leverage(sym)
    s["leverage"] = lev
    print(f"@@LEVERAGE@@{lev}")

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
            _flow_threshold = 0.85 if _is_low_vol_of else 0.95
            _flow_label = f"低波動放寬 {_flow_threshold}" if _is_low_vol_of else f"高波動嚴格 {_flow_threshold}"
            if side == 'buy':
                if asks == 0 or bids / asks < _flow_threshold:
                    print(f"🛑 [Filter:OrderFlow] {sym} 買盤支撐不足 (BidVol: {bids:.2f} / AskVol: {asks:.2f} < {_flow_threshold} | {_flow_label})，疑似假突破，拒絕做多！")
                    return
            else:
                if bids == 0 or asks / bids < _flow_threshold:
                    print(f"🛑 [Filter:OrderFlow] {sym} 賣盤壓力不足 (AskVol: {asks:.2f} / BidVol: {bids:.2f} < {_flow_threshold} | {_flow_label})，疑似假跌破，拒絕做空！")
                    return
        except Exception as e:
            print(f"⚠️ [OrderFlow] 讀取掛單簿失敗 {sym}: {e}")
    if not PAPER_TRADING:
        try:
            await exchange_futures.set_leverage(lev, convert_to_ccxt_symbol(sym))
        except Exception as e:
            print(f"⚠️ [槓桿設定失敗] {sym}: {e}")

    margin = compute_per_coin_margin(sym, allocation_pct)

    if margin <= 0:
        print(f"⚠️ [風控] {sym} 無可用保證金")
        return

    try:
        ticker = await exchange_futures.fetch_ticker(sym)
        market_price = ticker.get('last')
        if market_price and market_price > 0:
            deviation = abs(price - market_price) / market_price
            if deviation > 0.05:
                print(f"🚨 [風控] {sym} 訂單價格 {price} 嚴重偏離市價 {market_price} (偏離 {deviation*100:.2f}%)，拒絕執行！")
                return
    except Exception as e:
        print(f"⚠️ [價格偏離檢查失敗] {e}")

    now = time.time()
    if s["entry_count"] > 0 and not is_rescue_dca:
        if now - s["last_entry_time"] < s["entry_cooldown_sec"]:
            print(f"⏳ [加倉冷卻] {sym} 距離上次加倉不足 {s['entry_cooldown_sec']} 秒")
            return
        if s["entry_count"] >= 3:
            print(f"⚠️ [加倉上限] {sym} 已達絕對層數上限 (3層)")
            return

        avg_price = s.get("avg_price", 0.0)
        if avg_price > 0:
            profit_pct = (price - avg_price) / avg_price if side == 'buy' else (avg_price - price) / avg_price
            if profit_pct < 0.015:
                print(f"🛑 [金字塔防護] {sym} 目前利潤 {profit_pct*100:.2f}% 未達安全門檻 1.5%，拒絕加倉以防拉高成本！")
                return

        last_entry_price = s.get("last_entry_price", avg_price)
        if last_entry_price > 0:
            reversal = (last_entry_price - price) / last_entry_price if side == 'buy' else (price - last_entry_price) / last_entry_price
            if reversal > 0.01:
                print(f"🛑 [反轉過濾] {sym} 價格與上次加倉發生大幅反轉 ({reversal*100:.2f}% > 1%)，拒絕加倉！")
                return

            current_vol = s.get("current_vol", 0.0)
            vol_ma20 = s.get("vol_ma20", 1e-8)
            if current_vol < vol_ma20 * 0.6:
                print(f"🛑 [量能過濾] {sym} 當前量能低於均量 0.6 倍，動能不足拒絕加倉！")
                return

            if len(s.get("ohlcv", [])) >= 3:
                c1 = s["ohlcv"][-2]
                c2 = s["ohlcv"][-3]
                body1 = abs(c1[4] - c1[1])
                body2 = abs(c2[4] - c2[1])
                vol1 = c1[5]
                vol2 = c2[5]

                is_bull1 = c1[4] > c1[1]
                is_bull2 = c2[4] > c2[1]

                from core.indicators import calculate_macd
                macd_hist = s.get("macd_hist", 0.0)
                prev_macd_hist = 0.0
                if len(s.get("ohlcv", [])) >= 34:
                    try:
                        closes = np.array([x[4] for x in s["ohlcv"]])
                        _, _, m_hist, p_line, p_sig = calculate_macd(closes)
                        macd_hist = m_hist
                        prev_macd_hist = p_line - p_sig
                    except:
                        pass

                rsi = s.get("current_rsi", 50.0)
                is_strong_long = rsi > 70 and macd_hist > 0 and macd_hist > prev_macd_hist
                is_strong_short = rsi < 30 and macd_hist < 0 and macd_hist < prev_macd_hist

                if side == 'buy' and is_bull1 and is_bull2 and body1 < body2 * 0.8 and vol1 < vol2 * 0.8:
                    if is_strong_long:
                        print(f"@@COIN_DEBUG@@ ⚡ [斜率過濾] {sym} 強勢突破中，忽略實體與量能衰減")
                    else:
                        print(f"🛑 [斜率過濾] {sym} 價格創高但實體與量能雙雙衰減，動能不足拒絕加碼！")
                        return
                if side == 'sell' and not is_bull1 and not is_bull2 and body1 < body2 * 0.8 and vol1 < vol2 * 0.8:
                    if is_strong_short:
                        print(f"@@COIN_DEBUG@@ ⚡ [斜率過濾] {sym} 強勢跌破中，忽略實體與量能衰減")
                    else:
                        print(f"🛑 [斜率過濾] {sym} 價格創低但實體與量能雙雙衰減，動能不足拒絕加碼！")
                        return

            if len(s.get("ohlcv", [])) >= 2:
                current_close = s["ohlcv"][-1][4]
                prev_close = s["ohlcv"][-2][4]
                if side == 'buy' and current_close <= prev_close:
                    print(f"🛑 [方向確認] {sym} 多單加倉失敗，當前收盤價未高於前K線，拒絕接刀！")
                    return
                if side == 'sell' and current_close >= prev_close:
                    print(f"🛑 [方向確認] {sym} 空單加倉失敗，當前收盤價未低於前K線，拒絕接刀！")
                    return

        current_atr = s.get("current_atr", 0.0)
        last_entry_price = s.get("last_entry_price", s.get("avg_price", 0.0))
        if last_entry_price > 0 and current_atr > 0:
            price_diff = abs(price - last_entry_price)
        personality = s.get("personality", "balanced")
        profile_type = COIN_PROFILE_CONFIG.get(sym, {}).get("profile_type", "")
        if profile_type in ["Core_Trend", "High_Beta_Momentum"]:
            space_threshold = 0.8 * current_atr
        else:
            space_threshold = 1.0 * current_atr

        if price_diff < max(space_threshold, price * 0.005):
                print(f"🛑 [空間關卡] {sym} 加倉距離不足! 差距: {price_diff:.4f} < 門檻: {max(space_threshold, price * 0.005):.4f}")
                return

        from core.indicators import _macd_vals
        macd_hist, prev_macd_hist = _macd_vals(s)

        rsi = s.get("current_rsi", 50.0)
        is_strong_momentum_long = (side == 'buy' and rsi > 75 and macd_hist > 0 and macd_hist > prev_macd_hist)
        is_strong_momentum_short = (side == 'sell' and rsi < 25 and macd_hist < 0 and macd_hist < prev_macd_hist)

        if is_strong_momentum_long or is_strong_momentum_short:
            print(f"@@COIN_DEBUG@@ 🚀 [強勢豁免] {sym} RSI與MACD動能極強，豁免量能過濾直接加倉！")
        else:
            from core.entry_filter import is_entry_volume_confirmed
            if not is_entry_volume_confirmed(sym, side):
                print(f"🛑 [動能關卡] {sym} 量能不足以支持加倉!")
                return

        if (side == 'buy' and macd_hist <= 0) or (side == 'sell' and macd_hist >= 0):
            print(f"🛑 [動能關卡] {sym} MACD動能不一致 (Hist: {macd_hist:.4f})，拒絕加倉!")
            return

        if abs(macd_hist) <= abs(prev_macd_hist):
            print(f"🛑 [動能關卡] {sym} MACD動能未擴張 (Hist: {abs(macd_hist):.5f} <= Prev: {abs(prev_macd_hist):.5f})，拒絕加倉!")
            return

        if s["avg_price"] > 0 and s["close_price"] > 0:
            profit_pct = (s["close_price"] - s["avg_price"]) / s["avg_price"] if side == 'buy' else (s["avg_price"] - s["close_price"]) / s["avg_price"]
            if profit_pct < 0.001:
                print(f"🛑 [保本關卡] {sym} 目前尚未回到保本線以上，不加倉 (利潤: {profit_pct*100:.2f}%)")
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
            print(
                f"🔥 [重裝雙發進場] {sym} 倉位計算中...\n"
                f"   ➔ 當前錢包總權益 (total): {total_usdt:.4f} USDT\n"
                f"   ➔ 單筆核配保證金 (= total/2): {required_margin:.4f} USDT\n"
                f"   ➔ {DUAL_SHOT_LEVERAGE}倍槓桿發射價值: {base_notional:.2f} USDT (名義合約大小)\n"
                f"   ➔ 當前可用餘額 (free): {free_usdt:.4f} USDT"
            )
            if required_margin > free_usdt and free_usdt > 0:
                print(f"⚠️ [資金關卡] {sym} 可用餘額 {free_usdt:.2f} < 所需保證金 {required_margin:.2f}，調整為可用餘額下單！")
                base_notional = free_usdt * DUAL_SHOT_LEVERAGE
        except Exception as e:
            print(f"⚠️ [餘額檢查失敗] {e}")
    else:
        print(
            f"🔥 [重裝雙發進場-Paper] {sym}\n"
            f"   ➔ 模擬錢包總權益: {balance:.4f} USDT\n"
            f"   ➔ 單筆核配保證金: {required_margin:.4f} USDT (= total/2)\n"
            f"   ➔ {DUAL_SHOT_LEVERAGE}倍槓桿發射價值: {base_notional:.2f} USDT"
        )
        if required_margin > balance * 0.98:
            base_notional = (balance * 0.98) * DUAL_SHOT_LEVERAGE

    base_amt = base_notional / price
    base_amt = await sanitize_order_qty(sym, base_amt)

    actual_notional = base_amt * price
    if actual_notional < 6.0 and actual_notional > 0:
        min_qty = 6.0 / price
        min_qty = await sanitize_order_qty(sym, min_qty)
        if (min_qty * price) / lev > balance * 0.98:
            print(f"⚠️ [風控] {sym} 資金不足以達到最小開倉額度 6 USDT (餘額: {balance:.2f})")
            return
        base_amt = min_qty
        actual_notional = base_amt * price

    if base_amt <= 0.0:
        print(f"⚠️ [風控] {sym} 計算後開倉數量為 0")
        return

    if PAPER_TRADING:
        try:
            spread_pct = 0.0003
            limit_price = price * (1 + spread_pct) if side == 'buy' else price * (1 - spread_pct)
            s["pending_paper_order"] = {
                "side": side, "limit_price": limit_price, "qty": base_amt,
                "margin": margin, "placed_at": now, "timeout": DUAL_SHOT_ORDER_TIMEOUT,
            }
            direction = "做多" if side == 'buy' else "做空"
            print(f"⏳ [Paper限價掛出] {sym} {direction} {base_amt:.4f} @ {limit_price:.6f} (等待最多{DUAL_SHOT_ORDER_TIMEOUT}秒成交)")
        except Exception as e:
            print(f"🛑 [模擬掛單失敗] {sym}: {e}")
    else:
        try:
            try:
                ob = await exchange_futures.fetch_order_book(sym, limit=5)
                asks = ob.get('asks', [])
                bids = ob.get('bids', [])
                ask1 = float(asks[0][0]) if asks else price
                bid1 = float(bids[0][0]) if bids else price

                if side == 'buy':
                    limit_price = ask1 if ask1 > price else price
                    order_type = 'STOP_MARKET' if limit_price > price else 'limit'
                    print(f"📌 [Right-Side Stop] {sym} 順勢多單，設定 {order_type} @ {limit_price:.6f}")
                else:
                    limit_price = bid1 if bid1 < price else price
                    order_type = 'STOP_MARKET' if limit_price < price else 'limit'
                    print(f"📌 [Right-Side Stop] {sym} 順勢空單，設定 {order_type} @ {limit_price:.6f}")
            except Exception:
                limit_price = price
                order_type = 'limit'

            params = {'marginMode': 'isolated', 'timeInForce': 'GTC'}
            if order_type == 'STOP_MARKET':
                params['stopPrice'] = limit_price
                params.pop('timeInForce', None)

            order = await exchange_futures.create_order(
                sym, type=order_type, side=side, amount=abs(base_amt), price=limit_price if order_type != 'STOP_MARKET' else None,
                params=params
            )
            order_id = order['id']
            order_ts = time.time()

            ctx.PENDING_LIMIT_ORDERS[order_id] = {
                "sym": sym, "side": side, "qty": base_amt,
                "price": limit_price, "timestamp": order_ts
            }
            print(f"⏳ [限價單挂出] {sym} {side} {base_amt:.4f} @ {limit_price:.6f} (ID: {order_id})")

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
                print(f"✅ [限價成交] {sym} {side} {filled_qty:.4f} @ {fill_price:.6f}")
            elif filled_qty > 0:
                fill_price = float(fetched.get('average') or limit_price)
                base_amt = filled_qty
                print(f"⚠️ [部分成交] {sym} 實際成交: {filled_qty:.4f} (OK率: {filled_qty/base_amt*100:.1f}%)")
            else:
                print(f"⏳ [等待成交] {sym} 限價單 {order_id} 尚未成交，由逃期止單機制接管")
                return

            try:
                positions = await exchange_futures.fetch_positions([sym])
                actual_pos = next((p for p in positions if p.get('symbol') == sym and abs(float(p.get('contracts', 0) or 0)) > 0), None)
                if actual_pos:
                    actual_qty = float(actual_pos.get('contracts', 0) or 0)
                    actual_side_sign = 1 if side == 'buy' else -1
                    s["qty"] = actual_qty * actual_side_sign
                    print(f"📊 [持倉同步] {sym} 交易所實際持倉: {s['qty']:.4f}")
                else:
                    old_qty = s["qty"]
                    if side == 'buy':
                        s["qty"] += base_amt
                    else:
                        s["qty"] -= base_amt
            except Exception as pe:
                print(f"⚠️ [持倉同步失敗] {sym}: {pe}")
                old_qty = s["qty"]
                if side == 'buy':
                    s["qty"] += base_amt
                else:
                    s["qty"] -= base_amt

            slippage = abs(fill_price - price) / price if price > 0 else 0
            print(f"✅ [實盤開倉成功] {sym} {side} | 信號價: {price:.6f} | 限價: {limit_price:.6f} | 實際: {fill_price:.6f} | 滑價: {slippage*100:.3f}%")

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
                        print(f"⚠️ [取消舊止損單失敗] {sym}: {ce}")

                stop_order = await exchange_futures.create_order(
                    sym, type='STOP_MARKET', side=stop_side, amount=abs(s["qty"]),
                    params={'stopPrice': stop_price, 'reduceOnly': True}
                )
                s["exchange_stop_order_id"] = stop_order['id']
                print(f"🛡️ [交易所挂單] {sym} 成功挂出 Stop Market 止損單 @ {stop_price} (數量: {abs(s['qty'])})")
            except Exception as se:
                print(f"🚨 [交易所止損挂單失敗] {sym}: {se}")

        except Exception as e:
            print(f"🚨 [開倉錯誤] {sym}: {e}")


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
                    print(f"ℹ️ [超時撤單] {sym} 訂單 {order_id} 已為 {order_status} 狀態，跳過撤單。")
                    continue

                await exchange_futures.cancel_order(order_id, sym)
                cancel_ok = True
                print(
                    f"⏳ [超時撤單] {sym} 限價單超時未成交 "
                    f"(已掛單 {elapsed:.1f} 秒 > {MAX_WAIT_SECONDS}s)。"
                    f"為防止穿價風險，執行自動撤單！ OrderID: {order_id} "
                    f"部分成交量: {filled_qty:.4f}/{original_qty:.4f}"
                )
            except Exception as ce:
                print(f"⚠️ [超時撤單失敗] {sym} {order_id}: {ce}")

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
                    print(
                        f"📊 [持倉同步] {sym} 撤銷後實際持倉: {s['qty']:.4f} "
                        f"(原始預期: {original_qty:.4f})"
                    )
                else:
                    print(
                        f"🔄 [狀態重置] {sym} 限價單完全未成交 (filled=0)，"
                        f"撤單後清除追蹤狀態，機器人重回 ACTIVE 掃描模式。"
                    )
                    if s.get('entry_count', 0) == 0 and abs(s.get('qty', 0.0)) < 1e-6:
                        s["pending_side"] = None
                        s["pending_time"] = 0
                        s["last_entry_time"] = 0.0
                        s["status"] = "ACTIVE"

            except Exception as pe:
                print(f"⚠️ [持倉同步失敗] {sym}: {pe}")
