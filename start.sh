#!/bin/bash
# start.sh — Launch bot + API server (single-instance guarded)
set -u

ROOT_DIR="/home/shudgai999/project/binance-bot"
BIN="$ROOT_DIR/.venv/bin"
CONFIG_FILE="$ROOT_DIR/.entry_mode"
API_PORT="8005"
API_CMD="$BIN/uvicorn services.api:app --host 0.0.0.0 --port $API_PORT"

cd "$ROOT_DIR" || exit 1

find_port_pids() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :$API_PORT" 2>/dev/null | awk -F'pid=' '/pid=/{split($2,a,",");print a[1]}' | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$API_PORT" -sTCP:LISTEN 2>/dev/null | sort -u
  fi
}

existing_pids="$(find_port_pids)"
if [ -n "$existing_pids" ]; then
  for pid in $existing_pids; do
    cmdline="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    if echo "$cmdline" | grep -q "$BIN/uvicorn services.api:app --host 0.0.0.0 --port $API_PORT"; then
      echo "✅ API already running on port $API_PORT (pid=$pid), skip duplicate start."
      exit 0
    fi
    echo "⚠️ Port $API_PORT occupied by pid=$pid, terminating stale process..."
    kill "$pid" 2>/dev/null || true
  done

  for _ in 1 2 3 4 5; do
    sleep 1
    [ -z "$(find_port_pids)" ] && break
  done
fi

ENTRY_MODE="${ENTRY_STRICTNESS_MODE:-relaxed}"
if [ -f "$CONFIG_FILE" ]; then
  FILE_MODE="$(tr -d '[:space:]' < "$CONFIG_FILE")"
  if [ -n "$FILE_MODE" ]; then
    ENTRY_MODE="$FILE_MODE"
  fi
fi

if [ -n "${ENTRY_STRICTNESS_MODE:-}" ]; then
  ENTRY_MODE="$ENTRY_STRICTNESS_MODE"
fi

export ENTRY_STRICTNESS_MODE="$ENTRY_MODE"

# 機器人本體改由 API 啟動時的受監控流程拉起（services/bot_manager_service.py 的
# _startup_radar_restore + read_bot_output），這樣才有「意外停止 5 秒後自動重啟」的保護。
# 這裡直接呼叫 main.py 反而繞過了那套監控，機器人掛掉不會自動救回來。
echo "🌐 Starting API server on port $API_PORT (機器人將由 API 啟動流程自動拉起，含異常自動重啟)..."
exec $API_CMD
