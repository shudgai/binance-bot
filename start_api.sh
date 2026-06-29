#!/bin/bash
cd /home/shudgai999/project/binance-bot-live || exit 1
LOG_FILE="api_run.log"

# 只殺死佔用 8006 埠號的 API 進程，不干擾沙盒 8005
fuser -k 8006/tcp 2>/dev/null
sleep 1

nohup venv/bin/uvicorn api:app --host 0.0.0.0 --port 8006 >> "$LOG_FILE" 2>&1 &
echo "✅ 實體 API 後端 (Port 8006) 已啟動"
