import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta

async def main():
    exchange = ccxt.binance({'enableRateLimit': True})
    klines = await exchange.fetch_ohlcv('SIREN/USDT', timeframe='1h', limit=100)
    print(f"Got {len(klines)} klines")
    if len(klines) > 0:
        df = pd.DataFrame(klines, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        print(df['close'].tail())
    await exchange.close()

asyncio.run(main())
