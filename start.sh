#!/bin/bash
# start.sh — Launch bot + API server
BIN=/home/shudgai999/project/binance-bot-live/venv/bin
CONFIG_FILE=/home/shudgai999/project/binance-bot-live/.entry_mode

ENTRY_MODE="${ENTRY_STRICTNESS_MODE:-relaxed}"
if [ -f "$CONFIG_FILE" ]; then
  FILE_MODE="$(tr -d '[:space:]' < "$CONFIG_FILE")"
  if [ -n "$FILE_MODE" ]; then
    ENTRY_MODE="$FILE_MODE"
  fi
fi

if [ -n "$ENTRY_STRICTNESS_MODE" ]; then
  ENTRY_MODE="$ENTRY_STRICTNESS_MODE"
fi

export ENTRY_STRICTNESS_MODE="$ENTRY_MODE"

# 機器人本體改由 API 啟動時的受監控流程拉起（services/bot_manager_service.py 的
# _startup_radar_restore + read_bot_output），這樣才有「意外停止 5 秒後自動重啟」的保護。
# 這裡直接呼叫 main.py 反而繞過了那套監控，機器人掛掉不會自動救回來。
echo "🌐 Starting API server on port 8006 (機器人將由 API 啟動流程自動拉起，含異常自動重啟)..."
$BIN/uvicorn services.api:app --host 0.0.0.0 --port 8006
