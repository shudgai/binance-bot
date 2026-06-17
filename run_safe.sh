#!/bin/bash

# 定義路徑
APP_DIR="/home/shudgai999/project/binance-bot"
LOCK_FILE="/tmp/trading_pause.lock"
PID_FILE="/tmp/bot.pid"

cd "$APP_DIR"

# 1. 檢查冷卻保護
if [ -f "$LOCK_FILE" ]; then
    PAUSE_UNTIL=$(cat "$LOCK_FILE")
    CURRENT_TIME=$(date +%s)
    if [ "$CURRENT_TIME" -lt "$PAUSE_UNTIL" ]; then
        echo "🚨 [系統保護] 機器人處於冷卻中，禁止啟動。"
        exit 1
    fi
fi

# 2. 記憶體預防性檢查
FREE_MEM=$(free -m | awk '/^Mem:/{print $7}')
if [ "$FREE_MEM" -lt 200 ]; then
    echo "🚨 [記憶體警告] 當前可用記憶體僅 ${FREE_MEM}MB，停止啟動。"
    exit 1
fi

# 3. 啟動機器人
echo "🚀 [系統啟動] 安全檢查通過..."
./venv/bin/python multi_coin_bot.py &
echo $! > "$PID_FILE"

wait $!
