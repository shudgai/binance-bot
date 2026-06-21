import re

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update global HARD_STOP_LOSS_PCT and SL_ATR_MULTIPLIER
content = re.sub(r"HARD_STOP_LOSS_PCT\s*=\s*0\.02", "HARD_STOP_LOSS_PCT = 0.015", content)
content = re.sub(r"SL_ATR_MULTIPLIER\s*=\s*1\.5", "SL_ATR_MULTIPLIER = 1.2", content)

# 2. Update breakeven_threshold
content = re.sub(r"breakeven_threshold\s*=\s*max\(entry_atr_pct\s*\*\s*1\.0,\s*0\.002\)", "breakeven_threshold = max(entry_atr_pct * 0.6, 0.0015)", content)

# 3. Update personality config hard_stop_loss_pct
content = re.sub(r'"hard_stop_loss_pct": 0.03,', '"hard_stop_loss_pct": 0.02,', content)
content = re.sub(r'"hard_stop_loss_pct": 0.02,', '"hard_stop_loss_pct": 0.015,', content)
content = re.sub(r'"hard_stop_loss_pct": 0.015,', '"hard_stop_loss_pct": 0.01,', content)

# 4. Update specific coins' sl_atr_multiplier and tp_atr_multiplier to make them smaller
def adjust_coin_config(match):
    conf = match.group(0)
    # Extract current SL and TP
    sl_match = re.search(r'"sl_atr_multiplier":\s*([\d\.]+)', conf)
    tp_match = re.search(r'"tp_atr_multiplier":\s*([\d\.]+)', conf)
    if sl_match and tp_match:
        sl = float(sl_match.group(1))
        tp = float(tp_match.group(1))
        # Reduce by roughly 25-30%
        new_sl = round(sl * 0.75, 1)
        new_tp = round(tp * 0.75, 1)
        # Apply min thresholds
        new_sl = max(1.2, new_sl)
        new_tp = max(2.4, new_tp)
        conf = re.sub(r'"sl_atr_multiplier":\s*[\d\.]+', f'"sl_atr_multiplier": {new_sl}', conf)
        conf = re.sub(r'"tp_atr_multiplier":\s*[\d\.]+', f'"tp_atr_multiplier": {new_tp}', conf)
    return conf

content = re.sub(r'".+USDT":\s*\{.*?"sl_atr_multiplier":.+?\}', adjust_coin_config, content)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Update complete")
