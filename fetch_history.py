import ccxt
import pandas as pd
import time
import os
from datetime import datetime, timedelta

def fetch_historical_data(symbol, timeframe, limit=1000, days=30):
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    
    since = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    all_ohlcv = []
    
    print(f"Fetching {symbol} {timeframe} data for the past {days} days...")
    
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since, limit)
            if not ohlcv:
                break
            
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            
            # Check if we've reached the current time
            if len(ohlcv) < limit:
                break
                
            time.sleep(0.5) # rate limit
        except Exception as e:
            print(f"Error fetching {symbol}: {e}")
            time.sleep(2)
            
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    # Remove duplicates if any
    df = df.drop_duplicates(subset=['timestamp'])
    return df

def main():
    os.makedirs('backtest_data', exist_ok=True)
    
    # 抓取 Altcoins 的 5m K線 (用於訊號與PrevVol計算)
    altcoins = ['WLD/USDT', 'SOL/USDT', '1000PEPE/USDT']
    for sym in altcoins:
        df = fetch_historical_data(sym, '5m', days=30)
        filename = f"backtest_data/{sym.replace('/', '')}_5m.csv"
        df.to_csv(filename, index=False)
        print(f"Saved {filename} ({len(df)} rows)")
        
    # 抓取大盤的 15m K線 (用於瀑布風控計算)
    majors = ['BTC/USDT', 'ETH/USDT']
    for sym in majors:
        df = fetch_historical_data(sym, '15m', days=30)
        filename = f"backtest_data/{sym.replace('/', '')}_15m.csv"
        df.to_csv(filename, index=False)
        print(f"Saved {filename} ({len(df)} rows)")

if __name__ == '__main__':
    main()
