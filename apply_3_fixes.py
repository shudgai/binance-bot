import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# Fix A: Minimum Space Floor
old_space = "if price_diff < 1.2 * current_atr:"
new_space = "if price_diff < max(1.2 * current_atr, price * 0.002):"
code = code.replace(old_space, new_space)

# Fix B: Entry Cooldown
old_cooldown = 's["entry_cooldown_sec"] = 90'
new_cooldown = 's["entry_cooldown_sec"] = 180'
code = code.replace(old_cooldown, new_cooldown)

# Fix C: Whipsaw Anti-Spam (Exit Frequency Limit)
# In check_exits, we can check if it was recently stopped out or we can just limit the COOLDOWN logic.
# Wait, if the issue was consecutive exits, we don't need to change check_exits if we fix the double stop loss bug.
# But let's add a frequency limit in check_exits just in case.
old_check_exits = """async def check_exits(sym):
    s = STATES[sym]
    if s["status"] != "ACTIVE":
        return"""

new_check_exits = """async def check_exits(sym):
    s = STATES[sym]
    if s["status"] != "ACTIVE":
        return
        
    # [防插針與連續洗盤保護] 如果在 5 分鐘內已經平倉過，禁止再次觸發非緊急平倉 (這裡以防有殘留倉位被連環洗)
    now = time.time()
    if now - s.get("last_flip_time", 0) < 300 and s.get("status_reason", "").startswith("冷卻"):
        # This is just a safeguard
        pass"""

# Let's apply just A and B first, they address the user's specific request.
with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied Fix A and B")
