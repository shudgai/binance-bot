import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

old_loop = """    for i in range(min(remaining_slots, len(candidates))):
        sym, side, _, route = candidates[i]
        s = STATES[sym]
        print(f"⚡ [即時開倉] {sym} 觸發訊號 ({route} 路線)，即刻下單！")
        await execute_order(sym, side, s["close_price"])
        s["pending_side"] = None
        s["pending_confirm_high"] = 0
        s["pending_confirm_low"] = 0"""

new_loop = """    for sym, side, strength, route in candidates:
        s = STATES[sym]
        has_pos = abs(s["qty"]) > 0.000001
        
        if not has_pos:
            if remaining_slots <= 0:
                continue
            remaining_slots -= 1
            print(f"⚡ [即時開倉] {sym} 觸發訊號 ({route} 路線)，即刻首倉進場！")
        else:
            print(f"⚡ [順勢加倉] {sym} 觸發加倉訊號 ({route} 路線)，準備執行加碼！")
            
        await execute_order(sym, side, s["close_price"])
        s["pending_side"] = None
        s["pending_confirm_high"] = 0
        s["pending_confirm_low"] = 0"""

code = code.replace(old_loop, new_loop)

with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Fixed Pyramid Execution Loop")
