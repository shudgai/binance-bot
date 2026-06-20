#!/bin/bash
cd /home/shudgai999/project/binance-bot || exit 1
LOG_FILE="api_run.log"

pkill -f "uvicorn api:app" 2>/dev/null
pkill -f "python api.py" 2>/dev/null
sleep 1

nohup venv/bin/uvicorn api:app --host 0.0.0.0 --port 8005 >> "$LOG_FILE" 2>&1 &
echo "✅ API 後端已啟動"
