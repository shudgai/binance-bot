import re

with open("multi_coin_bot.py", "r") as f:
    content = f.read()

new_functions = """
async def fetch_bb_4h(exchange, sym):
    try:
        async with request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '4h', limit=50)
        if not ohlcv or len(ohlcv) == 0:
            return None, None
        closes = np.array([x[4] for x in ohlcv])
        mbb, upper, lower = calculate_bollinger_bands(closes, 20, 2)
        return float(upper[-1]), float(lower[-1])
    except Exception as e:
        print(f"⚠️ [4H BB獲取失敗] {sym}: {e}")
        return None, None

async def fetch_all_bb_4h(exchange):
    tasks = [fetch_bb_4h(exchange, sym) for sym in ALL_SYMBOLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            upper, lower = results[i]
            if upper is not None and lower is not None:
                STATES[sym]["bb_upper_4h"] = upper
                STATES[sym]["bb_lower_4h"] = lower
"""

content = content.replace("async def load_open_positions():", new_functions + "\nasync def load_open_positions():")

# Now add `await fetch_all_bb_4h(exchange)` inside `background_mtf_updater` loop
update_loop = """            await fetch_all_ema20_15m(exchange)
            await fetch_all_ema50_1h(exchange)"""
new_update_loop = """            await fetch_all_ema20_15m(exchange)
            await fetch_all_ema50_1h(exchange)
            await fetch_all_bb_4h(exchange)"""
content = content.replace(update_loop, new_update_loop)

# Also add to initialization `main()` where `await fetch_all_ema50_1h(exchange_futures)` is called
init_call = """    await fetch_all_ema20_15m(exchange_futures)
    await fetch_all_ema50_1h(exchange_futures)"""
new_init_call = """    await fetch_all_ema20_15m(exchange_futures)
    await fetch_all_ema50_1h(exchange_futures)
    await fetch_all_bb_4h(exchange_futures)"""
content = content.replace(init_call, new_init_call)

with open("multi_coin_bot.py", "w") as f:
    f.write(content)
