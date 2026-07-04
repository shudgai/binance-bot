#!/bin/bash
# stop.sh — Stop bot + API server
echo "🛑 Stopping Binance Bot..."
pkill -f 'main\.py' 2>/dev/null
echo "🛑 Stopping API server..."
pkill -f '/home/shudgai999/project/binance-bot/.venv/bin/uvicorn services.api:app --host 0.0.0.0 --port 8005' 2>/dev/null
pkill -f 'uvicorn services.api:app --host 0.0.0.0 --port 8005' 2>/dev/null
# Clean up lock file
rm -f /tmp/binance_bot_32f2e2ed.lock
echo "✅ Done"
