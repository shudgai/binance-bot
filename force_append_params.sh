#!/bin/bash

TARGET_FILE="multi_coin_bot.py"

if [ ! -f "$TARGET_FILE" ]; then
    echo "❌ 錯誤：找不到 $TARGET_FILE"
    exit 1
fi

echo "🚀 正在強制將參數追加到檔案末尾..."

# 直接將參數塊追加到檔案最後一行
cat << 'PARAMS_EOF' >> "$TARGET_FILE"

# --- 強制注入參數 (Fallback) ---
min_entry_score = 13.0
min_profit_room = 0.015
pullback_rsi_long = (35.0, 50.0)
pullback_rsi_short = (50.0, 60.0)
price_offset = 0.001
max_slots = 2
leverage = 5
order_timeout = 45
max_hold_time = 14400
PARAMS_EOF

echo "✅ 參數已追加至檔案末尾。"
echo "🔍 驗證結果："
grep "pullback_rsi_long" "$TARGET_FILE"
