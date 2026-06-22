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
            if pos:
                # Update the position's realized pnl
                current_pnl = pos.get("realized_pnl", 0.0)
                pos["realized_pnl"] = current_pnl + pnl
                pos["qty"] = 0.0
            
            # Add to trades list
            trade_entry = {
                "symbol": paper_key,
                "price": price,
                "qty": qty_abs,
                "time": int(time.time() * 1000),
                "isBuyer": (side == "buy"),
                "realized_pnl": pnl,
                "is_close": True
            }
            state["trades"].append(trade_entry)
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
                
                positions[paper_key] = {
                    "qty": new_qty,
                    "avg_price": new_avg,
                    "realized_pnl": old_pos.get("realized_pnl", 0.0)
                }
            else:
                # New position
                positions[paper_key] = {
                    "qty": signed_qty,
                    "avg_price": price,
                    "realized_pnl": positions.get(paper_key, {}).get("realized_pnl", 0.0)
                }
            
            # Add to trades list
            state["trades"].append({
                "symbol": paper_key,
                "price": price,
                "qty": qty_abs,
                "time": int(time.time() * 1000),
                "isBuyer": (side == "buy"),
                "realized_pnl": 0.0,
                "is_close": False
            })

        with open(PAPER_STATE_FILE, "w") as f:
            json.dump(state, f, indent=4)
