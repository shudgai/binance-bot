import json
import os
import uuid
import time
import sys
import fcntl

PAPER_STATE_FILE = "paper_state.json"
# 測試環境隔離，避免單元測試污染實際紙交易數據
if any("pytest" in x or "unittest" in x for x in sys.argv) or "pytest" in sys.modules or "unittest" in sys.modules:
    PAPER_STATE_FILE = "test_paper_state.json"

def update_paper_state(symbol, side, price, qty, is_close=False, pnl=0.0):
    state = {
        "balance_usdt": 150.0,
        "positions": {},
        "trades": []
    }
    
    # Ensure file exists
    if not os.path.exists(PAPER_STATE_FILE):
        with open(PAPER_STATE_FILE, "w") as f:
            json.dump(state, f)
            
    with open(PAPER_STATE_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            state = json.load(f)
        except:
            pass
            
        state.setdefault("trades", [])
        pos = state.setdefault("positions", {}).setdefault(symbol, {"qty": 0.0, "avg_price": 0.0, "realized_pnl": 0.0})
        
        trade = {
            "id": str(uuid.uuid4())[:8],
            "order_id": str(uuid.uuid4())[:8],
            "symbol": symbol,
            "price": price,
            "qty": qty,
            "quote_qty": price * qty,
            "time": int(time.time() * 1000),
            "isBuyer": (side == 'buy'),
            "isMaker": False,
            "realized_pnl": pnl if is_close else 0.0,
            "is_close": is_close
        }
        
        state["trades"].append(trade)
        # Keep only the last 100 trades to avoid file bloat
        state["trades"] = state["trades"][-100:]
        
        current_qty = pos["qty"]
        current_avg = pos["avg_price"]
        
        # [ATOMIC GLOBAL LOCK] 防止多行程併發開倉的 Race Condition
        if current_qty == 0 and not is_close:
            open_count = sum(1 for sym, p in state["positions"].items() if abs(p.get("qty", 0)) > 0)
            if open_count >= 2:
                print(f"🛑 [底層阻擋] 檢測到持倉數已達上限 (2)，放棄 {symbol} 的開單請求！")
                raise ValueError("MAX_POSITIONS reached")
        
        if side == 'buy':
            signed_qty = qty
        else:
            signed_qty = -qty
            
        if not is_close:
            new_qty = current_qty + signed_qty
            if abs(new_qty) < 0.000001:
                pos["avg_price"] = 0.0
                new_qty = 0.0
            else:
                # 1. 相同方向加倉 (Adding to position)
                if (current_qty > 0 and signed_qty > 0) or (current_qty < 0 and signed_qty < 0):
                    pos["avg_price"] = (abs(current_qty) * current_avg + abs(signed_qty) * price) / abs(new_qty)
                
                # 2. 反向開倉/翻倉 (Flipping position - new position size is larger than old)
                elif (current_qty > 0 and signed_qty < 0 and new_qty < 0) or (current_qty < 0 and signed_qty > 0 and new_qty > 0):
                    pos["avg_price"] = price # 新的成本價就是當前翻倉的價格
                    
                    # 把舊倉位的 PnL 結算掉
                    if current_qty > 0:
                        pnl_flip = (price - current_avg) * abs(current_qty)
                    else:
                        pnl_flip = (current_avg - price) * abs(current_qty)
                    pos["realized_pnl"] += pnl_flip
                    state["balance_usdt"] += pnl_flip
                    
                # 3. 部分平倉 (Partial close - doesn't change avg_price, but realizes some PnL)
                elif (current_qty > 0 and signed_qty < 0 and new_qty > 0) or (current_qty < 0 and signed_qty > 0 and new_qty < 0):
                    # pos["avg_price"] 不變
                    if current_qty > 0:
                        pnl_partial = (price - current_avg) * abs(signed_qty)
                    else:
                        pnl_partial = (current_avg - price) * abs(signed_qty)
                    pos["realized_pnl"] += pnl_partial
                    state["balance_usdt"] += pnl_partial
                    
                # 4. 全新開倉 (Fresh open)
                elif current_qty == 0:
                    pos["avg_price"] = price
                    
            pos["qty"] = new_qty
        else:
            # 防禦機制: 避免重複平倉導致把倉位平成反向且均價為 0
            if (current_qty > 0 and signed_qty > 0) or (current_qty < 0 and signed_qty < 0):
                print(f"�� [防禦] 嘗試平倉但方向錯誤 (增加倉位)，忽略！ current: {current_qty}, signed: {signed_qty}")
                new_qty = current_qty
                pnl = 0.0
            elif abs(signed_qty) > abs(current_qty) + 1e-6:
                print(f"🛑 [防禦] 嘗試平倉數量大於持有數量，只平倉剩餘部分！")
                signed_qty = -current_qty
                new_qty = 0.0
                pos["avg_price"] = 0.0
            else:
                new_qty = current_qty + signed_qty
                if abs(new_qty) < 0.000001:
                    new_qty = 0.0
                    pos["avg_price"] = 0.0
            
            pos["qty"] = new_qty
            pos["realized_pnl"] += pnl
            state["balance_usdt"] += pnl
            
        # 統一扣除手續費 (開平倉皆適用 Binance taker fee 0.05%)
        fee = (price * abs(qty)) * 0.0005
        state["balance_usdt"] -= fee

        state["positions"][symbol] = pos
        
        # Write back to file while still locked
        f.seek(0)
        json.dump(state, f, indent=4)
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)
