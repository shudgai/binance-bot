#!/bin/bash
# start.sh — Launch bot + API server
BIN=/home/shudgai999/project/binance-bot/.venv/bin
CONFIG_FILE=/home/shudgai999/project/binance-bot/.entry_mode

ENTRY_MODE="balanced"
if [ -f "$CONFIG_FILE" ]; then
  ENTRY_MODE="$(cat "$CONFIG_FILE")"
fi

if [ -n "$ENTRY_STRICTNESS_MODE" ]; then
  ENTRY_MODE="$ENTRY_STRICTNESS_MODE"
fi

export ENTRY_STRICTNESS_MODE="$ENTRY_MODE"

echo "🚀 Starting Binance Bot with entry mode: $ENTRY_MODE"
$BIN/python main.py &

echo "🌐 Starting API server on port 8005..."
$BIN/uvicorn services.api:app --host 0.0.0.0 --port 8005

wait
