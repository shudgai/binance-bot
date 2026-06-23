import re

with open("multi_coin_bot.py", "r") as f:
    content = f.read()

# 1. Inject ADX into compute_indicators
adx_inject = """        s["prev_macd_signal"] = p_sig
    if len(closes) >= 15:
        s["adx"] = calculate_adx(highs, lows, closes, 14)"""
content = content.replace('        s["prev_macd_signal"] = p_sig', adx_inject)

# 2. Inject get_dynamic_cooldown and check_pyramiding_eligibility
cooldown_code = """
def get_dynamic_cooldown(current_atr, avg_atr, adx_value, base_cooldown=15):
    volatility_ratio = current_atr / avg_atr if avg_atr > 0 else 1.0
    vol_factor = 1.0 + (max(0, volatility_ratio - 1.0) * 0.5)

    if adx_value > 30:
        trend_factor = 0.8
    elif adx_value < 20:
        trend_factor = 1.5
    else:
        trend_factor = 1.0

    dynamic_cooldown = base_cooldown * vol_factor * trend_factor
    return max(5, min(60, round(dynamic_cooldown)))

def check_pyramiding_eligibility(s):
    if not s.get('entries'):
        return False, 0

    last_entry = s['entries'][-1]
    last_entry_time = last_entry['time']
    
    current_atr = s.get('current_atr', 0.0)
    avg_atr = s.get('atr_ma20', current_atr)
    adx_value = s.get('adx', 25.0)

    dynamic_cooldown_mins = get_dynamic_cooldown(current_atr, avg_atr, adx_value)
    
    current_time = time.time()
    seconds_passed = current_time - last_entry_time
    minutes_passed = seconds_passed / 60
    
    is_cooldown_over = minutes_passed >= dynamic_cooldown_mins
    is_under_max_layers = len(s['entries']) < 3
    
    if is_cooldown_over and is_under_max_layers:
        price_gap = abs(s['close_price'] - s.get('avg_price', s['close_price'])) / s.get('avg_price', s['close_price'])
        if price_gap < 0.05:
            return True, dynamic_cooldown_mins
            
    return False, dynamic_cooldown_mins

async def check_entries():"""

content = content.replace("async def check_entries():", cooldown_code)

# 3. Inject pyramiding check inside check_entries
target_block = """        # --- 方向鎖定 (Direction Lock) 與 高門檻自動反手 ---
        if has_position and side != current_direction:
            if await is_eligible_for_reverse(sym, strength):"""

replace_block = """        # --- 方向鎖定 (Direction Lock) 與 高門檻自動反手 ---
        if has_position:
            if side != current_direction:
                if await is_eligible_for_reverse(sym, strength):
                    print(f"⚡ [AUTOMATIC_REVERSE] {sym} 觸發強勢反轉，執行平倉並反手建立 {side} 倉位")
                    await close_position(sym, current_direction, abs(s["qty"]), s["close_price"], s["avg_price"], reason="[AUTOMATIC_REVERSE]")
                    await asyncio.sleep(1)
                    await create_orders(sym, side, s["close_price"])
                    continue
                else:
                    continue
            else:
                # 金字塔加倉邏輯 (順勢加碼)
                is_eligible, cooldown_mins = check_pyramiding_eligibility(s)
                if not is_eligible:
                    print(f"⏳ [加碼防禦] {sym} 欲順勢加倉 {side}，但未達動態冷卻 ({cooldown_mins}m) 或已達上限，攔截加碼")
                    continue

        if not is_entry_allowed(sym, side, route):"""

# Because replacing that whole block is tricky with regex, we can replace the exact lines:
old_lines = """        # --- 方向鎖定 (Direction Lock) 與 高門檻自動反手 ---
        if has_position and side != current_direction:
            if await is_eligible_for_reverse(sym, strength):
                print(f"⚡ [AUTOMATIC_REVERSE] {sym} 觸發強勢反轉，執行平倉並反手建立 {side} 倉位")
                # 執行平倉
                await close_position(sym, current_direction, abs(s["qty"]), s["close_price"], s["avg_price"], reason="[AUTOMATIC_REVERSE]")
                # 安全性強制校準
                await asyncio.sleep(1)
                # 執行反向開倉
                await create_orders(sym, side, s["close_price"])
                continue
            else:
                # 已經有持倉，不允許反向訊號加倉且未達反手門檻
                continue

        if not is_entry_allowed(sym, side, route):"""

content = content.replace(old_lines, replace_block)

with open("multi_coin_bot.py", "w") as f:
    f.write(content)
