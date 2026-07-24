#!/bin/bash
# start_8007.sh — Launch Port 8007 High-Frequency Scalping Bot + API Server
BIN=/home/shudgai999/project/binance-bot/.venv/bin

export ENTRY_STRICTNESS_MODE="relaxed"
export MIN_TREND_ADX="15.0"
export MAX_SIGNAL_CHASE_PCT="0.0010"
export SCALP_MODE="true"
export SCALP_TP1_PCT="0.0025"
export SCALP_TP2_PCT="0.010"
export HARD_STOP_LOSS_PCT="0.015"
export PORT="8007"
export FOLLOW_SYMBOLS_FROM="$(pwd)/data/bot_symbols.json"

if [ -f ".env.8007" ]; then
  source .env.8007
fi

echo "🌐 Starting Port 8007 High-Frequency Scalping API server (+0.5% TP / -1.5% SL)..."
$BIN/uvicorn services.api:app --host 0.0.0.0 --port 8007
