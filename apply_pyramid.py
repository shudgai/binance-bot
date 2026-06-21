import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

old_add_logic = """    if s["entry_count"] > 0:
        if now - s["last_entry_time"] < s["entry_cooldown_sec"]:
            print(f"⏳ [加倉冷卻] {sym} 距離上次加倉不足 {s['entry_cooldown_sec']} 秒")
            return
        if s["entry_count"] >= s["max_additional_entries"]:
            print(f"⚠️ [加倉上限] {sym} 已達最大加倉次數")
            return
        if s["avg_price"] > 0 and s["close_price"] > 0:
            profit_pct = (s["close_price"] - s["avg_price"]) / s["avg_price"] if side == 'buy' else (s["avg_price"] - s["close_price"]) / s["avg_price"]
            if profit_pct < 0.001:
                print(f"🛑 [加倉風控] {sym} 目前尚未回到保本線以上，不加倉 (利潤: {profit_pct*100:.2f}%)")
                return"""

new_add_logic = """    if s["entry_count"] > 0:
        if now - s["last_entry_time"] < s["entry_cooldown_sec"]:
            print(f"⏳ [加倉冷卻] {sym} 距離上次加倉不足 {s['entry_cooldown_sec']} 秒")
            return
        if s["entry_count"] >= s["max_additional_entries"]:
            print(f"⚠️ [加倉上限] {sym} 已達最大加倉次數")
            return
            
        # 1. 空間關卡 (Space Check): 距離上一次加倉是否大於 1.5 * ATR
        current_atr = s.get("current_atr", 0.0)
        last_entry_price = s.get("last_entry_price", s.get("avg_price", 0.0))
        if last_entry_price > 0 and current_atr > 0:
            price_diff = abs(price - last_entry_price)
            if price_diff < 1.5 * current_atr:
                print(f"🛑 [空間關卡] {sym} 加倉距離不足! 差距: {price_diff:.4f} < 門檻: {1.5 * current_atr:.4f}")
                return
                
        # 2. 動能關卡 (Momentum Check): 量能與 MACD 雙重確認
        if not is_entry_volume_confirmed(sym, side):
            print(f"🛑 [動能關卡] {sym} 量能不足以支持加倉!")
            return
            
        macd_hist = s.get("macd_hist", 0.0)
        if (side == 'buy' and macd_hist <= 0) or (side == 'sell' and macd_hist >= 0):
            print(f"🛑 [動能關卡] {sym} MACD動能不一致 (Hist: {macd_hist:.4f})，拒絕加倉!")
            return

        # 3. 原有的保本檢查
        if s["avg_price"] > 0 and s["close_price"] > 0:
            profit_pct = (s["close_price"] - s["avg_price"]) / s["avg_price"] if side == 'buy' else (s["avg_price"] - s["close_price"]) / s["avg_price"]
            if profit_pct < 0.001:
                print(f"🛑 [保本關卡] {sym} 目前尚未回到保本線以上，不加倉 (利潤: {profit_pct*100:.2f}%)")
                return"""

code = code.replace(old_add_logic, new_add_logic)

# Replace the margin checks in execute_order
old_margin_check_1 = """    # 檢查可用餘額 (Free Balance)
    if not PAPER_TRADING:
        try:
            bal = await exchange_futures.fetch_balance()
            free_usdt = float(bal.get("USDT", {}).get("free", 0.0))
            if margin > free_usdt:
                print(f"⚠️ [資金保護] {sym} 計算出的保證金 {margin:.2f} 大於可用餘額 {free_usdt:.2f}，自動降至可用餘額！")
                margin = free_usdt * 0.95
        except Exception as e:
            print(f"⚠️ [餘額檢查失敗] {e}")"""

# Remove the initial margin check, we will do it comprehensively at the end.
code = code.replace(old_margin_check_1, "")

old_margin_check_2 = """    # 3. 最大名義價值限制 (防極端爆倉)
    max_notional = 1000.0  # 絕對最大名義價值 1000 USDT
    if base_notional > max_notional:
        base_notional = max_notional
        
    # 4. 真實可用餘額二次檢查
    # 如果要求的保證金 (名義價值 / 槓桿) 大於當前真實可用餘額，則向下修正
    balance = get_balance()
    required_margin = base_notional / lev
    if required_margin > balance * 0.98:
        base_notional = (balance * 0.98) * lev"""

new_margin_check_2 = """    # 3. 最大名義價值限制與風險關卡 (Risk Check)
    max_notional = 1000.0  # 絕對最大名義價值 1000 USDT
    if base_notional > max_notional:
        base_notional = max_notional
        
    # 4. 資金關卡與餘額檢查 (Capital Check)
    balance = get_balance()
    required_margin = base_notional / lev
    
    if not PAPER_TRADING:
        try:
            bal = await exchange_futures.fetch_balance()
            free_usdt = float(bal.get("USDT", {}).get("free", 0.0))
            # 資金關卡：確保加倉後系統依然保留總資金 10% 的可用餘額做為緩衝
            safe_free_usdt = max(0.0, free_usdt - (balance * 0.1))
            if required_margin > safe_free_usdt:
                print(f"⚠️ [資金關卡] {sym} 扣除 10% 緩衝後，安全餘額 {safe_free_usdt:.2f} < 所需保證金 {required_margin:.2f}，自動降至安全餘額下單！")
                base_notional = safe_free_usdt * lev
        except Exception as e:
            print(f"⚠️ [餘額檢查失敗] {e}")
    else:
        if required_margin > balance * 0.98:
            base_notional = (balance * 0.98) * lev"""

code = code.replace(old_margin_check_2, new_margin_check_2)

# Save s["last_entry_price"]
old_save_state = """            s["open_time"] = now
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["entry_count"] += 1"""

new_save_state = """            s["open_time"] = now
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["last_entry_price"] = price
            s["entry_count"] += 1"""

code = code.replace(old_save_state, new_save_state)

with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Apply pyramid logic done.")
