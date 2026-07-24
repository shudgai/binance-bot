#!/bin/bash
# start.sh — Launch Port 8005 Paper Trading Bot + API Server
BIN=/home/shudgai999/project/binance-bot/.venv/bin

export PORT="8005"
export FOLLOW_SYMBOLS_FROM="$(pwd)/data/bot_symbols.json"

echo "🌐 Starting Port 8005 Paper Trading API server..."
$BIN/uvicorn services.api:app --host 0.0.0.0 --port 8005
