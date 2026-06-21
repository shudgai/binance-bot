import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

# 1. Trailing SL Sync
old_sl = """    else:
        sl = avg - sl_dist if is_long else avg + sl_dist"""
new_sl = """    else:
        sl = avg - sl_dist if is_long else avg + sl_dist

    # --- 停損同步 (Trailing SL Sync) ---
    if s.get("entry_count", 0) > 0:
        if is_long:
            sl = max(sl, avg * 1.0015)
        else:
            sl = min(sl, avg * 0.9985)"""
code = code.replace(old_sl, new_sl)

# 2. Max Notional Cap (30%)
old_notional = """    max_notional = 1000.0"""
new_notional = """    max_notional = min(1000.0, total_balance * 0.3)"""
code = code.replace(old_notional, new_notional)

# 3. Increase safe margin buffer to 20%
old_buffer = """    safe_free_usdt = max(0.0, free_usdt - (total_balance * 0.1))"""
new_buffer = """    safe_free_usdt = max(0.0, free_usdt - (total_balance * 0.2))"""
code = code.replace(old_buffer, new_buffer)

# 4 & 5. Slope/Momentum Expansion Check & Hard Max Entry Count (execute_order)
old_entry = """        if s["entry_count"] >= s["max_additional_entries"]:
            print(f"⚠️ [加倉上限] {sym} 已達最大加倉次數")
            return"""
new_entry = """        if s["entry_count"] >= 3:
            print(f"⚠️ [加倉上限] {sym} 已達絕對層數上限 (3層)")
            return
            
        # 動能斜率判斷: 最近兩根K線的漲跌幅度是否縮小
        if len(s.get("ohlcv", [])) >= 3:
            c1 = s["ohlcv"][-2]  # 最新已收盤 K 線
            c2 = s["ohlcv"][-3]  # 前一根已收盤 K 線
            body1 = abs(c1[4] - c1[1])
            body2 = abs(c2[4] - c2[1])
            vol1 = c1[5]
            vol2 = c2[5]
            
            is_bull1 = c1[4] > c1[1]
            is_bull2 = c2[4] > c2[1]
            
            if side == 'buy' and is_bull1 and is_bull2 and body1 < body2 * 0.8 and vol1 < vol2 * 0.8:
                print(f"🛑 [斜率過濾] {sym} 價格創高但實體與量能雙雙衰減，動能不足拒絕加碼！")
                return
            if side == 'sell' and not is_bull1 and not is_bull2 and body1 < body2 * 0.8 and vol1 < vol2 * 0.8:
                print(f"🛑 [斜率過濾] {sym} 價格創低但實體與量能雙雙衰減，動能不足拒絕加碼！")
                return"""
code = code.replace(old_entry, new_entry)


with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Applied Defense Mods")
