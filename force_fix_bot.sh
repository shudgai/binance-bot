#!/bin/bash

TARGET_FILE="multi_coin_bot.py"

if [ ! -f "$TARGET_FILE" ]; then
    echo "❌ 錯誤：找不到 $TARGET_FILE"
    exit 1
fi

echo "🚀 正在強制注入狙擊手參數並修正數值..."

python3 -c "
import re

with open('$TARGET_FILE', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 處理 __init__ 注入
# 尋找 def __init__(self): 這一行
init_match = re.search(r'(def __init__(self):)', content)
if init_match:
    init_line = init_match.group(1)
    # 我們要插入的參數塊
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
    # 在 init_line 後面插入參數塊
    new_content = content.replace(init_line, init_line + params_block)
else:
    new_content = content
    print('⚠️ 警告：找不到 def __init__(self): 行，可能名稱不同。')

# 2. 再次確保 pullback_rsi_long 是正確的數值
# 這裡使用正則表達式強制覆蓋該行
new_content = re.sub(r'(self\.pullback_rsi_long\s*=\s*\(.*?\))', r'\1', new_content)
# 因為上面的 regex 只是匹配，我們直接用最保險的字串替換
new_content = re.sub(r'self\.pullback_rsi_long\s*=\s*\(.*?\)', 'self.pullback_rsi_long = (35.0, 50.0)', new_content)

with open('$TARGET_FILE', 'w', encoding='utf-8') as f:
    f.write(new_content)

print('✨ 強制注入與數值修正完成！')
"

if [ $? -eq 0 ]; then
    echo "🎉 任務完成！"
else
    echo "❌ 發生錯誤，請檢查代碼內容。"
fi
