#!/bin/bash

echo "🚀 開始自動分析幣種數據..."
python3 scripts/auto_cluster_coins.py

echo "📊 配置更新完成，啟動交易機器人..."
python3 multi_coin_bot.py
