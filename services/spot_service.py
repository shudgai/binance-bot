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
        return {"balances": {"USDT": 10000.0}, "avg_prices": {}, "history": []}
    with open(SPOT_STATE_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
    if "avg_prices" not in d:
        d["avg_prices"] = {}
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

    # Update balances
    balances[from_coin] = max(0.0, from_bal - amount)

    old_qty    = balances.get(to_coin, 0.0)
    old_avg    = avg_p.get(to_coin, 0.0)
    new_qty    = old_qty + q["to_amount"]
    # Weighted average cost
    if to_coin != "USDT" and new_qty > 0:
        avg_p[to_coin] = (old_qty * old_avg + q["to_amount"] * q["to_price_usdt"]) / new_qty
    balances[to_coin] = new_qty

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

def reset_spot(initial_usdt: float = 10000.0):
    state = {"balances": {"USDT": initial_usdt}, "avg_prices": {}, "history": []}
    _save_state(state)
    return state
