import ccxt
import json
import os

# 自動讀取當前目錄下的 config.json
with open('config.json', 'r') as f:
    config = json.load(f)

# 初始化交易所物件
exchange = ccxt.binance({
    'apiKey': config['api_key'],
    'secret': config['api_secret'],
    'options': {'defaultType': 'future'}
})

# 獲取餘額並列印
try:
    balance = exchange.fetch_balance()
    print(balance['total']['USDT'])
except Exception as e:
    # 如果出錯，將錯誤資訊印到 stderr，避免干擾變數賦值
    import sys
    print(f"Error: {e}", file=sys.stderr)
