s = {"qty": -10, "avg_price": 100}
base_amt = 5
price = 90

s["qty"] -= base_amt # -15
old_abs_qty = abs(s["qty"]) - base_amt
s["avg_price"] = ((100 * old_abs_qty) + (price * base_amt)) / abs(s["qty"])
print("Short:", s["avg_price"])

s = {"qty": 10, "avg_price": 100}
s["qty"] += base_amt # 15
old_abs_qty = abs(s["qty"]) - base_amt
s["avg_price"] = ((100 * old_abs_qty) + (price * base_amt)) / abs(s["qty"])
print("Long:", s["avg_price"])
