#!/bin/bash
# start.sh — Launch bot + API server
BIN=/home/shudgai999/project/binance-bot/.venv/bin

echo "🚀 Starting Binance Bot..."
$BIN/python main.py &

echo "🌐 Starting API server on port 8005..."
$BIN/uvicorn services.api:app --host 0.0.0.0 --port 8005

wait
