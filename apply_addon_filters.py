import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

old_addon_logic = """        if s["entry_count"] >= 3:
            print(f"⚠️ [加倉上限] {sym} 已達絕對層數上限 (3層)")
            return
            
        # 動能斜率判斷: 最近兩根K線的漲跌幅度是否縮小"""

new_addon_logic = """        if s["entry_count"] >= 3:
            print(f"⚠️ [加倉上限] {sym} 已達絕對層數上限 (3層)")
            return
            
        # [加倉防護 1] 虧損加倉防護
        avg_price = s.get("avg_price", 0.0)
        if avg_price > 0:
            profit_pct = (price - avg_price) / avg_price if side == 'buy' else (avg_price - price) / avg_price
            if profit_pct < 0.003:
                print(f"🛑 [虧損加倉防護] {sym} 目前利潤 {profit_pct*100:.2f}% 不足 0.3%，拒絕加倉！")
                return

        # [加倉防護 2] 價格大幅反轉過濾
        last_entry_price = s.get("last_entry_price", avg_price)
        if last_entry_price > 0:
            reversal = (last_entry_price - price) / last_entry_price if side == 'buy' else (price - last_entry_price) / last_entry_price
            if reversal > 0.01:
                print(f"🛑 [反轉過濾] {sym} 價格與上次加倉發生大幅反轉 ({reversal*100:.2f}% > 1%)，拒絕加倉！")
                return

        # [加倉防護 3] 動能一致性
        current_vol = s.get("current_vol", 0.0)
        vol_ma20 = s.get("vol_ma20", 1e-8)
        if current_vol < vol_ma20 * 0.8:
            print(f"🛑 [量能過濾] {sym} 當前量能低於均量 0.8 倍，動能不足拒絕加倉！")
            return
            
        # 動能斜率判斷: 最近兩根K線的漲跌幅度是否縮小"""

code = code.replace(old_addon_logic, new_addon_logic)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied add-on filters successfully!")
