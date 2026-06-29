import os
import json
from services.binance_service import get_price
from services.bot_manager_service import restart_bot, get_bot_status
from services.update_paper_state import update_paper_state
from services.system_log_service import add_system_log

PAPER_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "paper_state.json")
import sys
# 測試環境隔離，避免單元測試污染實際紙交易數據
if any("pytest" in x or "unittest" in x for x in sys.argv) or "pytest" in sys.modules or "unittest" in sys.modules:
    PAPER_STATE_FILE = "test_paper_state.json"

def get_paper_position(symbol: str, quote_asset: str, base_asset: str, paper_key: str):
    qty = 0.0
    avg_price = 0.0
    realized_pnl = 0.0
    
    if os.path.exists(PAPER_STATE_FILE):
        try:
            with open(PAPER_STATE_FILE, "r") as f:
                state = json.load(f)
            positions = state.get("positions", {})
            pos = positions.get(paper_key)
            if pos is None:
                pos = positions.get(f"{symbol}:USDT") or positions.get(symbol) or {}
            qty = float(pos.get("qty", 0.0))
            avg_price = float(pos.get("avg_price", 0.0))
            realized_pnl = float(pos.get("realized_pnl", 0.0))
        except:
            pass

    current_price = 0.0
    try:
        current_price = get_price(symbol)["price"]
    except:
        pass

    unrealized_pnl = 0.0
    if qty > 0:
        unrealized_pnl = (current_price - avg_price) * abs(qty)
    elif qty < 0:
        unrealized_pnl = (avg_price - current_price) * abs(qty)

    total_cost = abs(qty) * avg_price
    current_value = abs(qty) * current_price
    pnl_percent = (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0.0

    return {
        "asset": base_asset,
        "quote_asset": quote_asset,
        "qty": qty,
        "avg_price": avg_price,
        "total_cost": total_cost,
        "current_price": current_price,
        "current_value": current_value,
        "pnl": unrealized_pnl,
        "pnl_percent": pnl_percent,
        "realized_pnl": realized_pnl
    }

def get_paper_trades(symbol: str, paper_key: str):
    if os.path.exists(PAPER_STATE_FILE):
        try:
            with open(PAPER_STATE_FILE, "r") as f:
                state = json.load(f)
                trades = state.get("trades", [])
                if symbol == "ALL":
                    result = list(reversed(trades))[:30]
                    _enrich_trades_with_current_price(result, state)
                    return result
                symbol_trades = [t for t in trades if t.get("symbol") in (symbol, paper_key)]
                result = list(reversed(symbol_trades))[:15]
                _enrich_trades_with_current_price(result, state)
                return result
        except:
            return []
    return []

def _enrich_trades_with_current_price(trades, state):
    """為每筆交易補上 current_price (從即時報價)"""
    for t in trades:
        sym = t.get("symbol", "")
        if not sym:
            continue
        try:
            clean_sym = sym.replace(":USDT", "USDT")
            price_data = get_price(clean_sym)
            t["current_price"] = price_data.get("price", 0)
        except:
            t["current_price"] = 0

def get_paper_balance():
    if os.path.exists(PAPER_STATE_FILE):
        try:
            with open(PAPER_STATE_FILE, "r") as f:
                state = json.load(f)
                return float(state.get("balance_usdt", 150.0))
        except:
            pass
    return 150.0

def market_buy(symbol: str, amount: float):
    price = get_price(symbol)["price"]
    qty = amount / price
    active_sym = f"{symbol.replace('USDT', '')}:USDT"
    if ":USDT:USDT" in active_sym:
        active_sym = active_sym.replace(":USDT:USDT", ":USDT")
        
    update_paper_state(active_sym, "buy", price, qty)
    
    bot_status = get_bot_status()
    if bot_status.get("is_running"):
        add_system_log("♻️ 已手動加倉...", "warning")
        
    return {"orderId": "manual_paper", "executedQty": str(qty)}

def market_short(symbol: str, amount: float):
    price = get_price(symbol)["price"]
    qty = amount / price
    active_sym = f"{symbol.replace('USDT', '')}:USDT"
    if ":USDT:USDT" in active_sym:
        active_sym = active_sym.replace(":USDT:USDT", ":USDT")
        
    update_paper_state(active_sym, "sell", price, qty)
    
    bot_status = get_bot_status()
    if bot_status.get("is_running"):
        add_system_log("♻️ 已手動加倉...", "warning")
        
    return {"orderId": "manual_paper", "executedQty": str(qty)}

def market_sell(symbol: str, paper_key: str):
    if os.path.exists(PAPER_STATE_FILE):
        with open(PAPER_STATE_FILE, "r") as f:
            state = json.load(f)
        
        positions = state.get("positions", {})
        active_sym = paper_key
        pos = positions.get(paper_key)
        if pos is None:
            pos = positions.get(f"{symbol}:USDT") or positions.get(symbol) or {}
            if pos:
                active_sym = f"{symbol}:USDT" if positions.get(f"{symbol}:USDT") else symbol
            
        qty = float(pos.get("qty", 0.0))
        
        if abs(qty) > 0:
            current_price = get_price(symbol)["price"]
            avg_price = float(pos.get("avg_price", 0.0))
            
            if avg_price <= 0.0:
                pnl = 0.0
            else:
                pnl = (current_price - avg_price) * qty
                
            close_side = "sell" if qty > 0 else "buy"
            
            update_paper_state(active_sym, close_side, current_price, abs(qty), is_close=True, pnl=pnl)
            
            bot_status = get_bot_status()
            if bot_status.get("is_running"):
                add_system_log("♻️ 已重置虛擬倉位...", "warning")
                
            return f"模擬平倉成功！獲利 {pnl:.2f} USDT"
        else:
            add_system_log(f"🟡 {symbol} 已有倉位已平倉，無需操作", "warning")
            return f"{symbol} 倉位已平倉"
    add_system_log(f"🟡 {symbol} 無對應倉位紀錄", "warning")
    return f"{symbol} 無倉位紀錄"

def force_close_all_positions():
    """強制平倉所有持有部位 (每日重置使用)"""
    if os.path.exists(PAPER_STATE_FILE):
        import fcntl
        with open(PAPER_STATE_FILE, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                state = json.load(f)
            except:
                fcntl.flock(f, fcntl.LOCK_UN)
                return
            
            positions = state.get("positions", {})
            symbols_to_close = []
            for sym, pos in positions.items():
                if abs(float(pos.get("qty", 0.0))) > 0:
                    symbols_to_close.append(sym)
                    
            fcntl.flock(f, fcntl.LOCK_UN)
            
        # 逐一呼叫 market_sell 進行平倉
        # market_sell 內部會重新讀取並上鎖 update_paper_state
        for sym in symbols_to_close:
            try:
                # sym 通常是 symbol:USDT，我們要還原成純 symbol 去抓價格
                raw_sym = sym.replace(":USDT", "") if ":USDT" in sym else sym
                market_sell(raw_sym, sym)
                add_system_log(f"🧹 [每日淨空] 已強制平倉 {raw_sym}", "info")
            except Exception as e:
                add_system_log(f"⚠️ [每日淨空] 平倉 {sym} 失敗: {e}", "danger")


def reset_paper_state(starting_balance: float = 150.0):
    """將 paper_state.json 重置為初始值，並回復起始資金。"""
    bot_status = get_bot_status()
    if bot_status.get("is_running"):
        add_system_log("🧹 正在停止機器人以進行紙交易重置...", "warning")
        try:
            from services.bot_manager_service import kill_bot
            kill_bot()
        except Exception:
            pass

    state = {
        "balance_usdt": float(starting_balance),
        "session_start_balance": float(starting_balance),
        "positions": {},
        "trades": []
    }
    import fcntl
    with open(PAPER_STATE_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(state, f, indent=4)
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)

    add_system_log(f"🧹 紙交易狀態已重置為 {starting_balance} USDT，持倉與交易紀錄已清空。", "success")

    if bot_status.get("is_running"):
        add_system_log("♻️ 已重置紙交易狀態，將自動同步初始資金。", "warning")

    return state


def get_session_start_balance():
    """回傳本次 session 的起始資金（reset 時記錄），預設 150.0。"""
    if os.path.exists(PAPER_STATE_FILE):
        try:
            with open(PAPER_STATE_FILE, "r") as f:
                state = json.load(f)
                return float(state.get("session_start_balance", 150.0))
        except:
            pass
    return 150.0


def get_paper_positions():
    """Return all positions for the current paper state."""
    state = {}
    if os.path.exists(PAPER_STATE_FILE):
        try:
            with open(PAPER_STATE_FILE, 'r') as f:
                state = json.load(f)
        except Exception:
            pass
    positions = {}
    for coin, data in state.get('positions', {}).items():
        positions[coin] = {
            "qty": data.get("qty", 0),
            "avg_price": data.get("avg_price", 0),
            "realized_pnl": data.get("realized_pnl", 0)
        }
    return positions
