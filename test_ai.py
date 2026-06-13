import asyncio
import json
import logging
import time
from ai_signal import fetch_ai_signals
from multi_coin_bot_v2 import ALL_SYMBOLS

async def main():
    ctxs = []
    for sym in ALL_SYMBOLS:
        ctxs.append({
            "symbol": sym, "price": 60000, "rsi": 50, "ema20": 60000, "ema50": 60000,
            "macd_hist": 0, "atr": 100, "atr_ma20": 100, "bb_up": 61000, "bb_low": 59000,
            "htf_trend": "long", "sma200_15m": 60000, "recent_close_changes_pct": [],
            "btc_change_15m": 0, "fear_and_greed_index": 50, "market_regime": "NORMAL_CHOP",
            "position_qty": 0.0, "profit_pct": 0.0
        })
    print(f"Testing fetch_ai_signals with {len(ctxs)} coins...")
    t0 = time.time()
    try:
        res = await asyncio.wait_for(fetch_ai_signals(ctxs), timeout=350)
        print("RES keys:", res.keys() if res else "EMPTY")
        print("Time:", time.time() - t0)
    except Exception as e:
        print("ERROR:", e)

asyncio.run(main())
