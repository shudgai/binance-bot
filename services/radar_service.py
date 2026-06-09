import time
import threading
from services.system_log_service import add_system_log
from services.bot_manager_service import get_bot_status, start_bot, kill_bot
from services.binance_service import get_top_volume_altcoins

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

def auto_radar_switch(force_start=False):
    global last_api_call
    if not radar_lock.acquire(blocking=False):
        add_system_log("⚠️ [雷達掃描] 前一次掃描尚未完成，跳過", "warning")
        return get_bot_status().get("active_symbols", [])
    try:
        add_system_log("🔥 [雷達切換] 啟動全網小幣掃描 (Top 5 成交額)...", "warning")
        
        elapsed = time.time() - last_api_call
        if elapsed < API_RATE_LIMIT:
            time.sleep(API_RATE_LIMIT - elapsed)
        last_api_call = time.time()

        bot_status = get_bot_status()
        current_syms = bot_status.get("active_symbols", [])
        
        clean_blacklist()
        ignore_list = list(BLACKLIST.keys())
        top_5 = get_top_volume_altcoins(5, ignore_list=ignore_list)

        if not top_5:
            add_system_log("⚠️ [雷達掃描] 無法獲取 Top 5 榜單，維持原狀", "warning")
            return current_syms

        # 排序讓比較不受順序影響
        if sorted(top_5) == sorted(current_syms):
            add_system_log(f"✅ [雷達掃描] 當前榜單依然稱霸 ({', '.join(top_5)})，維持不變", "success")
            if force_start and not bot_status.get("is_running"):
                start_bot(top_5, bot_status.get("trade_amount", 150.0))
            return top_5

        add_system_log(f"🎯 [雷達鎖定] 最新熱門小幣榜單: {', '.join(top_5)}", "success")
        bot_status["active_symbols"] = top_5
        
        if bot_status.get("is_running") or force_start:
            start_bot(top_5, bot_status.get("trade_amount", 150.0))
            
        return top_5
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
