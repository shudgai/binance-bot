#!/bin/bash
cd /home/shudgai999/project/binance-bot || exit 1
SESSION="binance_bot"
WINDOW="multi_coin_bot"
LOG_FILE="multi_coin_bot.log"
CMD="./venv/bin/python -u multi_coin_bot.py"

if command -v tmux >/dev/null 2>&1; then
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "已偵測到 tmux session '$SESSION'，先關閉舊的 session..."
        tmux kill-session -t "$SESSION"
    fi
    echo "啟動 tmux session '$SESSION'，將程式輸出寫入 $LOG_FILE..."
    tmux new-session -d -s "$SESSION" -n "$WINDOW" "cd $(pwd) && $CMD > \"$LOG_FILE\" 2>&1"
    echo "tmux session 已啟動：$SESSION"
    echo "可使用以下指令查看或連接："
    echo "  tmux ls"
    echo "  tmux attach -t $SESSION"
else
    echo "tmux 未安裝，改用 nohup 啟動。"
    nohup $CMD > "$LOG_FILE" 2>&1 &
    echo $! > /tmp/multi_coin_bot.pid
    echo "機器人已在背景啟動，PID 存於 /tmp/multi_coin_bot.pid"
fi
