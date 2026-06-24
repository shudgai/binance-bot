#!/bin/bash

TARGET_FILE="multi_coin_bot.py"

if [ ! -f "$TARGET_FILE" ]; then
    echo "❌ 錯誤：找不到 $TARGET_FILE"
    exit 1
fi

echo "🚀 正在執行終極強效注入..."

python3 -c "
import re

with open('$TARGET_FILE', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 尋找 __init__ 函數的定義行 (不管括號內有什麼內容)
# 這個正則表達式會匹配任何包含 def __init__ 的行
init_match = re.search(r'def\s+__init__\(.*?\):', content)

if init_match:
    init_line = init_match.group(0)
    # 我們要注入的參數塊 (包含縮排)
    # 注意：我們自動偵測原本的縮排
    indent = '    ' # 預設 4 個空格
    
    # 如果 init_line 前面有縮排，我們就用那個縮排
    line_start_index = content.find(init_line)
    actual_indent = content[max(0, line_start_index-10):line_start_index].replace(' ', '')
    # 簡單處理：我們直接用 8 個空格作為注入縮排，通常 class 內的函數是 8 個空格
    params_block = \"\"\"
        self.min_entry_score = 13.0
        self.min_profit_room = 0.015  # 已微調為 1.5%
        
        # --- 新增：狙擊手專屬參數 ---
        self.pullback_rsi_long = (35.0, 50.0) 
        self.pullback_rsi_short = (50.0, 60.0)
        self.price_offset = 0.001 
        # ----------------------------
        
        self.max_slots = 2
        self.leverage = 5
        self.order_timeout = 45
        self.max_hold_time = 4 * 3600
\"\"\"
    
    # 在 init_line 後面插入
    new_content = content.replace(init_line, init_line + params_block)
else:
    new_content = content
    print('⚠️ 警告：依然找不到任何包含 __init__ 的行。')

# 2. 強制覆蓋 pullback_rsi_long 的數值 (不管它現在是什麼)
# 這個正則會抓到 self.pullback_rsi_long = (任何內容)
new_content = re.sub(r'(self\.pullback_rsi_long\s*=\s*\(.*?\))', r'\1', new_content)
# 因為上面的 regex 是為了抓到整行，我們直接用最保險的「包含字串」替換
# 找到包含 pullback_rsi_long 的那一行，並將其替換為標準格式
lines = new_content.split('\n')
final_lines = []
for line in lines:
    if 'self.pullback_rsi_long' in line:
        # 保留該行的縮排
        indent = line[:line.find('self.pullback_rsi_long')]
        final_lines.append(f'{indent}self.pullback_rsi_long = (35.0, 50.0)')
    else:
        final_lines.append(line)

# 如果上面迴圈沒抓到，就用最初的 new_content
if len(final_lines) == len(lines):
    final_content = new_content
else:
    final_content = '\n'.join(final_lines)

with open('$TARGET_FILE', 'w', encoding='utf-8') as f:
    f.write(final_content)

print('✨ 強制注入與數值修正完成！')
"

if [ $? -eq 0 ]; then
    echo "🎉 任務完成！"
else
    echo "❌ 發生錯誤，請檢查代碼內容。"
fi
