#!/bin/bash

# --- 設定區域 ---
PYTHON_FILE="main.py"
LOG_FILE="bot_runtime.log"
PID_FILE="bot.pid"

# --- 顏色定義 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}=== Binance Bot 自動啟動管理系統 ===${NC}"

# 檢查 python3 是否存在
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}錯誤: 系統未安裝 python3，請先安裝。${NC}"
    exit 1
fi

start_bot() {
    echo -e "${GREEN}[$(date)] 正在啟動機器人...${NC}"
    # 使用 nohup 啟動，確保變數被正確解析
    nohup python3 "$PYTHON_FILE" >> "$LOG_FILE" 2>&1 &
    BOT_PID=$!
    echo $BOT_PID > "$PID_FILE"
    echo -e "${GREEN}機器人已啟動，PID: $BOT_PID${NC}"
    echo -e "${YELLOW}所有日誌將記錄於: $LOG_FILE${NC}"
}

stop_bot() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        echo -e "${RED}[$(date)] 正在停止機器人 (PID: $PID)...${NC}"
        kill $PID 2>/dev/null
        rm "$PID_FILE"
        echo -e "${GREEN}機器人已停止。${NC}"
    else
        echo -e "${YELLOW}未偵測到正在運行的機器人 PID。${NC}"
    fi
}

monitor_bot() {
    echo -e "${YELLOW}進入自動監控模式...（若程式崩潰將自動重啟）${NC}"
    while true; do
        if [ ! -f "$PID_FILE" ]; then
            echo -e "${RED}[$(date)] 偵測到機器人已停止，正在自動重啟...${NC}"
            start_bot
        fi
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            if ! kill -0 $PID 2>/dev/null; then
                echo -e "${RED}[$(date)] 偵測到 PID $PID 已失效，正在重啟...${NC}"
                rm "$PID_FILE"
                start_bot
            fi
        fi
        sleep 30
    done
}

case "$1" in
    start)
        start_bot
        monitor_bot
        ;;
    stop)
        stop_bot
        ;;
    restart)
        stop_bot
        sleep 2
        start_bot
        monitor_bot
        ;;
    status)
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            if kill -0 $PID 2>/dev/null; then
                echo -e "${GREEN}機器人正在運行中 (PID: $PID)${NC}"
            else
                echo -e "${RED}機器人 PID 存在但已停止。${NC}"
            fi
        else
            echo -e "${RED}機器人未啟動。${NC}"
        fi
        ;;
    *)
        echo "使用方法: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
