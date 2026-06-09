import asyncio
import ccxt.pro as ccxtpro

async def main():
    exchange = ccxtpro.binance({'enableRateLimit': True})
    try:
        print("Waiting for SOL/USDT kline...")
        ohlcv = await exchange.watch_ohlcv('SOL/USDT', '1m')
        print("Got klines:", len(ohlcv))
    except Exception as e:
        print(e)
    finally:
        await exchange.close()

asyncio.run(main())
