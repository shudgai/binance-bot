import json
import os
import time

SPOT_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "spot_state.json")
SPOT_FEE_RATE = 0.001  # 0.1%

SUPPORTED_COINS = [
    "USDT", "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE",
    "DOT", "LTC", "LINK", "SUI", "AVAX", "NEAR", "APT", "ARB",
    "OP", "INJ", "HYPE", "AAVE",
]

def _load_state():
    if not os.path.exists(SPOT_STATE_FILE):
        return {"balances": {"USDT": 10000.0}, "avg_prices": {}, "open_trades": {}, "history": []}
    with open(SPOT_STATE_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("avg_prices", {})
    d.setdefault("open_trades", {})
    return d

def _save_state(state):
    with open(SPOT_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4, ensure_ascii=False)

def get_state():
    return _load_state()

def get_balances():
    return _load_state()["balances"]

def get_history():
    return _load_state()["history"]

def get_open_trades():
    return _load_state()["open_trades"]

def get_spot_price(coin: str, client) -> float:
    if coin == "USDT":
        return 1.0
    try:
        t = client.get_symbol_ticker(symbol=f"{coin}USDT")
        return float(t["price"])
    except Exception:
        try:
            t = client.futures_symbol_ticker(symbol=f"{coin}USDT")
            return float(t["price"])
        except Exception:
            return None

def get_quote(from_coin: str, to_coin: str, amount: float, client):
    fp = get_spot_price(from_coin, client)
    tp = get_spot_price(to_coin, client)
    if fp is None or tp is None:
        return {"success": False, "error": "無法取得價格"}
    usdt_val  = amount * fp
    fee_usdt  = usdt_val * SPOT_FEE_RATE
    to_amount = (usdt_val - fee_usdt) / tp
    return {
        "success": True,
        "from_coin": from_coin, "to_coin": to_coin,
        "from_amount": amount, "to_amount": to_amount,
        "from_price_usdt": fp, "to_price_usdt": tp,
        "fee_usdt": fee_usdt, "rate": fp / tp,
    }

def execute_convert(from_coin: str, to_coin: str, amount: float, client, action: str = "convert"):
    state    = _load_state()
    balances = state["balances"]
    avg_p    = state["avg_prices"]

    from_bal = balances.get(from_coin, 0.0)
    if amount <= 0:
        return {"success": False, "error": "金額必須大於 0"}
    if from_bal < amount - 1e-9:
        return {"success": False, "error": f"{from_coin} 餘額不足 ({from_bal:.6f})"}

    q = get_quote(from_coin, to_coin, amount, client)
    if not q["success"]:
        return q

    balances[from_coin] = max(0.0, from_bal - amount)

    old_qty = balances.get(to_coin, 0.0)
    old_avg = avg_p.get(to_coin, 0.0)
    new_qty = old_qty + q["to_amount"]
    if to_coin != "USDT" and new_qty > 0:
        avg_p[to_coin] = (old_qty * old_avg + q["to_amount"] * q["to_price_usdt"]) / new_qty
    balances[to_coin] = new_qty

    # Clean up open_trades if selling fully
    if action == "sell" and from_coin != "USDT":
        if balances[from_coin] < 1e-9:
            state["open_trades"].pop(from_coin, None)
            avg_p.pop(from_coin, None)
            balances[from_coin] = 0.0

    record = {
        "time":            int(time.time() * 1000),
        "action":          action,
        "from_coin":       from_coin,
        "to_coin":         to_coin,
        "from_amount":     amount,
        "to_amount":       q["to_amount"],
        "from_price_usdt": q["from_price_usdt"],
        "to_price_usdt":   q["to_price_usdt"],
        "fee_usdt":        q["fee_usdt"],
        "rate":            q["rate"],
    }
    state["history"].insert(0, record)
    state["history"] = state["history"][:500]
    _save_state(state)
    return {"success": True, "trade": record, "balances": balances}

# ─── SL/TP 監控 ────────────────────────────────────────────

def register_open_trade(coin: str, qty: float, entry_price: float,
                        sl_pct: float, tp_pct: float, trail_pct: float):
    """Register a trade for SL/TP/trail monitoring after buy."""
    state = _load_state()
    ot    = state["open_trades"]
    if coin in ot:
        # Average into existing
        old = ot[coin]
        new_qty = old["qty"] + qty
        new_entry = (old["qty"] * old["entry_price"] + qty * entry_price) / new_qty
        ot[coin] = {
            "entry_price":  new_entry,
            "qty":          new_qty,
            "sl_price":     new_entry * (1 - sl_pct / 100),
            "tp_price":     new_entry * (1 + tp_pct / 100),
            "trail_high":   max(old.get("trail_high", new_entry), entry_price),
            "trail_active": old.get("trail_active", False),
            "trail_pct":    trail_pct,
            "open_time":    old.get("open_time", int(time.time() * 1000)),
        }
    else:
        ot[coin] = {
            "entry_price":  entry_price,
            "qty":          qty,
            "sl_price":     entry_price * (1 - sl_pct / 100),
            "tp_price":     entry_price * (1 + tp_pct / 100),
            "trail_high":   entry_price,
            "trail_active": False,
            "trail_pct":    trail_pct,
            "open_time":    int(time.time() * 1000),
        }
    _save_state(state)
    return ot[coin]

def check_sltp(client) -> list:
    """Check all open trades; execute sell if SL/TP/trail triggered. Returns closed list."""
    state = _load_state()
    ot    = state["open_trades"]
    closed = []

    for coin, trade in list(ot.items()):
        price = get_spot_price(coin, client)
        if price is None:
            continue

        entry      = trade["entry_price"]
        sl_price   = trade["sl_price"]
        tp_price   = trade["tp_price"]
        trail_pct  = trade.get("trail_pct", 1.5)
        trail_high = trade.get("trail_high", entry)
        trail_act  = trade.get("trail_active", False)

        # Update trail high
        if price > trail_high:
            trade["trail_high"] = price
            trail_high = price

        # Activate trailing stop when price exceeds TP by trail_pct
        trail_activate_price = entry * (1 + trail_pct / 100)
        if not trail_act and price >= trail_activate_price:
            trade["trail_active"] = True
            trail_act = True

        reason = None

        # Trailing stop: trail_pct below the highest price reached
        if trail_act:
            trail_sl = trail_high * (1 - trail_pct / 100)
            if price <= trail_sl:
                reason = f"移動止損 ▼{trail_pct}% (觸發@{price:.4f}, 高點@{trail_high:.4f})"

        # Take profit
        if price >= tp_price:
            reason = f"止盈 🎯 (TP={tp_price:.4f}, 現價={price:.4f})"

        # Stop loss (only if trail not activated yet)
        if not trail_act and price <= sl_price:
            reason = f"止損 🛑 (SL={sl_price:.4f}, 現價={price:.4f})"

        if reason:
            qty = trade["qty"]
            result = execute_convert(coin, "USDT", qty, client, "sell")
            if result["success"]:
                pnl = (price - entry) * qty
                closed.append({
                    "coin":        coin,
                    "reason":      reason,
                    "entry_price": entry,
                    "exit_price":  price,
                    "qty":         qty,
                    "pnl":         pnl,
                })
                ot.pop(coin, None)
        else:
            ot[coin] = trade

    state["open_trades"] = ot
    _save_state(state)
    return closed

def reset_spot(initial_usdt: float = 10000.0):
    state = {"balances": {"USDT": initial_usdt}, "avg_prices": {}, "open_trades": {}, "history": []}
    _save_state(state)
    return state
