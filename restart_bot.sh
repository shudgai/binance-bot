#!/bin/bash
cd /home/shudgai999/project/binance-bot || exit 1
./stop_bot.sh
sleep 1
./start_bot.sh
