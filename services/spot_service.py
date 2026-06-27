import json
import os
import time

SPOT_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "spot_state.json")
SPOT_FEE_RATE = 0.001  # 0.1% Binance spot taker fee

SUPPORTED_COINS = [
    "USDT", "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE",
    "DOT", "LTC", "LINK", "SUI", "AVAX", "NEAR", "APT", "ARB",
    "OP", "INJ", "HYPE", "AAVE",
]

def _load_state():
    if not os.path.exists(SPOT_STATE_FILE):
        return {"balances": {"USDT": 10000.0}, "history": []}
    with open(SPOT_STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_state(state):
    with open(SPOT_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4, ensure_ascii=False)

def get_balances():
    return _load_state()["balances"]

def get_history():
    return _load_state()["history"]

def get_spot_price(coin: str, client) -> float:
    """Return USDT price for a coin using spot API."""
    if coin == "USDT":
        return 1.0
    try:
        ticker = client.get_symbol_ticker(symbol=f"{coin}USDT")
        return float(ticker["price"])
    except Exception:
        # fallback: try futures price
        try:
            ticker = client.futures_symbol_ticker(symbol=f"{coin}USDT")
            return float(ticker["price"])
        except Exception:
            return None

def get_quote(from_coin: str, to_coin: str, amount: float, client):
    from_price = get_spot_price(from_coin, client)
    to_price   = get_spot_price(to_coin, client)
    if from_price is None or to_price is None:
        return {"success": False, "error": "無法取得價格，請稍後再試"}
    usdt_value = amount * from_price
    fee_usdt   = usdt_value * SPOT_FEE_RATE
    to_amount  = (usdt_value - fee_usdt) / to_price
    return {
        "success": True,
        "from_coin": from_coin,
        "to_coin":   to_coin,
        "from_amount": amount,
        "to_amount":   to_amount,
        "from_price_usdt": from_price,
        "to_price_usdt":   to_price,
        "fee_usdt":  fee_usdt,
        "rate": from_price / to_price,
    }

def execute_convert(from_coin: str, to_coin: str, amount: float, client):
    state    = _load_state()
    balances = state["balances"]

    from_balance = balances.get(from_coin, 0.0)
    if amount <= 0:
        return {"success": False, "error": "金額必須大於 0"}
    if from_balance < amount:
        return {"success": False, "error": f"餘額不足：{from_coin} 現有 {from_balance:.6f}，需要 {amount:.6f}"}

    q = get_quote(from_coin, to_coin, amount, client)
    if not q["success"]:
        return q

    balances[from_coin] = from_balance - amount
    balances[to_coin]   = balances.get(to_coin, 0.0) + q["to_amount"]

    record = {
        "time":            int(time.time() * 1000),
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
    state["history"] = state["history"][:200]
    _save_state(state)
    return {"success": True, "trade": record, "balances": balances}

def reset_spot(initial_usdt: float = 10000.0):
    state = {"balances": {"USDT": initial_usdt}, "history": []}
    _save_state(state)
    return state
