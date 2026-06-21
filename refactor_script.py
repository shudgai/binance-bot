import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

# 1. Semaphore
if "request_semaphore =" not in code:
    code = code.replace("WATCH_TASKS = {}", "WATCH_TASKS = {}\nrequest_semaphore = asyncio.Semaphore(5)")

# Replace fetch_all_klines implementation
old_fetch_klines = """async def fetch_all_klines(exchange):
    tasks = {}
    for sym in ALL_SYMBOLS:
        tasks[sym] = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=100)
    results = await asyncio.gather(*[tasks[sym] for sym in ALL_SYMBOLS], return_exceptions=True)"""
new_fetch_klines = """async def fetch_all_klines(exchange):
    async def fetch_with_sem(sym):
        async with request_semaphore:
            return await exchange.fetch_ohlcv(sym, TIMEFRAME, limit=100)
            
    tasks = {sym: fetch_with_sem(sym) for sym in ALL_SYMBOLS}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)"""
code = code.replace(old_fetch_klines, new_fetch_klines)

old_fetch_sma = """async def fetch_sma200_15m(exchange, sym):
    try:
        ohlcv = await exchange.fetch_ohlcv(sym, '15m', limit=200)"""
new_fetch_sma = """async def fetch_sma200_15m(exchange, sym):
    try:
        async with request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '15m', limit=200)"""
code = code.replace(old_fetch_sma, new_fetch_sma)

old_fetch_ema = """async def fetch_ema50_1h(exchange, sym):
    try:
        ohlcv = await exchange.fetch_ohlcv(sym, '1h', limit=100)"""
new_fetch_ema = """async def fetch_ema50_1h(exchange, sym):
    try:
        async with request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '1h', limit=100)"""
code = code.replace(old_fetch_ema, new_fetch_ema)

# 2. WATCH_TASKS Clean up
old_watch_tasks_clean = """    for sym in current_symbols - desired_symbols:
        task = WATCH_TASKS.pop(sym, None)
        if task:
            task.cancel()"""
new_watch_tasks_clean = """    for sym in list(current_symbols - desired_symbols):
        task = WATCH_TASKS.pop(sym, None)
        if task:
            task.cancel()
            asyncio.create_task(handle_task_cleanup(task))"""
code = code.replace(old_watch_tasks_clean, new_watch_tasks_clean)

task_cleanup_func = """
async def handle_task_cleanup(task):
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"⚠️ [任務清理異常] {e}")

async def update_watch_tasks"""
if "handle_task_cleanup" not in code:
    code = code.replace("async def update_watch_tasks", task_cleanup_func)


# 3. adjusted_this_tick robustness
close_pos_def = "async def close_position(sym, close_side, qty, price, avg_price, reason=\"\", is_stop_loss=False):"
close_pos_inner = """async def close_position(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = STATES[sym]
    s["adjusted_this_tick"] = True
    try:
        await _close_position_inner(sym, close_side, qty, price, avg_price, reason, is_stop_loss)
    finally:
        s["adjusted_this_tick"] = False

async def _close_position_inner(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):"""
if "_close_position_inner" not in code:
    code = code.replace(close_pos_def, close_pos_inner)
    code = code.replace('    s["adjusted_this_tick"] = True', '', 1)


# 4 & 5: fetch_balance & max_qty in execute_order
# Let's see how `compute_per_coin_margin` is used.
# "在 execute_order 之前，應呼叫 fetch_balance 並檢查 USDT 的 free 欄位，而非僅僅檢查 total。"
# Since `execute_order` is async, we can await fetch_balance there.
exec_order_old = """async def execute_order(sym, side, price):
    s = STATES[sym]
    pk = paper_key(sym)
    lev = get_symbol_leverage(sym)
    s["leverage"] = lev
    print(f"@@LEVERAGE@@{lev}")
    if not PAPER_TRADING:
        try:
            await exchange_futures.set_leverage(lev, convert_to_ccxt_symbol(sym))
        except Exception as e:
            print(f"⚠️ [槓桿設定失敗] {sym}: {e}")
    margin = compute_per_coin_margin(sym)"""

exec_order_new = """async def execute_order(sym, side, price):
    s = STATES[sym]
    pk = paper_key(sym)
    lev = get_symbol_leverage(sym)
    s["leverage"] = lev
    print(f"@@LEVERAGE@@{lev}")
    if not PAPER_TRADING:
        try:
            await exchange_futures.set_leverage(lev, convert_to_ccxt_symbol(sym))
        except Exception as e:
            print(f"⚠️ [槓桿設定失敗] {sym}: {e}")
            
    margin = compute_per_coin_margin(sym)
    
    # 檢查可用餘額 (Free Balance)
    if not PAPER_TRADING:
        try:
            bal = await exchange_futures.fetch_balance()
            free_usdt = float(bal.get("USDT", {}).get("free", 0.0))
            if margin > free_usdt:
                print(f"⚠️ [資金保護] {sym} 計算出的保證金 {margin:.2f} 大於可用餘額 {free_usdt:.2f}，自動降至可用餘額！")
                margin = free_usdt * 0.95
        except Exception as e:
            print(f"⚠️ [餘額檢查失敗] {e}")
"""
if "bal = await exchange_futures.fetch_balance()" not in code:
    code = code.replace(exec_order_old, exec_order_new)

# max_qty hard limit
old_base_amt = "base_amt = round_step(margin / price, prec[\"step_size\"])"
new_base_amt = """base_amt = round_step(margin / price, prec["step_size"])
        
        # 增加 max_qty 硬性限制：防止單筆部位名目價值超過帳戶餘額的 50%
        # 這裡的 balance 是 get_balance() 即總資金
        total_bal = get_balance()
        max_notional = total_bal * 0.5
        if (base_amt * price) > max_notional:
            print(f"🛑 [極限保護] {sym} 計算出的名目價值 ({base_amt * price:.2f}) 超過總資金的 50% ({max_notional:.2f})，硬性下修！")
            base_amt = round_step(max_notional / price, prec["step_size"])
"""
if "max_notional = total_bal * 0.5" not in code:
    code = code.replace(old_base_amt, new_base_amt)


# 5. update_market_wind moved out of main_loop
# Find where it's called in main_loop:
old_update_wind = "            await update_market_wind(exchange)"
if old_update_wind in code:
    code = code.replace(old_update_wind, "            # await update_market_wind(exchange)  # 已移至獨立 Task")

# Add independent task for market wind
wind_task = """async def market_wind_loop(exchange):
    while True:
        try:
            await update_market_wind(exchange)
        except Exception as e:
            print(f"⚠️ [大盤風向更新失敗] {e}")
        await asyncio.sleep(60)

"""
if "async def market_wind_loop" not in code:
    code = code.replace("async def main_loop(exchange):", wind_task + "async def main_loop(exchange):\n    asyncio.create_task(market_wind_loop(exchange))")

with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Refactor complete.")
