import ccxt
import pandas as pd
import time
import os
from datetime import datetime, timedelta

def fetch_crash_period():
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    
    # 1. Fetch daily BTC data for the last 180 days
    since = int((datetime.now() - timedelta(days=180)).timestamp() * 1000)
    print("Fetching daily BTC data to find a crash...")
    daily_ohlcv = exchange.fetch_ohlcv('BTC/USDT', '1d', since, 1000)
    
    df_daily = pd.DataFrame(daily_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_daily['timestamp'] = pd.to_datetime(df_daily['timestamp'], unit='ms')
    df_daily['drop'] = (df_daily['close'] - df_daily['open']) / df_daily['open']
    
    # Find the day with the biggest drop
    crashes = df_daily[df_daily['drop'] <= -0.05].sort_values('drop')
    if crashes.empty:
        print("No >5% drop found in the last 180 days. Picking the worst day.")
        worst_day = df_daily.loc[df_daily['drop'].idxmin()]
    else:
        worst_day = crashes.iloc[0]
        
    crash_date = worst_day['timestamp']
    drop_pct = worst_day['drop'] * 100
    print(f"Found Crash Day: {crash_date} with {drop_pct:.2f}% drop.")
    
    # We will fetch data for 7 days surrounding the crash day (e.g. 3 days before to 3 days after)
    start_ts = int((crash_date - timedelta(days=3)).timestamp() * 1000)
    end_ts = int((crash_date + timedelta(days=3)).timestamp() * 1000)
    
    os.makedirs('crash_data', exist_ok=True)
    
    def fetch_historical_data(symbol, timeframe, start, end):
        all_ohlcv = []
        current_since = start
        while current_since < end:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe, current_since, 1000)
                if not ohlcv:
                    break
                # Filter out data beyond end_ts
                ohlcv = [x for x in ohlcv if x[0] <= end]
                if not ohlcv:
                    break
                all_ohlcv.extend(ohlcv)
                current_since = ohlcv[-1][0] + 1
                time.sleep(0.5)
            except Exception as e:
                print(f"Error fetching {symbol}: {e}")
                time.sleep(2)
                
        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.drop_duplicates(subset=['timestamp'])
        return df

    # Fetch 5m
    altcoins = ['WLD/USDT', 'SOL/USDT', '1000PEPE/USDT']
    for sym in altcoins:
        print(f"Fetching {sym} 5m data for crash period...")
        df = fetch_historical_data(sym, '5m', start_ts, end_ts)
        filename = f"crash_data/{sym.replace('/', '')}_5m.csv"
        df.to_csv(filename, index=False)
        print(f"Saved {filename} ({len(df)} rows)")
        
    # Fetch 15m
    majors = ['BTC/USDT', 'ETH/USDT']
    for sym in majors:
        print(f"Fetching {sym} 15m data for crash period...")
        df = fetch_historical_data(sym, '15m', start_ts, end_ts)
        filename = f"crash_data/{sym.replace('/', '')}_15m.csv"
        df.to_csv(filename, index=False)
        print(f"Saved {filename} ({len(df)} rows)")

if __name__ == '__main__':
    fetch_crash_period()
