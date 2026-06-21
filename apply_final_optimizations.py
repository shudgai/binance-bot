import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# Fix 1: Dynamic Space Floor based on Personality
old_space = "if price_diff < max(1.5 * current_atr, price * 0.003):"
new_space = """        # 動態空間門檻 (依幣種性格)
        personality = s.get("personality", "balanced")
        if personality in ["trend_follower", "breakout_chaser"]: # Core_Trend
            floor_pct = 0.004
        elif personality in ["mean_reversion", "contrarian"]: # Speculative
            floor_pct = 0.002
        else: # High_Beta / Balanced
            floor_pct = 0.003
        if price_diff < max(1.5 * current_atr, price * floor_pct):"""
code = code.replace(old_space, new_space)

# Fix 3: Post-Stop Loss Pause in mark_exit
old_mark_exit = """def mark_exit(sym, is_stop_loss=False, reason=""):
    s = STATES[sym]
    now = time.time()
    s["status"] = "COOLDOWN"
    s["next_status_time"] = now + COOLDOWN_SEC
    s["status_reason"] = f"冷卻中 (5分鐘) - {reason}"
    print(f"⏳ [狀態] {sym} 平倉 ({reason}) ({reason}) → COOLDOWN 5分鐘")"""

new_mark_exit = """def mark_exit(sym, is_stop_loss=False, reason=""):
    s = STATES[sym]
    now = time.time()
    s["status"] = "COOLDOWN"
    
    # 動態靜默期：一般平倉 5 分鐘，停損 30 分鐘
    actual_cooldown = 1800 if is_stop_loss else 300
    s["next_status_time"] = now + actual_cooldown
    
    cd_min = actual_cooldown // 60
    s["status_reason"] = f"冷卻中 ({cd_min}分鐘) - {reason}"
    print(f"⏳ [狀態] {sym} 平倉 ({reason}) → COOLDOWN {cd_min}分鐘")"""
code = code.replace(old_mark_exit, new_mark_exit)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied Final Optimizations successfully!")
