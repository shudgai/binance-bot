import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# 1. Update the space check to correctly log the max threshold
old_space_check = """        if price_diff < max(1.5 * current_atr, price * floor_pct):
                print(f"🛑 [空間關卡] {sym} 加倉距離不足! 差距: {price_diff:.4f} < 門檻: {1.5 * current_atr:.4f}")
                return"""

new_space_check = """        threshold = max(1.5 * current_atr, price * floor_pct)
        if price_diff < threshold:
                print(f"🛑 [空間關卡] {sym} 加倉距離不足! 差距: {price_diff:.4f} < 門檻: {threshold:.4f}")
                return"""

code = code.replace(old_space_check, new_space_check)

# 2. Dynamic breakeven after order execution
# We need to insert it in the PAPER_TRADING block and the REAL_TRADING block after entry_count is incremented
old_paper_finish = """            s["entry_count"] += 1
            direction = "做多" if side == 'buy' else "做空"
            print(f"🟢 [{direction}] {sym} {base_amt:.4f} @ {price} (保證金:{margin:.2f} USDT)")"""

new_paper_finish = """            s["entry_count"] += 1
            
            # --- 動態保本上推 (Dynamic Breakeven) ---
            if s["entry_count"] > 1:
                new_avg = s["avg_price"]
                if side == 'buy':
                    new_breakeven = new_avg * 1.001
                    s["trailing_stop_price"] = max(s.get("trailing_stop_price", 0.0), new_breakeven)
                else:
                    new_breakeven = new_avg * 0.999
                    if s.get("trailing_stop_price", 0.0) == 0.0:
                        s["trailing_stop_price"] = new_breakeven
                    else:
                        s["trailing_stop_price"] = min(s.get("trailing_stop_price", 0.0), new_breakeven)
                print(f"🛡️ [動態停損] {sym} 加倉成功，停損點移至 {s['trailing_stop_price']:.6f} 確保保本")
                
            direction = "做多" if side == 'buy' else "做空"
            print(f"🟢 [{direction}] {sym} {base_amt:.4f} @ {price} (保證金:{margin:.2f} USDT)")"""

code = code.replace(old_paper_finish, new_paper_finish)

old_real_finish = """            s["entry_count"] += 1
            s["last_flip_time"] = now"""

new_real_finish = """            s["entry_count"] += 1
            s["last_flip_time"] = now
            
            # --- 動態保本上推 (Dynamic Breakeven) ---
            if s["entry_count"] > 1:
                new_avg = s["avg_price"]
                if side == 'buy':
                    new_breakeven = new_avg * 1.001 # 涵蓋手續費
                    s["trailing_stop_price"] = max(s.get("trailing_stop_price", 0.0), new_breakeven)
                else:
                    new_breakeven = new_avg * 0.999
                    if s.get("trailing_stop_price", 0.0) == 0.0:
                        s["trailing_stop_price"] = new_breakeven
                    else:
                        s["trailing_stop_price"] = min(s.get("trailing_stop_price", 0.0), new_breakeven)
                print(f"🛡️ [動態停損] {sym} 實盤加倉成功，停損點移至 {s['trailing_stop_price']:.6f} 確保保本")"""

code = code.replace(old_real_finish, new_real_finish)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied Pyramid Sizing Fixes and Dynamic Breakeven successfully!")
