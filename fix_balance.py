import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

old_code = """    # 3. 最大名義價值限制與風險關卡 (Risk Check)
    max_notional = min(1000.0, balance * 0.3)  # 絕對最大名義價值 1000 USDT
    if base_notional > max_notional:
        base_notional = max_notional
        
    # 4. 資金關卡與餘額檢查 (Capital Check)
    balance = get_balance()
    required_margin = base_notional / lev"""

new_code = """    # 3. 最大名義價值限制與風險關卡 (Risk Check)
    balance = get_balance()
    max_notional = min(1000.0, balance * 0.3)  # 絕對最大名義價值 1000 USDT
    if base_notional > max_notional:
        base_notional = max_notional
        
    # 4. 資金關卡與餘額檢查 (Capital Check)
    required_margin = base_notional / lev"""

code = code.replace(old_code, new_code)

with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Fixed balance UnboundLocalError")
