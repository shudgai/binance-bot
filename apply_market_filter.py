import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# [AI Action 3] 環境過濾
old_logic = """        # 原本的計算邏輯
        side_strength = compute_signal_strength(sym)
        if side_strength[0] is None:
            continue
        side, strength, route = side_strength"""

new_logic = """        # 原本的計算邏輯
        side_strength = compute_signal_strength(sym)
        if side_strength[0] is None:
            continue
        side, strength, route = side_strength
        
        # [AI Action 3] 環境過濾：大盤多頭時禁止做空山寨幣
        if side == "sell" and MARKET_WIND.get("btc_trend") == "BULL":
            print(f"🛑 [大盤過濾] {sym} 訊號為空，但 BTC 處於上漲趨勢 (BULL)，禁止逆勢做空！")
            continue"""

code = code.replace(old_logic, new_logic)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied AI advice: Market Filter added")
