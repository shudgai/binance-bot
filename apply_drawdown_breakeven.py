import sys
import re

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# 1. Add Daily Drawdown Global Variables
if "START_OF_DAY_BALANCE = 0.0" not in code:
    code = code.replace("REAL_BALANCE = 150.0", "REAL_BALANCE = 150.0\nSTART_OF_DAY_BALANCE = 0.0\nLAST_DAY_STR = ''\nIS_DAILY_BANNED = False\nfrom datetime import datetime")

# 2. Modify fetch_real_balance
old_fetch_balance = """async def fetch_real_balance():
    global REAL_BALANCE
    try:
        if PAPER_TRADING:
            pass # 由 update_paper_state 處理
        else:
            balance_info = await exchange_futures.fetch_balance()
            usdt_balance = float(balance_info.get('USDT', {}).get('total', 150.0))
            REAL_BALANCE = usdt_balance
    except Exception as e:
        print(f"⚠️ 獲取真實餘額失敗: {e}")"""

new_fetch_balance = """async def fetch_real_balance():
    global REAL_BALANCE, START_OF_DAY_BALANCE, LAST_DAY_STR, IS_DAILY_BANNED
    try:
        if PAPER_TRADING:
            # Paper trading needs daily drawdown too
            import json
            import os
            if os.path.exists('paper_state.json'):
                with open('paper_state.json', 'r') as f:
                    state = json.load(f)
                    REAL_BALANCE = float(state.get("balance_usdt", 150.0))
        else:
            balance_info = await exchange_futures.fetch_balance()
            REAL_BALANCE = float(balance_info.get('USDT', {}).get('total', 150.0))
            
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        if today_str != LAST_DAY_STR:
            START_OF_DAY_BALANCE = REAL_BALANCE
            LAST_DAY_STR = today_str
            IS_DAILY_BANNED = False
            print(f"🌅 [換日] 紀錄當日起始資金: {START_OF_DAY_BALANCE:.2f}")
            
        if START_OF_DAY_BALANCE > 0:
            drawdown = (START_OF_DAY_BALANCE - REAL_BALANCE) / START_OF_DAY_BALANCE
            if drawdown >= 0.10: # 10.0% Daily Limit
                if not IS_DAILY_BANNED:
                    IS_DAILY_BANNED = True
                    print(f"🚨 [強制關機] 當日累積虧損達 {drawdown*100:.2f}% (>= 10%)！已觸發日損上限，今日禁止所有新開倉。")
            else:
                IS_DAILY_BANNED = False

    except Exception as e:
        print(f"⚠️ 獲取餘額失敗: {e}")"""

if "START_OF_DAY_BALANCE" not in old_fetch_balance:
    code = code.replace(old_fetch_balance, new_fetch_balance)

# 3. Add IS_DAILY_BANNED check to check_entries
old_check_entries_start = """async def check_entries():
    \"\"\"
    並發處理進場信號，限制每個週期的最大並發數以避免 API Rate Limit
    \"\"\""""

new_check_entries_start = """async def check_entries():
    \"\"\"
    並發處理進場信號，限制每個週期的最大並發數以避免 API Rate Limit
    \"\"\"
    global IS_DAILY_BANNED
    if IS_DAILY_BANNED:
        return # 觸發日損上限，停止開倉
"""
if "global IS_DAILY_BANNED" not in code.split("async def check_entries():")[1][:200]:
    code = code.replace(old_check_entries_start, new_check_entries_start)

# 4. Modify update_trailing_stop for 0.5% breakeven
old_breakeven_long = """            # --- 保本邏輯 ---
            trigger_mult = s.get("breakeven_trigger")
            if trigger_mult is None:
                trigger_mult = s.get("sl_atr_multiplier", 1.5)
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = s["avg_price"] + sl_dist_atr
            if current_price >= breakeven_trigger:
                breakeven_sl = s["avg_price"]
                trail_sl = max(trail_sl, breakeven_sl)"""

new_breakeven_long = """            # --- 0.5% 強制保本邏輯 ---
            if current_price >= s["avg_price"] * 1.005:
                breakeven_sl = s["avg_price"] * 1.0005 # +0.05% 覆蓋手續費
                trail_sl = max(trail_sl, breakeven_sl)"""

code = code.replace(old_breakeven_long, new_breakeven_long)

old_breakeven_short = """            # --- 保本邏輯 ---
            trigger_mult = s.get("breakeven_trigger")
            if trigger_mult is None:
                trigger_mult = s.get("sl_atr_multiplier", 1.5)
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = s["avg_price"] - sl_dist_atr
            if current_price <= breakeven_trigger:
                breakeven_sl = s["avg_price"]
                trail_sl = min(trail_sl, breakeven_sl)"""

new_breakeven_short = """            # --- 0.5% 強制保本邏輯 ---
            if current_price <= s["avg_price"] * 0.995:
                breakeven_sl = s["avg_price"] * 0.9995 # -0.05% 覆蓋手續費
                trail_sl = min(trail_sl, breakeven_sl)"""

code = code.replace(old_breakeven_short, new_breakeven_short)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied 10% daily drawdown and 0.5% breakeven stop.")
