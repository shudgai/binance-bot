import json

with open("paper_state.json", "r") as f:
    data = json.load(f)

# 1. 修正 balance_usdt (退還第二筆手續費)
fee_to_refund = 0.022496020000000002
data["balance_usdt"] += fee_to_refund

# 2. 修正 positions (移除第二筆 entry，將 qty 改回單筆)
link_pos = data["positions"]["LINK:USDT"]
link_pos["qty"] = -5.96
if len(link_pos["entries"]) == 2:
    link_pos["entries"].pop()

# 3. 修正 trades (移除最後一筆重複的交易)
if len(data["trades"]) > 0 and data["trades"][-1]["symbol"] == "LINK:USDT":
    data["trades"].pop()

with open("paper_state.json", "w") as f:
    json.dump(data, f, indent=4)
