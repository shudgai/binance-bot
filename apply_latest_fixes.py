import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# Fix 1: Update space check to 1.5 ATR with 0.3% floor
old_space = "if price_diff < max(1.2 * current_atr, price * 0.002):"
new_space = "if price_diff < max(1.5 * current_atr, price * 0.003):"
if old_space in code:
    code = code.replace(old_space, new_space)
else:
    # Fallback if it was still 1.2
    code = code.replace("if price_diff < 1.2 * current_atr:", new_space)

# Fix 2: Direction confirmation
old_dir = """        # 1. 空間關卡 (Space Check): 距離上一次加倉是否大於 1.5 * ATR"""
new_dir = """        # [加倉防護 4] 方向確認 (確保不在逆勢接刀)
        if len(s.get("ohlcv", [])) >= 2:
            current_close = s["ohlcv"][-1][4]
            prev_close = s["ohlcv"][-2][4]
            if side == 'buy' and current_close <= prev_close:
                print(f"🛑 [方向確認] {sym} 多單加倉失敗，當前收盤價未高於前K線，拒絕接刀！")
                return
            if side == 'sell' and current_close >= prev_close:
                print(f"🛑 [方向確認] {sym} 空單加倉失敗，當前收盤價未低於前K線，拒絕接刀！")
                return

        # 1. 空間關卡 (Space Check): 距離上一次加倉是否大於 1.5 * ATR"""
code = code.replace(old_dir, new_dir)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied latest fixes successfully!")
