import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# 1. Space Check Relaxation
old_space = "if price_diff < 1.5 * current_atr:"
new_space = "if price_diff < 1.2 * current_atr:"
code = code.replace(old_space, new_space)

# 2. MACD Momentum Relaxation
old_macd = """        if abs(macd_hist) <= abs(prev_macd_hist):
            print(f"🛑 [動能關卡] {sym} MACD動能未擴張 (Hist: {abs(macd_hist):.5f} <= Prev: {abs(prev_macd_hist):.5f})，拒絕加倉!")
            return"""
new_macd = """        # 允許動能微幅縮減 (只要沒有大幅衰退 > 30%)
        if abs(macd_hist) < abs(prev_macd_hist) * 0.7:
            print(f"🛑 [動能關卡] {sym} MACD動能大幅衰竭 (Hist: {abs(macd_hist):.5f} < Prev: {abs(prev_macd_hist):.5f}*0.7)，拒絕加倉!")
            return"""
code = code.replace(old_macd, new_macd)

# 3. Flip Buffer Relaxation based on Exit Reason
# First, record exit reason in close_position
old_close = """    s["open_time"] = 0
    s["last_buy_time"] = 0
    s["highest_profit_pct"] = 0.0
    s["pending_side"] = None"""

new_close = """    s["open_time"] = 0
    s["last_buy_time"] = 0
    s["highest_profit_pct"] = 0.0
    s["pending_side"] = None
    s["last_exit_reason"] = reason"""
code = code.replace(old_close, new_close)

# Then relax flip buffer in check_entries (min_flip_time)
old_flip = """            min_flip = s.get("min_flip_time", 900)  # 嚴格方向鎖定：由 300 秒延長至 15 分鐘 (900秒)
            if flip_elapsed < min_flip:
                print(f"⏳ [方向鎖定] {sym} 欲 {side}，但距離上次做 {last_trade_side} 僅 {flip_elapsed:.0f}s (冷卻需 {min_flip}s)，禁止頻繁反手。")
                continue"""

new_flip = """            # 動態冷卻：如果上次是停損出場，代表趨勢已逆轉，允許更快的反手 (縮短為 60 秒)
            last_exit = s.get("last_exit_reason", "")
            is_stop_loss = "Stop" in last_exit or "Loss" in last_exit or "Trailing" in last_exit or "Momentum_Fade" in last_exit
            min_flip = 60 if is_stop_loss else s.get("min_flip_time", 900)
            
            if flip_elapsed < min_flip:
                print(f"⏳ [方向鎖定] {sym} 欲 {side}，但距離上次做 {last_trade_side} 僅 {flip_elapsed:.0f}s (冷卻需 {min_flip}s, 原因:{last_exit})，禁止頻繁反手。")
                continue"""
code = code.replace(old_flip, new_flip)

# 4. Pyramid Allocation
old_alloc = """    if s["entry_count"] == 0:
        allocation_pct = 0.50  # 首倉 50%
    elif s["entry_count"] == 1:
        allocation_pct = 0.30  # 次倉 30%
    else:
        allocation_pct = 0.20  # 再倉 20%
    base_notional = target_notional * allocation_pct"""

new_alloc = """    if s["entry_count"] == 0:
        allocation_pct = 0.40  # 首倉 40%
    elif s["entry_count"] == 1:
        allocation_pct = 0.30  # 次倉 30%
    else:
        allocation_pct = 0.30  # 再倉 30%
    base_notional = target_notional * allocation_pct
    
    # 最低加倉門檻保護 (確保滿足幣安合約最小下單金額 5~10 USDT)
    if base_notional < 10.0 and margin * lev >= 10.0:
        base_notional = 10.0"""
code = code.replace(old_alloc, new_alloc)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied 4 relaxations successfully!")
