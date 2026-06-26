#!/bin/bash
BOT_SCRIPT="heavy_dual_shot_core.py"
LOG_FILE="bot.log"
PID_FILE="/tmp/binance_bot_bash.pid"
PYTHON_EXEC="/home/shudgai999/project/binance-bot/venv/bin/python3"

CDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$CDIR"

start_bot() {
    echo "=== 🚀 正在啟動重裝雙發量化機器人 ==="
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "⚠️  警告：守護進程已在執行中 (PID: $OLD_PID)"
            return 1
        fi
    fi
    (
        echo "$$" > "$PID_FILE"
        while true; do
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🤖 機器人主程序啟動..." >> "$LOG_FILE"
            $PYTHON_EXEC -u "$BOT_SCRIPT" >> "$LOG_FILE" 2>&1
            EXIT_CODE=$?
            # exit code 0 = 正常退出（防禦分流或手動停止）→ 不重啟
            if [ "$EXIT_CODE" -eq 0 ]; then
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ 機器人已正常退出 (code 0)，守護進程停止。" >> "$LOG_FILE"
                rm -f "$PID_FILE"
                break
            fi
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚨 機器人異常退出，錯誤碼: $EXIT_CODE，5秒後重啟..." >> "$LOG_FILE"
            sleep 5
        done
    ) &
    echo "✅ 機器人已成功在背景啟動！"
}

stop_bot() {
    echo "=== 🛑 正在停止機器人與守護進程 ==="
    if [ -f "$PID_FILE" ]; then
        BASH_PID=$(cat "$PID_FILE")
        kill "$BASH_PID" 2>/dev/null
        rm -f "$PID_FILE"
    fi
    PY_PID=$(ps aux | grep "$BOT_SCRIPT" | grep -v grep | awk '{print $2}')
    if [ ! -z "$PY_PID" ]; then
        kill -15 "$PY_PID" 2>/dev/null
        sleep 1
        kill -9 "$PY_PID" 2>/dev/null
    fi
    rm -f "/tmp/binance_bot_v2.lock"
    echo "✨ 機器人已安全關閉。"
}

case "$1" in
    start) start_bot ;;
    stop) stop_bot ;;
    restart) stop_bot; sleep 2; start_bot ;;
    tail) tail -n 50 -f "$LOG_FILE" ;;
    *) echo "💡 使用說明: $0 {start|stop|restart|tail}" ;;
esac
