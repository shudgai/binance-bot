#!/bin/bash
cd /home/shudgai999/project/binance-bot
pkill -f "multi_coin_bot.py"
sleep 1
nohup ./venv/bin/python -u multi_coin_bot.py > multi_coin_bot.log 2>&1 &
echo $! > /tmp/multi_coin_bot.pid
