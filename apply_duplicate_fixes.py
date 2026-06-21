import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# Fix 1: Make is_symbol_locked more robust against duplicate orders
old_locked = 'return abs(s["qty"]) > 0.000001 or s["entry_count"] > 0 or s["open_time"] > 0 or s["status"] in ("COOLDOWN", "BANNED")'
new_locked = 'return abs(s["qty"]) > 0.000001 or s["entry_count"] > 0 or s["open_time"] > 0 or s["status"] in ("COOLDOWN", "BANNED") or s.get("pending_side") is not None'
code = code.replace(old_locked, new_locked)

# Fix 2: Add 60s hard cooldown in check_entries for NEW orders
old_check_entries_top = """        if is_symbol_locked(sym):
            continue"""
new_check_entries_top = """        if is_symbol_locked(sym):
            continue
            
        # [防連發保護] 新開倉前必須確保距離上次開倉大於 60 秒
        if time.time() - s.get("last_entry_time", 0) < 60:
            continue"""
code = code.replace(old_check_entries_top, new_check_entries_top)

# Fix 3: Exit issue with -0.16%
# Let's verify where a -0.16% exit could come from.
# In `check_exits`, if it's Whipsaw stop or Momentum fade. Let's add a log when an exit is triggered to ensure it's not a noise.
# Actually, the user's ATR floor is already there: sl_dist = max(atr_val * sl_multiplier, p * 0.005)
# Wait, maybe the trailing SL gets too tight?
# Let's ensure the initial_sl is respected.

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied duplicate entry fixes!")
