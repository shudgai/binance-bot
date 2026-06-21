import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# Modification 1 & 2: Increase breakeven threshold and trailing distance
old_logic = """    if profit_dist >= sl_dist:
        # 達到 1:1，首先確保停損位移到保本點 (含 0.25% 手續費與滑價緩衝)
        breakeven_sl = avg * 1.0025 if is_long else avg * 0.9975
        
        # 接著，如果利潤繼續拉開，使用 1.5 * ATR 進行追蹤止損
        trail_dist = atr_val * 1.5
        trail_sl = p - trail_dist if is_long else p + trail_dist"""

new_logic = """    if profit_dist >= sl_dist * 1.5:
        # 達到 1.5 倍風險距離才啟動保本/追蹤，確保停損位移到保本點 (含 0.25% 手續費與滑價緩衝)
        breakeven_sl = avg * 1.0025 if is_long else avg * 0.9975
        
        # 接著，如果利潤繼續拉開，使用 2.2 * ATR 進行追蹤止損，給予更大的呼吸空間
        trail_dist = atr_val * 2.2
        trail_sl = p - trail_dist if is_long else p + trail_dist"""

code = code.replace(old_logic, new_logic)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied dynamic SL modifications!")
