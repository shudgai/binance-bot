#!/bin/bash
cd /home/shudgai999/project/binance-bot || exit 1
LOG_FILE="api_run.log"

# 只殺死佔用 8005 埠號的 API 進程
fuser -k 8005/tcp 2>/dev/null
sleep 1

nohup venv/bin/uvicorn api:app --host 0.0.0.0 --port 8005 >> "$LOG_FILE" 2>&1 &
echo "✅ 沙盒 API 後端 (Port 8005) 已啟動"
