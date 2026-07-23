import json
import os
import fcntl
import threading
import time

from core.config import PAPER_STATE_FILE
_lock = threading.Lock()


def _default_state():
    return {
        "balance_usdt": 150.0,
        "session_start_balance": 150.0,
        "positions": {},
        "trades": [],
    }


def mutate_paper_state(mutator):
    """Read-modify-write paper_state.json under a process-wide file lock."""
    with _lock:
        os.makedirs(os.path.dirname(PAPER_STATE_FILE), exist_ok=True)
        with open(PAPER_STATE_FILE, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read().strip()
                state = json.loads(raw) if raw else _default_state()
                if not isinstance(state, dict):
                    state = _default_state()
                state.setdefault("positions", {})
                state.setdefault("trades", [])

                result = mutator(state)

                f.seek(0)
                f.truncate()
                json.dump(state, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
                return result
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def update_paper_state(symbol: str, side: str, price: float, qty: float, is_close: bool = False, pnl: float = 0.0):
    """
    Updates the paper trading state in paper_state.json.
    Handles both new entries and closing positions.
    qty passed in should be absolute (positive).
    """
    qty_abs = abs(qty)
    if price <= 0 or qty_abs <= 0:
        print(f"[REJECT_PAPER] {symbol} price={price}, qty={qty_abs} — 拒絕 0 元交易！")
        return

    def _mutate(state):
        positions = state["positions"]

        paper_key = symbol
        if ":USDT" not in symbol:
            paper_key = f"{symbol}:USDT"

        if is_close:
            pos = positions.get(paper_key)
            if not pos or abs(pos.get("qty", 0.0)) < 0.000001:
                print(f"[REJECT_DUP_CLOSE] {symbol} 倉位已平 (qty≈0)，忽略重複平倉記錄！")
                return

            current_pnl = pos.get("realized_pnl", 0.0)
            pos["realized_pnl"] = current_pnl + pnl

            if "entries" in pos:
                qty_to_remove = qty_abs
                while qty_to_remove > 0.000001 and len(pos["entries"]) > 0:
                    first_entry = pos["entries"][0]
                    if first_entry["qty"] <= qty_to_remove + 0.000001:
                        qty_to_remove -= first_entry["qty"]
                        pos["entries"].pop(0)
                    else:
                        first_entry["qty"] -= qty_to_remove
                        qty_to_remove = 0

            if abs(pos.get("qty", 0.0)) - qty_abs <= 0.000001:
                pos["qty"] = 0.0
                pos["entries"] = []
            else:
                signed_qty = -qty_abs if pos.get("qty", 0.0) > 0 else qty_abs
                pos["qty"] += signed_qty

            trade_entry = {
                "symbol": paper_key,
                "price": price,
                "qty": qty_abs,
                "time": int(time.time() * 1000),
                "isBuyer": (side == "buy"),
                "realized_pnl": pnl,
                "is_close": True,
            }
            fee = price * qty_abs * 0.0005
            trade_entry["fee"] = fee
            state["trades"].append(trade_entry)

            current_balance = state.get("balance_usdt", 150.0)
            state["balance_usdt"] = current_balance + pnl - fee
            return

        signed_qty = qty_abs if side == "buy" else -qty_abs

        if paper_key in positions and abs(positions[paper_key].get("qty", 0)) > 0.000001:
            old_pos = positions[paper_key]
            old_qty = old_pos.get("qty", 0)
            old_avg = old_pos.get("avg_price", 0)
            new_qty = old_qty + signed_qty

            if (old_qty > 0 and signed_qty > 0) or (old_qty < 0 and signed_qty < 0):
                new_avg = ((old_avg * abs(old_qty)) + (price * abs(signed_qty))) / abs(new_qty)
            else:
                new_avg = price if abs(new_qty) > 0.000001 else 0.0

            entries = old_pos.get("entries", [])
            entries.append({"price": price, "qty": qty_abs, "time": int(time.time() * 1000), "side": side})

            positions[paper_key] = {
                "qty": new_qty,
                "avg_price": new_avg,
                "realized_pnl": old_pos.get("realized_pnl", 0.0),
                "entries": entries,
            }
        else:
            positions[paper_key] = {
                "qty": signed_qty,
                "avg_price": price,
                "realized_pnl": positions.get(paper_key, {}).get("realized_pnl", 0.0),
                "entries": [{"price": price, "qty": qty_abs, "time": int(time.time() * 1000), "side": side}],
            }

        fee = price * qty_abs * 0.0005
        state["trades"].append({
            "symbol": paper_key,
            "price": price,
            "qty": qty_abs,
            "time": int(time.time() * 1000),
            "isBuyer": (side == "buy"),
            "realized_pnl": 0.0,
            "fee": fee,
            "is_close": False,
        })

        current_balance = state.get("balance_usdt", 150.0)
        state["balance_usdt"] = current_balance - fee

    return mutate_paper_state(_mutate)
