import sys

with open('multi_coin_bot.py', 'r') as f:
    content = f.read()

# 1. Imports
content = content.replace('import csv\n\nload_dotenv()', 'import csv\nfrom services.exit_manager import ExitManager\n\nload_dotenv()')

# 2. exit_mgr init
content = content.replace('    "PEPEUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0}\n}\n\nALL_SYMBOLS = list(COIN_PROFILE_CONFIG.keys())', '    "PEPEUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0}\n}\n\nexit_mgr = ExitManager(COIN_PROFILE_CONFIG)\n\nALL_SYMBOLS = list(COIN_PROFILE_CONFIG.keys())')

# 3. Layer 0 replace
layer_0_old = """    # ==========================================
    # Waterfall Logic (5-Layer Defense System)
    # ==========================================

    # ── 底層防護：硬停損 (Hard Stop Loss) & 動態保本 ──
    sl_base = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long)
    sl_mult = sl_base * 1.5 if hold_sec < 120 else sl_base
    atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)
    sl_dist = min(max(sl_mult * atr_val, avg * 0.005), avg * 0.012)
    
    entry_atr_pct = (s.get("entry_atr", atr_val) / avg) if avg > 0 else 0.002
    breakeven_threshold = max(entry_atr_pct * 1.0, 0.004) # 保本門檻提高到 0.4%
    if s.get("highest_profit_pct", 0.0) >= breakeven_threshold:
        sl = avg * 1.0015 if is_long else avg * 0.9985
    else:
        sl = avg - sl_dist if is_long else avg + sl_dist

    if (is_long and p <= sl) or (not is_long and p >= sl):
        reason_str = "Layer_0_Breakeven_Stop" if abs(sl - (avg * 1.0015 if is_long else avg * 0.9985)) < 1e-6 else "Layer_0_Hard_Stop"
        print(f"🛑 [硬防護] {sym} 觸發底層停損 ({reason_str})")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason_str, is_stop_loss=True)
        return"""

layer_0_new = """    # ==========================================
    # ── ExitManager 底層防線 (MMP與硬停損) ──
    # ==========================================
    macd_is_down = s.get("macd_line", 0) < s.get("macd_signal", 0)
    macd_is_up = s.get("macd_line", 0) > s.get("macd_signal", 0)
    trend_reversed = (is_long and macd_is_down) or (not is_long and macd_is_up)
    
    position_data = {
        "qty": s["qty"],
        "avg_price": s["avg_price"],
        "open_time": s["open_time"]
    }
    market_data = {
        "current_price": p,
        "current_atr": s.get("current_atr", 0.0),
        "trend_reversed": trend_reversed
    }
    
    decision = exit_mgr.check_exit_conditions(sym, position_data, market_data)
    
    if decision["should_exit"]:
        is_stop_loss = "STOP_LOSS" in decision["reason"]
        qty_to_close = abs(s["qty"])
        if decision["exit_type"] == "PARTIAL_50":
            qty_to_close *= 0.5
            s["trade_status"] = "PARTIAL_EXIT"
            
        print(f"🛑 [ExitManager] {sym} 觸發平倉: {decision['reason']} ({decision['exit_type']})")
        await close_position(sym, cs, qty_to_close, p, avg, reason=decision['reason'], is_stop_loss=is_stop_loss)
        return
        
    if "BELOW_MMP" in decision["reason"]:
        # 未達最小意義獲利門檻，且未觸發硬停損或僵局，攔截後續進階邏輯
        return

    # ==========================================
    # Waterfall Logic (Layer 1-4 Defense System)
    # =========================================="""

content = content.replace(layer_0_old, layer_0_new)

# 4. Layer 5 replace
layer_5_old = """    # ── Layer 5: 時間防禦與分批平倉 (Time Defense & Partial Exit) ──
    trade_status = s.get("trade_status", "NORMAL")
    if trade_status == "NORMAL":
        # 5.1 50% 分批停利 (Partial Take Profit)
        if net_profit_pct >= tier2_target:
            half_qty = abs(s["qty"]) * 0.5
            print(f"💰 [Layer_5] {sym} 淨利達標 (>=2.5ATR)，市價平倉 50% 落袋為安！")
            await close_position(sym, cs, half_qty, p, avg, reason="Layer_5_Partial_TP")
            s["trade_status"] = "PARTIAL_EXIT"
            return
            
        # 5.2 盤整時間防禦 (Stagnation)
        stagnation_limit = get_dynamic_stagnation_limit(s.get("current_atr", atr_val), s.get("atr_ma20", current_atr))
        # 強化果斷性：如果持倉超過 5 分鐘 (300 秒) 或動態上限
        actual_stagnation_limit = min(stagnation_limit, 300)
        
        if hold_sec > actual_stagnation_limit:
            if net_profit_pct < 0.001: 
                print(f"⏳ [Layer_5] {sym} 僵局盤整過久且無法獲利，無效波動直接斬倉")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_5_Stagnation_Kill")
                s["highest_profit_pct"] = 0.0
                return
            elif 0.001 <= net_profit_pct <= 0.005:
                # 在 0.1% ~ 0.5% 之間直接全平，不分批了！
                print(f"⏳ [Layer_5] {sym} 僵局盤整超過 5 分鐘，微利 ({net_profit_pct*100:.2f}%) 直接全平釋放資金")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_5_Stagnation_Full_MicroProfit")
                s["highest_profit_pct"] = 0.0
                return
            elif net_profit_pct > 0.005:
                print(f"⏳ [Layer_5] {sym} 僵局盤整過久，獲利尚可，直接全平落袋")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_5_Stagnation_Full")
                s["highest_profit_pct"] = 0.0
                return

    elif trade_status == "PARTIAL_EXIT":
        # 已經平過 50%，如果卡了超過 8 分鐘且獲利不佳，全跑
        if hold_sec > 480 and net_profit_pct < 0.01:
            print(f"⏳ [Layer_5] {sym} 剩餘倉位盤整過久，全數平倉")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_5_Stagnation_Remaining")
            s["highest_profit_pct"] = 0.0
            return"""

layer_5_new = """    # ── Layer 5: 時間防禦與分批平倉 (Time Defense & Partial Exit) ──
    # 注意：Stalemate (盤整過久) 的防禦已經交由 ExitManager 處理
    trade_status = s.get("trade_status", "NORMAL")
    if trade_status == "NORMAL":
        # 5.1 50% 分批停利 (Partial Take Profit)
        if net_profit_pct >= tier2_target:
            half_qty = abs(s["qty"]) * 0.5
            print(f"💰 [Layer_5] {sym} 淨利達標 (>=2.5ATR)，市價平倉 50% 落袋為安！")
            await close_position(sym, cs, half_qty, p, avg, reason="Layer_5_Partial_TP")
            s["trade_status"] = "PARTIAL_EXIT"
            return"""

content = content.replace(layer_5_old, layer_5_new)

with open('multi_coin_bot.py', 'w') as f:
    f.write(content)

print("Patch applied.")
