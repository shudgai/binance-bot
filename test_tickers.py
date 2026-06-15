import asyncio
import ccxt.async_support as ccxt

async def main():
    ex = ccxt.binance({'options':{'defaultType':'swap'}})
    t = await ex.fetch_tickers(['BTCUSDT'])
    print(t.keys())
    await ex.close()

asyncio.run(main())
