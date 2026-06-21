import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

old_vol = """    # 動態量能門檻：嚴格模式
    vol_factor = s.get("volume_threshold_factor", 1.1)
    if side == 'sell':
        vol_factor = 1.1"""
new_vol = """    # 動態量能門檻：放寬模式 (由 1.1/1.2 調降至 1.0)
    vol_factor = s.get("volume_threshold_factor", 1.0)
    if side == 'sell':
        vol_factor = 1.0"""
code = code.replace(old_vol, new_vol)

with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Relaxed volume threshold to 1.0")
