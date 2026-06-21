import re

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Relax ATR Choppiness
content = re.sub(r'current_atr\s*<\s*atr_24h_avg\s*\*\s*0\.4', 'current_atr < atr_24h_avg * 0.25', content)
content = re.sub(r'放寬至允許 40% 的極低波動', '放寬至允許 25% 的極低波動', content)

# 2. Relax BB width
content = re.sub(r'bb_width_pct\s*<\s*0\.003', 'bb_width_pct < 0.0015', content)
content = re.sub(r'放寬至布林帶寬度 0\.3%', '放寬至布林帶寬度 0.15%', content)

# 3. Relax Volume confirmed
content = re.sub(r'vol_factor\s*=\s*s\.get\("volume_threshold_factor",\s*0\.8\)', 'vol_factor = s.get("volume_threshold_factor", 0.5)', content)
content = re.sub(r'vol_factor\s*=\s*1\.2\s*#\s*嚴格要求空單必須大於 20MA 的 1\.2 倍', 'vol_factor = 0.8  # 空單要求大於 20MA 的 0.8 倍', content)
content = re.sub(r'維持原本 0\.8', '降至 0.5', content)

# 4. Relax ADX
content = re.sub(r'adx_val\s*<\s*8', 'adx_val < 5', content)
content = re.sub(r'放寬 ADX 趨勢強度門檻', '大幅放寬 ADX 趨勢強度門檻', content)

# 5. Relax min volume
content = re.sub(r'min_volume\s*=\s*s\["vol_ma20"\]\s*\*\s*0\.1', 'min_volume = s["vol_ma20"] * 0.05', content)

# 6. Relax Signal Strength volume check
content = re.sub(r'current_vol\s*<\s*vol_ma10\s*\*\s*0\.3', 'current_vol < vol_ma10 * 0.15', content)
content = re.sub(r'放寬至 0\.3 倍均量即可通過', '放寬至 0.15 倍均量即可通過', content)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Relax entry complete")
