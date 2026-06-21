import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

# 1. Store first_entry_price in execute_order (Simulated trading)
old_sim_entry = """            if s["avg_price"] <= 0:
                s["avg_price"] = price
                s["entry_atr"] = max(s.get("current_atr", 0.0), price * 0.005)"""
new_sim_entry = """            if s["avg_price"] <= 0:
                s["avg_price"] = price
                s["first_entry_price"] = price
                s["entry_atr"] = max(s.get("current_atr", 0.0), price * 0.005)"""
code = code.replace(old_sim_entry, new_sim_entry)

# 2. Store first_entry_price in execute_order (Live trading)
old_live_entry = """            if s["avg_price"] <= 0:
                s["avg_price"] = fill_price
                s["entry_atr"] = max(s.get("current_atr", 0.0), fill_price * 0.005)"""
new_live_entry = """            if s["avg_price"] <= 0:
                s["avg_price"] = fill_price
                s["first_entry_price"] = fill_price
                s["entry_atr"] = max(s.get("current_atr", 0.0), fill_price * 0.005)"""
code = code.replace(old_live_entry, new_live_entry)

# 3. Reset first_entry_price in reset_coin_state
old_reset = """    s["avg_entry_price"] = 0.0"""
new_reset = """    s["avg_entry_price"] = 0.0
    s["first_entry_price"] = 0.0"""
code = code.replace(old_reset, new_reset)

# 4. Philosophy B+ logic in check_position_exits
old_sync = """    # --- 停損同步 (Trailing SL Sync) ---
    if s.get("entry_count", 0) > 0:
        if is_long:
            sl = max(sl, avg * 1.0015)
        else:
            sl = min(sl, avg * 0.9985)"""
new_sync = """    # --- 停損同步 (Trailing SL Sync) - Philosophy B+ ---
    if s.get("entry_count", 0) > 0:
        first_entry = s.get("first_entry_price", avg)
        atr_half = s.get("current_atr", atr_val) * 0.5
        
        if is_long:
            sl_floor = first_entry - atr_half
            sl = max(sl, sl_floor)
        else:
            sl_floor = first_entry + atr_half
            sl = min(sl, sl_floor)"""
code = code.replace(old_sync, new_sync)

with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Applied Philosophy B+ Mods")
