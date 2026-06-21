import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

# 1. MACD Expanding check
old_macd = """        macd_hist = s.get("macd_hist", 0.0)
        if (side == 'buy' and macd_hist <= 0) or (side == 'sell' and macd_hist >= 0):
            print(f"🛑 [動能關卡] {sym} MACD動能不一致 (Hist: {macd_hist:.4f})，拒絕加倉!")
            return"""
            
new_macd = """        macd_line = s.get("macd_line", 0.0)
        macd_signal = s.get("macd_signal", 0.0)
        prev_macd_line = s.get("prev_macd_line", 0.0)
        prev_macd_signal = s.get("prev_macd_signal", 0.0)
        macd_hist = macd_line - macd_signal
        prev_macd_hist = prev_macd_line - prev_macd_signal
        
        # 確保方向一致
        if (side == 'buy' and macd_hist <= 0) or (side == 'sell' and macd_hist >= 0):
            print(f"🛑 [動能關卡] {sym} MACD動能不一致 (Hist: {macd_hist:.4f})，拒絕加倉!")
            return
            
        # 確保動能擴張 (MACD 柱線絕對值變長)
        if abs(macd_hist) <= abs(prev_macd_hist):
            print(f"🛑 [動能關卡] {sym} MACD動能未擴張 (Hist: {abs(macd_hist):.5f} <= Prev: {abs(prev_macd_hist):.5f})，拒絕加倉!")
            return"""
code = code.replace(old_macd, new_macd)

# 2. Decreasing Allocation
old_alloc = """    # 2. 套用性格分配比例 (首倉 vs 加倉)
    allocation_pct = s["entry_size_pct"] if s["entry_count"] == 0 else s["add_entry_pct"]"""
new_alloc = """    # 2. 遞減式金字塔加倉比例 (Decreasing Allocation)
    if s["entry_count"] == 0:
        allocation_pct = 0.50  # 首倉 50%
    elif s["entry_count"] == 1:
        allocation_pct = 0.30  # 次倉 30%
    else:
        allocation_pct = 0.20  # 再倉 20%"""
code = code.replace(old_alloc, new_alloc)

# 3. 20% Margin Buffer fix
old_buffer = """            safe_free_usdt = max(0.0, free_usdt - (balance * 0.1))
            if required_margin > safe_free_usdt:
                print(f"⚠️ [資金關卡] {sym} 扣除 10% 緩衝後，安全餘額 {safe_free_usdt:.2f} < 所需保證金 {required_margin:.2f}，自動降至安全餘額下單！")"""
new_buffer = """            safe_free_usdt = max(0.0, free_usdt - (balance * 0.2))
            if required_margin > safe_free_usdt:
                print(f"⚠️ [資金關卡] {sym} 扣除 20% 緩衝後，安全餘額 {safe_free_usdt:.2f} < 所需保證金 {required_margin:.2f}，自動降至安全餘額下單！")"""
code = code.replace(old_buffer, new_buffer)

with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Applied Pyramid Fixes")
