import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

# 1. Remove local 'import numpy as np' which causes UnboundLocalError
code = code.replace("            import numpy as np\n", "")

# 2. Fix 'total_balance' NameError
code = code.replace("max_notional = min(1000.0, total_balance * 0.3)", "max_notional = min(1000.0, balance * 0.3)")

with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Fixed np and total_balance bugs")
