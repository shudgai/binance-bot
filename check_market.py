import asyncio
import ccxt.pro as ccxtpro

API_KEY = "你的key"
API_SECRET = "你的secret"

# 想测哪个就把对应的 SANDBOX 改成 True/False
SANDBOX = False  # True = 测试网, False = 正式网

# 想确认的交易对，可以多放几个一起测
SYMBOLS_TO_CHECK = ["SOL/USDT", "SOL/USDT:USDT", "BTC/USDT:USDT"]


async def main():
    exchange = ccxtpro.binance({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "options": {
            "defaultType": "future",  # 测合约用 future/swap，测现货改成 spot
        },
    })

    if SANDBOX:
        exchange.set_sandbox_mode(True)
        print(">>> 当前模式: 测试网 (sandbox)")
    else:
        print(">>> 当前模式: 正式网 (mainnet)")

    try:
        await exchange.load_markets()
        print(f">>> 成功加载市场，共 {len(exchange.markets)} 个交易对")

        for symbol in SYMBOLS_TO_CHECK:
            exists = symbol in exchange.markets
            print(f"    - {symbol}: {'存在 ✅' if exists else '不存在 ❌'}")

        try:
            balance = await exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            print(f">>> Key 验证成功，USDT 余额信息: {usdt}")
        except Exception as e:
            print(f">>> Key 验证失败: {e}")

    except Exception as e:
        print(f">>> 加载市场失败: {e}")

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
