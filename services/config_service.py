import os
import json
import threading

WHITELIST_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "whitelist.json")
_lock = threading.Lock()

def get_whitelist():
    try:
        with _lock:
            if not os.path.exists(WHITELIST_FILE):
                return []
            with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"Error reading whitelist: {e}")
        return []

def update_whitelist(symbols_list):
    try:
        # 確保皆為大寫且帶有 USDT
        clean_list = []
        for sym in symbols_list:
            clean_sym = sym.strip().upper()
            if not clean_sym.endswith("USDT"):
                clean_sym += "USDT"
            if clean_sym not in clean_list:
                clean_list.append(clean_sym)
                
        with _lock:
            with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
                json.dump(clean_list, f, indent=4)
        return clean_list
    except Exception as e:
        print(f"Error updating whitelist: {e}")
        return []
