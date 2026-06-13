import os
import json
import time
import threading
from services.system_log_service import add_system_log
from services.bot_manager_service import get_bot_status, start_bot, kill_bot
from services.binance_service import get_top_volume_altcoins
from services.config_service import get_whitelist

# 雷達掃描冷卻
last_radar_scan = 0
RADAR_SCAN_COOLDOWN = 10.0
radar_lock = threading.Lock()
last_api_call = 0
API_RATE_LIMIT = 1.0

# 熔斷黑名單 {symbol: expire_timestamp}
BLACKLIST = {}

def clean_blacklist():
    global BLACKLIST
    now = time.time()
    BLACKLIST = {k: v for k, v in BLACKLIST.items() if v > now}

def blacklist_coin(symbol: str, duration_sec: int = 86400):
    global BLACKLIST
    BLACKLIST[symbol] = time.time() + duration_sec
    add_system_log(f"🚨 [熔斷機制] {symbol} 已被列入黑名單，{duration_sec//3600} 小時內不會再被選中", "danger")

def get_radar_cooldown():
    global last_radar_scan
    elapsed = time.time() - last_radar_scan
    return max(0.0, RADAR_SCAN_COOLDOWN - elapsed)

def trigger_manual_radar():
    global last_radar_scan
    elapsed = time.time() - last_radar_scan
    if elapsed < RADAR_SCAN_COOLDOWN:
        return {"status": "success", "best_symbols": get_bot_status().get("active_symbols", []), "cooldown": round(RADAR_SCAN_COOLDOWN - elapsed, 1)}
    last_radar_scan = time.time()
    best_symbols = auto_radar_switch(force_start=True)
    return {"status": "success", "best_symbols": best_symbols}

def _get_recently_traded_symbols(hours=24):
    try:
        state_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "paper_state.json")
        if not os.path.exists(state_path):
            return []
        with open(state_path, "r") as f:
            state = json.load(f)
        symbols = set()
        now_ms = time.time() * 1000
        for t in state.get("trades", []):
            if now_ms - t.get("time", 0) < hours * 3600 * 1000:
                sym = t.get("symbol", "").replace(":USDT", "USDT").replace(":", "")
                if sym:
                    symbols.add(sym)
        return list(symbols)
    except Exception as e:
        print(f"⚠️ [讀取交易歷史] 失敗: {e}")
        return []

def _get_open_position_symbols():
    try:
        state_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "paper_state.json")
        if not os.path.exists(state_path):
            return []
        with open(state_path, "r") as f:
            state = json.load(f)
        symbols = []
        for key, pos in state.get("positions", {}).items():
            if abs(float(pos.get("qty", 0.0))) > 0.000001:
                sym = key.replace(":USDT", "USDT").replace(":", "")
                symbols.append(sym)
        return symbols
    except Exception as e:
        print(f"⚠️ [讀取持倉] 失敗: {e}")
        return []

def auto_radar_switch(force_start=False):
    global last_api_call
    if not radar_lock.acquire(blocking=False):
        add_system_log("⚠️ [雷達掃描] 前一次掃描尚未完成，跳過", "warning")
        return get_bot_status().get("active_symbols", [])
    try:
        add_system_log("🔥 [雷達切換] 啟動全網小幣掃描 (Top 20 成交額)...", "warning")
        
        elapsed = time.time() - last_api_call
        if elapsed < API_RATE_LIMIT:
            time.sleep(API_RATE_LIMIT - elapsed)
        last_api_call = time.time()

        bot_status = get_bot_status()
        current_syms = bot_status.get("active_symbols", [])
        
        clean_blacklist()
        ignore_list = list(BLACKLIST.keys())
        
        # 系統指令：嚴格執行交易白名單
        whitelist = get_whitelist()
        top_20 = [sym for sym in whitelist if sym not in ignore_list]

        if not top_20:
            add_system_log("⚠️ [雷達掃描] 白名單全數被熔斷，維持原狀", "warning")
            return current_syms

        # 系統指令：嚴格執行交易白名單，不再保留「近期交易過」的幣種
        # 僅保留「目前仍有實質持倉」的幣種以防孤兒單
        open_syms = _get_open_position_symbols()
        preserved = [s for s in open_syms if s not in top_20]
        
        if preserved:
            add_system_log(f"🔒 [持倉保護] 以下幣種仍有實質持倉，將持續監控至平倉: {', '.join(preserved)}", "warning")
        final_symbols = top_20 + preserved

        # 排序讓比較不受順序影響
        if sorted(final_symbols) == sorted(current_syms):
            add_system_log(f"✅ [雷達掃描] 當前榜單依然稱霸 ({', '.join(final_symbols)})，維持不變", "success")
            if force_start and not bot_status.get("is_running"):
                start_bot(final_symbols, bot_status.get("trade_amount", 150.0))
            return final_symbols

        add_system_log(f"🎯 [雷達鎖定] 最新熱門小幣榜單: {', '.join(top_20)}", "success")
        if preserved:
            add_system_log(f"🔒 [持倉保護] 保留持倉幣種: {', '.join(preserved)}", "warning")
        bot_status["active_symbols"] = final_symbols
        
        if bot_status.get("is_running") or force_start:
            start_bot(final_symbols, bot_status.get("trade_amount", 150.0))
            
        return final_symbols
    except Exception as e:
        add_system_log(f"🚨 [雷達掃描] 掃描失敗: {e}", "danger")
        bot_status = get_bot_status()
        if not bot_status.get("is_running") and not force_start:
            kill_bot()
        return bot_status.get("active_symbols", [])
    finally:
        radar_lock.release()

def replace_dead_coin(symbol: str):
    try:
        bot_status = get_bot_status()
        current_syms = bot_status.get("active_symbols", [])
        if symbol in current_syms:
            current_syms.remove(symbol)
            
        add_system_log(f"💀 [死水汰換] 剔除無波動死水幣 {symbol}，尋找替補...", "warning")
        
        # 抓取前 15 名來尋找替補
        clean_blacklist()
        ignore_list = list(BLACKLIST.keys())
        top_15 = get_top_volume_altcoins(15, ignore_list=ignore_list)
        new_coin = None
        for coin in top_15:
            if coin not in current_syms:
                new_coin = coin
                break
                
        if new_coin:
            current_syms.append(new_coin)
            bot_status["active_symbols"] = current_syms
            add_system_log(f"✨ [自動補位] 成功選入候補熱門小幣: {new_coin}", "success")
            start_bot(current_syms, bot_status.get("trade_amount", 10.0))
        else:
            add_system_log(f"⚠️ [自動補位] 找不到合適的候補小幣", "danger")
    except Exception as e:
        add_system_log(f"🚨 [自動補位] 發生錯誤: {e}", "danger")
