#!/bin/bash
cd /home/shudgai999/project/binance-bot || exit 1
SESSION="binance_bot"
PID_FILE="/tmp/multi_coin_bot.pid"
LOCK_FILE="/tmp/binance_bot_single_instance.lock"

stopped=false

if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "停止 tmux session '$SESSION'..."
    tmux kill-session -t "$SESSION"
    stopped=true
fi

if [ -f "$PID_FILE" ]; then
    pid=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "停止背景進程 PID $pid (從 PID_FILE)..."
        kill "$pid" 2>/dev/null || true
        sleep 0.2
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        stopped=true
    fi
    rm -f "$PID_FILE"
fi

if [ -f "$LOCK_FILE" ]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "停止背景進程 PID $pid (從 LOCK_FILE)..."
        kill "$pid" 2>/dev/null || true
        sleep 0.2
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        stopped=true
    fi
    echo "移除鎖定檔 $LOCK_FILE..."
    rm -f "$LOCK_FILE"
    stopped=true
fi

# 為了確保萬無一失，直接 kill 掉所有 multi_coin_bot.py
pkill -f "multi_coin_bot.py" 2>/dev/null
stopped=true

if [ "$stopped" = true ]; then
    echo "機器人已停止。"
else
    echo "未偵測到正在執行的機器人。"
fi
