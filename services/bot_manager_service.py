import os
import json
import sys
import time
import threading
import subprocess
from services.system_log_service import add_system_log

# 模擬交易機器人狀態 (支援多幣種多進程)
bot_status = {
    "is_running": False,
    "strategy": "Top 5 Sniper Mode",
    "balance_quote": 150.0,
    "active_orders": 0,
    "active_symbols": [],  # 現在改為陣列存放多個幣種 (主攻幣, 其實現在只支援單一運行)
    "watch_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"], # 使用者自訂的關注幣種
    "regime": "多幣種監控中",
    "coin_regimes": {},    # { symbol: regime }
    "trade_amount": 150.0,
}

bot_processes = {}  # {symbol: subprocess.Popen}
SYMBOL_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot_symbols.json")
DEFAULT_SYMBOLS = [
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "RENDERUSDT", "LINKUSDT", "AVAXUSDT",
    "DOTUSDT", "UNIUSDT", "NEARUSDT", "FETUSDT", "SUIUSDT"
]


def normalize_symbol(sym):
    if sym is None:
        return ""
    sym = str(sym).strip().upper()
    if not sym:
        return ""
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    return sym


def normalize_symbol_list(symbols, max_count=20):
    if isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        return list(DEFAULT_SYMBOLS[:max_count])
    seen = []
    for item in symbols:
        sym = normalize_symbol(item)
        if sym and sym not in seen:
            seen.append(sym)
    return seen[:max_count]


def load_symbol_config():
    try:
        with open(SYMBOL_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return normalize_symbol_list(data.get("symbols", []))
        return normalize_symbol_list(data)
    except Exception:
        return list(DEFAULT_SYMBOLS)


def load_symbol_profiles():
    try:
        with open(SYMBOL_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw_profiles = data.get("profiles", {})
            if isinstance(raw_profiles, dict):
                normalized_profiles = {}
                for sym, profile in raw_profiles.items():
                    normalized = normalize_symbol(sym)
                    if normalized and isinstance(profile, dict):
                        normalized_profiles[normalized] = profile
                return normalized_profiles
        return {}
    except Exception:
        return {}


def save_symbol_config(symbols):
    normalized = normalize_symbol_list(symbols)
    profiles = load_symbol_profiles()
    payload = {"symbols": normalized}
    if profiles:
        payload["profiles"] = profiles
    with open(SYMBOL_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return normalized


def get_bot_status():
    from services.paper_trade_service import get_paper_balance
    import os
    import json
    from dotenv import load_dotenv
    
    load_dotenv()
    if os.getenv("TRADING_MODE", "paper") == "paper":
        bot_status["balance_quote"] = get_paper_balance()
        
        # Calculate total realized PNL from paper_state.json
        try:
            total_realized = 0.0
            total_fees = 0.0
            state_path = os.path.join(os.path.dirname(__file__), "..", "paper_state.json")
            if os.path.exists(state_path):
                with open(state_path, "r") as f:
                    state = json.load(f)
                for t in state.get("trades", []):
                    fee = t.get("fee", (t.get("price", 0) * abs(t.get("qty", 0))) * 0.0005)
                    total_fees += fee
                    if t.get("is_close"):
                        pnl = t.get("realized_pnl", 0.0)
                        total_realized += pnl
            bot_status["total_realized_pnl"] = total_realized - total_fees
        except Exception as e:
            bot_status["total_realized_pnl"] = 0.0
        
    return bot_status

def set_bot_balance_quote(balance: float):
    bot_status["balance_quote"] = balance

def update_bot_status(key, value):
    bot_status[key] = value

def read_bot_output(proc, sym):
    for line in iter(proc.stdout.readline, ''):
        line = line.strip()
        if line:
            if line.startswith("@@REGIME@@"):
                bot_status["regime"] = line.replace("@@REGIME@@", "").strip()
            elif line.startswith("@@COIN_REGIME@@"):
                parts = line.replace("@@COIN_REGIME@@", "").strip().split("@@")
                if len(parts) >= 2:
                    coin_sym = parts[0]
                    coin_reg = parts[1]
                    bot_status["coin_regimes"][coin_sym] = coin_reg
            elif line.startswith("@@AMOUNT@@"):
                try:
                    bot_status["trade_amount"] = float(line.replace("@@AMOUNT@@", "").strip())
                except:
                    pass
            elif line.startswith("@@LEVERAGE@@"):
                try:
                    bot_status["leverage"] = int(line.replace("@@LEVERAGE@@", "").strip())
                except:
                    pass
            elif line.startswith("@@COIN_DEBUG@@"):
                add_system_log(line.replace("@@COIN_DEBUG@@", "").strip(), "info")
            else:
                add_system_log(f"[{sym}] {line}", "info")
    proc.stdout.close()
    proc.wait()
    
    if proc.returncode == 4:
        # 單幣熔斷停牌 (Exit Code 4)
        from services.radar_service import replace_dead_coin, blacklist_coin
        blacklist_coin(sym, duration_sec=24*3600)
        threading.Thread(target=replace_dead_coin, args=(sym,), daemon=True).start()
    elif proc.returncode == 3:
        # 死水幣觸發淘汰 (Exit Code 3)
        from services.radar_service import replace_dead_coin
        threading.Thread(target=replace_dead_coin, args=(sym,), daemon=True).start()
    elif proc.returncode == 2:
        # 觸發全自動雷達換倉機制 (保留)
        from services.radar_service import auto_radar_switch
        threading.Thread(target=auto_radar_switch, daemon=True).start()
    elif bot_status["is_running"] and sym in bot_processes and bot_processes[sym] == proc:
        # 非預期停止（使用者未手動關閉），啟動守護重啟機制
        add_system_log(f"⚠️ [系統守護] 偵測到機器人({sym})意外停止，將在 5 秒後自動重啟...", "danger")
        def daemon_restart():
            time.sleep(5)
            if not bot_status["is_running"]:
                return
            if sym == "__multi__":
                _start_multi_coin_bot(bot_status["trade_amount"])
            else:
                _start_single_bot(sym, bot_status["trade_amount"])
        threading.Thread(target=daemon_restart, daemon=True).start()


def _start_single_bot(symbol: str, trade_amt: float):
    global bot_processes
    if symbol == "__multi__":
        _start_multi_coin_bot(trade_amt)
        return
    bot_status["active_symbols"] = [symbol]
    save_symbol_config(bot_status["active_symbols"])
    _start_multi_coin_bot(trade_amt)


def _start_multi_coin_bot(trade_amt: float):
    global bot_processes
    cmd = [sys.executable, "-u", "multi_coin_bot.py"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
    bot_processes["__multi__"] = proc
    threading.Thread(target=read_bot_output, args=(proc, "__multi__"), daemon=True).start()
    count = len(bot_status.get("active_symbols", []))
    add_system_log(f"🚀 已啟動多幣輪動機器人 ({count}個幣種, 金額: {trade_amt})", "success")

def _get_open_position_symbols():
    try:
        state_path = os.path.join(os.path.dirname(__file__), "..", "paper_state.json")
        if not os.path.exists(state_path):
            return []
        with open(state_path, "r") as f:
            state = json.load(f)
        open_syms = []
        for key, pos in state.get("positions", {}).items():
            if abs(float(pos.get("qty", 0.0))) > 0.000001:
                sym = key.replace(":USDT", "USDT").replace(":", "")
                open_syms.append(sym)
        return open_syms
    except Exception:
        return []

def start_bot(symbols=None, trade_amt: float = None):
    global bot_processes
    # 確保啟動新 bot 前先清除舊的 bot 進程，避免系統中存在重複執行
    kill_bot()

    if symbols is None:
        symbols = load_symbol_config()
    elif isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        symbols = list(DEFAULT_SYMBOLS)

    symbols = normalize_symbol_list(symbols)
    # 保留有持倉的幣種，避免被換掉
    open_syms = _get_open_position_symbols()
    for s in open_syms:
        if s not in symbols:
            symbols.append(s)
    save_symbol_config(symbols)

    if trade_amt is None:
        trade_amt = bot_status.get("trade_amount", 150.0)

    bot_status["is_running"] = True
    bot_status["active_symbols"] = symbols
    bot_status["trade_amount"] = trade_amt
    
    # 啟動單一多幣行程
    _start_multi_coin_bot(trade_amt)

def _kill_single_bot(symbol: str):
    global bot_processes
    if symbol in bot_processes and bot_processes[symbol]:
        proc = bot_processes[symbol]
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except:
            proc.kill()
        bot_processes[symbol] = None
        del bot_processes[symbol]
        add_system_log(f"🛑 已終止背景機器人 ({symbol})", "warning")

def kill_bot():
    global bot_processes
    bot_status["is_running"] = False
    symbols = list(bot_processes.keys())
    for s in symbols:
        _kill_single_bot(s)
    
    # 確保所有遺留的 bot 行程都被清除，防止 API 重啟後產生孤兒行程
    # 使用精確字串匹配，避免誤殺 multi_coin_bot_v2.py（其名稱包含 multi_coin_bot.py 作為子字串）
    try:
        os.system("pkill -f 'multi_coin_bot_v2\\.py'")
        os.system("pkill -f '[^2]multi_coin_bot\\.py'")
    except:
        pass

    # 移除單例鎖定檔，避免已終止程序遺留鎖定導致新進程啟動失敗
    try:
        os.remove("/tmp/binance_bot_single_instance.lock")
    except FileNotFoundError:
        pass
    except Exception:
        pass

def restart_bot():
    kill_bot()
    start_bot()

def toggle_bot():
    is_running = not bot_status["is_running"]
    status_str = "啟動" if is_running else "停止"
    add_system_log(f"手動{status_str}機器人群組", "info")
    
    if is_running:
        start_bot()
    else:
        kill_bot()
    return bot_status["is_running"]

def set_bot_symbol(symbols):
    if isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        symbols = list(DEFAULT_SYMBOLS)

    symbols = normalize_symbol_list(symbols)
    save_symbol_config(symbols)
    bot_status["active_symbols"] = symbols

    amt = bot_status.get("trade_amount", 150.0)
    bot_status["strategy"] = f"Top 5 Sniper ({amt})"
    add_system_log(f"🎯 自動交易監聽目標切換為: {', '.join(symbols)}", "info")

    return symbols

def set_bot_watch_symbols(symbols):
    if not isinstance(symbols, list):
        symbols = [symbols]
    # 限定 5 隻
    symbols = [s.upper() for s in symbols][:5]
    bot_status["watch_symbols"] = symbols
    add_system_log(f"📋 使用者更新自選關注清單: {', '.join(symbols)}", "info")
    return symbols

def set_bot_amount(amount: float):
    if amount < 0 or amount > 1000:
        raise ValueError("單次交易數量必須限制在 0 至 1000 之間")
    bot_status["trade_amount"] = amount
    bot_status["strategy"] = f"Top 5 Sniper ({amount})"
    add_system_log(f"⚙️ 自動交易單次數量設定為: {amount}", "info")
    
    if bot_status.get("is_running"):
        add_system_log("♻️ 已重新啟動所有機器人以套用新的下單金額", "warning")
        restart_bot()
        
    return bot_status["trade_amount"]
