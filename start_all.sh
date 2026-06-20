#!/bin/bash

echo "--- 🚀 啟動自動化交易系統 ---"

# 1. 自動擴展新幣種配置
echo "🔄 正在同步新幣種配置..."
venv/bin/python3 scripts/sync_new_coins.py

# 2. 執行每日 AI 策略優化 (如果數據足夠)
echo "🧠 正在執行 AI 策略診斷..."
venv/bin/python3 ai_strategy_optimizer.py

# 3. 啟動主機器人
echo "🚀 機器人啟動中..."
venv/bin/python3 multi_coin_bot.py
