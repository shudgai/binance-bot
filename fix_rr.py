import re

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. 降低 SL_ATR_MULTIPLIER，讓初始停損變小
content = re.sub(r'SL_ATR_MULTIPLIER\s*=\s*[\d\.]+', 'SL_ATR_MULTIPLIER = 1.0', content)

# 2. 提高 weak_tp 避免太早停利
content = re.sub(r'weak_tp\s*=\s*0\.008', 'weak_tp = 0.015', content)
content = re.sub(r'weak_tp\s*=\s*0\.012', 'weak_tp = 0.02', content)

# 3. 提高 partial_tp_dist (分批停利) 的門檻，從 1.5 ATR 提高到 2.0 ATR
content = re.sub(r'partial_tp_dist\s*=\s*max\(atr_val\s*\*\s*1\.5,\s*p\s*\*\s*0\.008\)', 'partial_tp_dist = max(atr_val * 2.0, p * 0.012)', content)

# 4. 修改鎖利防護的回撤比例 (讓利潤不要回吐太多才平倉)
# tier3_target 回撤從 0.6 改為 0.8 (保留 80% 利潤)
content = re.sub(r'profit_pct\s*<\s*s\["highest_profit_pct"\]\s*\*\s*\(0\.6\s*if\s*is_trend_ok\s*else\s*0\.4\)', 'profit_pct < s["highest_profit_pct"] * (0.8 if is_trend_ok else 0.6)', content)

# tier2_target 回撤從 0.5 改為 0.7 (保留 70% 利潤)
content = re.sub(r'profit_pct\s*<\s*s\["highest_profit_pct"\]\s*\*\s*\(0\.5\s*if\s*is_trend_ok\s*else\s*0\.3\)', 'profit_pct < s["highest_profit_pct"] * (0.7 if is_trend_ok else 0.5)', content)

# tier1_target 回撤改為保留 50% 利潤，而不是固定的 max(atr_pct * 0.5, 0.0015)
content = re.sub(r'profit_pct\s*<\s*\(max\(atr_pct\s*\*\s*0\.5,\s*0\.0015\)\)', 'profit_pct < s["highest_profit_pct"] * 0.5', content)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Risk-Reward Fix complete")
