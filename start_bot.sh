#!/bin/bash
cd /home/shudgai999/project/binance-bot
nohup ./venv/bin/python -u multi_coin_bot.py > multi_coin_bot.log 2>&1 &
echo $! > /tmp/multi_coin_bot.pid
