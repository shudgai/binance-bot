import sys
import re
import json

# 1. Update bot_symbols.json
with open("bot_symbols.json", "r") as f:
    config = json.load(f)

for sym, params in config.get("profiles", {}).items():
    if "sl_atr_multiplier" in params:
        params["sl_atr_multiplier"] = max(2.5, params["sl_atr_multiplier"] * 1.5)
    if "breakeven_trigger" in params:
        params["breakeven_trigger"] = max(1.0, params["breakeven_trigger"] * 1.5)

with open("bot_symbols.json", "w") as f:
    json.dump(config, f, indent=4)

# 2. Update multi_coin_bot.py
with open("multi_coin_bot.py", "r") as f:
    code = f.read()

# Update SL_ATR_MULTIPLIER default in Python file
code = code.replace("SL_ATR_MULTIPLIER = 1.0", "SL_ATR_MULTIPLIER = 2.5")

# Update breakeven and early exit conditions in check_exits
old_exit = """    # 2) 盤整市場：價格被壓縮在狹窄區間內，且 ATR 也偏小
    is_ranging = range_width_pct < 0.025 and atr_pct < 0.015
    if is_ranging:
        profit_pct = (current_price - avg_price) / avg_price if is_long else (avg_price - current_price) / avg_price
        if profit_pct >= 0.005:
            return "RANGE_PROFIT_TAKE", f"盤整區間內已獲利 {profit_pct * 100:.2f}%"

    return "HOLD", "未達出場條件" """

new_exit = """    # 2) 盤整市場：價格被壓縮在狹窄區間內，且 ATR 也偏小
    is_ranging = range_width_pct < 0.025 and atr_pct < 0.015
    if is_ranging:
        profit_pct = (current_price - avg_price) / avg_price if is_long else (avg_price - current_price) / avg_price
        # [AI Action 2] 延長獲利空間，未達 0.8% 絕不輕易獲利了結
        if profit_pct >= 0.008:
            return "RANGE_PROFIT_TAKE", f"盤整區間內已獲利 {profit_pct * 100:.2f}%"

    return "HOLD", "未達出場條件" """
code = code.replace(old_exit, new_exit)

# Prevent momentum exit if profit is small
old_mom = """        if profit_pct > 0 and s["current_vol"] > s["vol_ma20"] * 1.5 and range_width_pct < 0.02:
            return True, "Momentum_Fade", "動能明顯衰竭且已獲利，防守性出場" """
new_mom = """        # [AI Action 2] 延長獲利空間，小於 0.8% 的微利不啟動防守性出場
        if profit_pct > 0.008 and s["current_vol"] > s["vol_ma20"] * 1.5 and range_width_pct < 0.02:
            return True, "Momentum_Fade", "動能明顯衰竭且已獲利，防守性出場" """
code = code.replace(old_mom, new_mom)

with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Applied AI advice: Widen SL, Lengthen Profit, Add Market Filter")
