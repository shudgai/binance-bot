import sys
import re

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# Modify check_exits: partial_tp logic
old_partial_tp = """    # 檢查是否觸發分批停利 (Partial Close at 1.5 ATR or 0.8%)
    partial_tp_dist = max(atr_val * 2.0, p * 0.012)
    partial_tp_price = avg + partial_tp_dist if is_long else avg - partial_tp_dist
    if not s.get("has_partial_closed", False) and ((is_long and p >= partial_tp_price) or (not is_long and p <= partial_tp_price)):
        half_qty = abs(s["qty"]) * 0.5
        if half_qty >= (s.get("min_qty", 0.001) if "min_qty" in s else 0.0):
            print(f"🎯 [分批停利] {sym} 觸發 1.5 ATR 或 0.8% 利潤，先平倉 50% 落袋為安")
            await close_position(sym, cs, half_qty, p, avg, reason="分批停利 50%")
            s["has_partial_closed"] = True
            # 不 return，讓剩餘倉位繼續走下面的追蹤邏輯"""

new_partial_tp = """    # 檢查是否觸發分批停利 (動態 0.5% ~ 0.8% 平一半)
    personality = s.get("personality", "balanced")
    if personality in ["trend_follower", "breakout_chaser"]: # Core_Trend / High Volatility
        tp_target_pct = 0.008
    elif personality in ["mean_reversion", "contrarian"]: # Speculative / Low Volatility
        tp_target_pct = 0.005
    else: # Balanced
        tp_target_pct = 0.006

    partial_tp_dist = max(atr_val * 1.5, p * tp_target_pct)
    partial_tp_price = avg + partial_tp_dist if is_long else avg - partial_tp_dist
    
    if not s.get("has_partial_closed", False) and ((is_long and p >= partial_tp_price) or (not is_long and p <= partial_tp_price)):
        half_qty = abs(s["qty"]) * 0.5
        if half_qty >= (s.get("min_qty", 0.001) if "min_qty" in s else 0.0):
            print(f"🎯 [階段一止盈] {sym} 獲利達標 ({tp_target_pct*100:.1f}%)，先平倉 50% 落袋為安")
            await close_position(sym, cs, half_qty, p, avg, reason="階段一止盈 50%")
            s["has_partial_closed"] = True
            s["trailing_stop_price"] = avg  # 確保剩餘倉位最差保本
            # 不 return，讓剩餘倉位繼續走下面的追蹤邏輯"""

code = code.replace(old_partial_tp, new_partial_tp)

# Modify update_trailing_stop: aggressive trailing after partial close
old_update_trailing = """def update_trailing_stop(sym, current_price, is_long):
    \"\"\"
    實作非對稱移動停損 (Asymmetric Trailing Stop)
    當價格創新高/新低時，上移停損點，且加入保本緩衝區防止被雜訊洗出場。
    \"\"\"
    s = STATES[sym]
    atr_val = s.get("current_atr", 0.0)
    if atr_val <= 0:
        return False, s["trailing_stop_price"]

    trailing_multiplier = s.get("trailing_stop_multiplier", 2.0)"""

new_update_trailing = """def update_trailing_stop(sym, current_price, is_long):
    \"\"\"
    實作非對稱移動停損 (Asymmetric Trailing Stop)
    當價格創新高/新低時，上移停損點，且加入保本緩衝區防止被雜訊洗出場。
    \"\"\"
    s = STATES[sym]
    atr_val = s.get("current_atr", 0.0)
    if atr_val <= 0:
        return False, s["trailing_stop_price"]

    # 極限追蹤止盈邏輯 (Aggressive Trailing Stop)
    if s.get("has_partial_closed", False):
        personality = s.get("personality", "balanced")
        if personality in ["trend_follower", "breakout_chaser"]:
            aggr_trail_pct = 0.004
        else:
            aggr_trail_pct = 0.003
        
        # 當已觸發階段一止盈，追蹤距離縮緊至回落 0.3%~0.4%
        trail_dist = max(atr_val * 0.5, current_price * aggr_trail_pct)
        
        if is_long:
            trail_sl = s.get("trailing_highest", current_price) - trail_dist
            s["trailing_stop_price"] = max(s.get("trailing_stop_price", 0.0), trail_sl)
        else:
            trail_sl = s.get("trailing_lowest", current_price) + trail_dist
            if s.get("trailing_stop_price", 0.0) == 0.0:
                s["trailing_stop_price"] = trail_sl
            else:
                s["trailing_stop_price"] = min(s.get("trailing_stop_price", 0.0), trail_sl)
        return False, s["trailing_stop_price"]

    trailing_multiplier = s.get("trailing_stop_multiplier", 2.0)"""

code = code.replace(old_update_trailing, new_update_trailing)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied dynamic trailing stop and partial TP logic successfully!")
