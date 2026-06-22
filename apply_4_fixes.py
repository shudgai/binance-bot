import re
import os

target_file = '/home/shudgai999/project/binance-bot-live/multi_coin_bot.py'
backup_file = target_file + '.bak.4fixes'

with open(target_file, 'r', encoding='utf-8') as f:
    content = f.read()

# Backup
with open(backup_file, 'w', encoding='utf-8') as f:
    f.write(content)

# Fix 1: Entry Silence Zone (接刀防護)
# Find `def check_position_exits(exchange, sym):` and insert logic
silence_zone_logic = """
    # --- [優化] 進場後靜默區 (接刀防護) ---
    if hold_sec < 60 and profit_pct < -0.005:
        print(f"🔪 [接刀防護] {sym} 進場後 60 秒內瞬間跌破 0.5%，強制平倉並封禁 1 小時！ (淨利: {profit_pct*100:.2f}%)")
        await close_position(sym, ("sell" if is_long else "buy"), abs(s["qty"]), p, avg, reason="Silence_Zone_Knife_Catch", is_stop_loss=True)
        # 設置臨時封禁 1 小時
        s["status"] = "BANNED"
        s["next_status_time"] = time.time() + 3600
        s["status_reason"] = "接刀防護 (進場即跳水)，封禁 1 小時"
        return
"""
if "# --- [優化] 進場後靜默區" not in content:
    pattern_exit = r'(hold_sec = time\.time\(\) - s\["open_time"\] if s\["open_time"\] > 0 else 9999\n)'
    content = re.sub(pattern_exit, r'\1' + silence_zone_logic, content)

# Fix 2: Trend Consistency (MACD 擴張要求)
# In is_entry_allowed, change:
# route_a_long = ( macd_hist > 0 and ... ) -> route_a_long = ( macd_hist > 0 and macd_hist > prev_macd_hist and ... )
# route_a_short = ( macd_hist < 0 and ... ) -> route_a_short = ( macd_hist < 0 and macd_hist < prev_macd_hist and ... )

if "macd_hist > prev_macd_hist" not in content.split('route_a_long = (')[1]:
    content = content.replace(
        "route_a_long = (\n        macd_hist > 0 and",
        "route_a_long = (\n        macd_hist > 0 and \n        macd_hist > prev_macd_hist and"
    )
    content = content.replace(
        "route_a_short = (\n        macd_hist < 0 and",
        "route_a_short = (\n        macd_hist < 0 and \n        macd_hist < prev_macd_hist and"
    )

# Fix 3: Dynamic SL Lower Bound
# Replace `p * 0.005` with `p * 0.012` in sl_dist calculations.
content = content.replace("max(atr_val * sl_multiplier, p * 0.005)", "max(atr_val * sl_multiplier, p * 0.012)")
content = content.replace("max(sl_mult * atr_val, avg * 0.005)", "max(sl_mult * atr_val, avg * 0.012)")
content = content.replace("max(atr_val * sl_multiplier, price * 0.005)", "max(atr_val * sl_multiplier, price * 0.012)")


# Fix 4: Blacklist System
# Track SL counts in 24 hours. The current code has `stop_count` and `first_stop_time` in `mark_exit`.
# Let's see: `s["stop_count"] >= MAX_STOPS_IN_WINDOW and (now - s["first_stop_time"]) <= BAN_WINDOW`
# Currently it's doing something similar!
# Wait, let's find `MAX_STOPS_IN_WINDOW` in content to see if it's already there but configured wrong.
"""
MAX_STOPS_IN_WINDOW = 3 # Original value? We will make it 5 stops in 24 hours (86400).
BAN_WINDOW = 3600
BAN_DURATION = 14400 # 4h
"""
# Let's adjust these global variables to match User's request: "5 times in 24 hours -> ban".
# So: MAX_STOPS_IN_WINDOW = 5, BAN_WINDOW = 86400, BAN_DURATION = 86400
content = re.sub(r'MAX_STOPS_IN_WINDOW\s*=\s*\d+', 'MAX_STOPS_IN_WINDOW = 5', content)
content = re.sub(r'BAN_WINDOW\s*=\s*\d+', 'BAN_WINDOW = 86400', content)
content = re.sub(r'BAN_DURATION\s*=\s*\d+', 'BAN_DURATION = 86400', content)

with open(target_file, 'w', encoding='utf-8') as f:
    f.write(content)

print("Done applying fixes.")
