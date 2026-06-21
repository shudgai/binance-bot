import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

old_check_exits = """    if hold_sec < 120:
        return

    # 1. 取得 ATR 停利停損倍數"""

new_check_exits = """    if hold_sec < 120:
        return

    # [防插針與連續洗盤保護] 如果在 5 分鐘內已經發生過平倉/停損，暫停非緊急出單
    if time.time() - s.get("last_flip_time", 0) < 300 and "Stop" in s.get("last_exit_reason", ""):
        # 給予 300 秒的緩衝期，避免被連續插針洗盤
        return

    # 1. 取得 ATR 停利停損倍數"""

code = code.replace(old_check_exits, new_check_exits)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied Fix C")
