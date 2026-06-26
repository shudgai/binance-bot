import json
import os
import fcntl
import threading
import time

PAPER_STATE_FILE = "paper_state.json"
_lock = threading.Lock()

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
    with _lock:
        if not os.path.exists(PAPER_STATE_FILE):
            state = {
                "balance_usdt": 150.0,
                "session_start_balance": 150.0,
                "positions": {},
                "trades": []
            }
        else:
            with open(PAPER_STATE_FILE, "r") as f:
                state = json.load(f)

        positions = state.get("positions", {})
        
        # Standardize the symbol key
        paper_key = symbol
        if ":USDT" not in symbol:
            paper_key = f"{symbol}:USDT"

        if is_close:
            # Handle closing a position
            pos = positions.get(paper_key)
            # 重複平倉防護：若倉位已為 0（或不存在），拒絕再次記錄平倉
            if not pos or abs(pos.get("qty", 0.0)) < 0.000001:
                print(f"[REJECT_DUP_CLOSE] {symbol} 倉位已平 (qty≈0)，忽略重複平倉記錄！")
                return
            if pos:
                # Update the position's realized pnl
                current_pnl = pos.get("realized_pnl", 0.0)
                pos["realized_pnl"] = current_pnl + pnl
                
                # FIFO entry removal for paper state
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
                            
                # If absolute qty drops to 0, ensure it is fully closed
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
                "is_close": True
            }
            fee = price * qty_abs * 0.0005
            trade_entry["fee"] = fee
            state["trades"].append(trade_entry)
            
            # Update overall balance
            current_balance = state.get("balance_usdt", 150.0)
            state["balance_usdt"] = current_balance + pnl - fee
        else:
            # Handle new entry
            signed_qty = qty_abs if side == "buy" else -qty_abs
            
            if paper_key in positions and abs(positions[paper_key].get("qty", 0)) > 0.000001:
                # If position exists and is not closed, update avg price/qty (scaling in)
                old_pos = positions[paper_key]
                old_qty = old_pos.get("qty", 0)
                old_avg = old_pos.get("avg_price", 0)
                
                new_qty = old_qty + signed_qty
                
                # Simple average logic for scaling in the same direction
                if (old_qty > 0 and signed_qty > 0) or (old_qty < 0 and signed_qty < 0):
                    new_avg = ((old_avg * abs(old_qty)) + (price * abs(signed_qty))) / abs(new_qty)
                else:
                    # If hedging or reversing, we don't update avg price simply here, but usually it's closed first.
                    new_avg = price if abs(new_qty) > 0.000001 else 0.0
                
                entries = old_pos.get("entries", [])
                entries.append({"price": price, "qty": qty_abs, "time": int(time.time() * 1000), "side": side})
                
                positions[paper_key] = {
                    "qty": new_qty,
                    "avg_price": new_avg,
                    "realized_pnl": old_pos.get("realized_pnl", 0.0),
                    "entries": entries
                }
            else:
                # New position
                positions[paper_key] = {
                    "qty": signed_qty,
                    "avg_price": price,
                    "realized_pnl": positions.get(paper_key, {}).get("realized_pnl", 0.0),
                    "entries": [{"price": price, "qty": qty_abs, "time": int(time.time() * 1000), "side": side}]
                }
            
            # Add to trades list
            fee = price * qty_abs * 0.0005
            state["trades"].append({
                "symbol": paper_key,
                "price": price,
                "qty": qty_abs,
                "time": int(time.time() * 1000),
                "isBuyer": (side == "buy"),
                "realized_pnl": 0.0,
                "fee": fee,
                "is_close": False
            })
            
            # Deduct fee from balance
            current_balance = state.get("balance_usdt", 150.0)
            state["balance_usdt"] = current_balance - fee

        with open(PAPER_STATE_FILE, "w") as f:
            json.dump(state, f, indent=4)
