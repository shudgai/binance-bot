s = {"qty": -10, "avg_price": 100}
base_amt = 5
price = 90
s["avg_price"] = ((s["avg_price"] * abs(s["qty"] - base_amt)) + (price * base_amt)) / abs(s["qty"])
print(s["avg_price"])
