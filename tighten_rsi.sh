#!/bin/bash

# 設定目標檔案
TARGET_FILE="multi_coin_bot.py"

# 檢查檔案是否存在
if [ ! -f "$TARGET_FILE" ]; then
    echo "❌ 錯誤：找不到 $TARGET_FILE"
    exit 1
fi

# 建立備份
cp "$TARGET_FILE" "${TARGET_FILE}.bak"
echo "✅ 已建立備份: ${TARGET_FILE}.bak"

echo "🚀 正在強制修正 pullback_rsi_long 到 (35.0, 50.0)..."

# 使用 Python 強制匹配包含 pullback_rsi_long 的行並替換
python3 -c "
import re

with open('$TARGET_FILE', 'r', encoding='utf-8') as f:
    content = f.read()

lines = content.split('\n')
new_lines = []
found = False

for line in lines:
    if 'self.pullback_rsi_long' in line:
        indent = line[:line.find('self.pullback_rsi_long')]
        new_lines.append(f'{indent}self.pullback_rsi_long = (35.0, 50.0)')
        found = True
    else:
        new_lines.append(line)

if found:
    with open('$TARGET_FILE', 'w', encoding='utf-8') as f:
        f.write('\n'.join(new_lines))
    print('✨ 修改成功！已強制更新為 (35.0, 50.0)')
else:
    print('⚠️ 警告：依然找不到包含 self.pullback_rsi_long 的行，請手動檢查檔案內容。')
"

if [ $? -eq 0 ]; then
    echo "🎉 任務完成！"
else
    echo "❌ 發生錯誤，請檢查代碼內容。"
fi
