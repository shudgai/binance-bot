import logging
import asyncio
import json
import os
import time
import numpy as np
from core.config import (
    TIMEFRAME, PAPER_TRADING,
    ATR_WARMUP_BATCH_SIZE, ATR_WARMUP_SYMBOL_COUNT, ATR_WARMUP_LIMIT, ATR_WARMUP_PAUSE_SEC,
)
from core.indicators import calculate_ema, calculate_bollinger_bands

logger = logging.getLogger(__name__)


async def update_market_wind(exchange):
    from core import ctx
    global_market_wind = ctx.MARKET_WIND
    try:
        btc_ohlcv = await exchange.fetch_ohlcv("BTC/USDT", TIMEFRAME, limit=100)
        eth_ohlcv = await exchange.fetch_ohlcv("ETH/USDT", TIMEFRAME, limit=100)
        btc_ohlcv_1h = await exchange.fetch_ohlcv("BTC/USDT", '1h', limit=50)
        btc_ohlcv_4h = await exchange.fetch_ohlcv("BTC/USDT", '4h', limit=50)

        global_market_wind["allow_long"] = True
        global_market_wind["allow_short"] = True

        if len(btc_ohlcv_1h) >= 20:
            btc_closes_1h = [x[4] for x in btc_ohlcv_1h]
            alpha = 2 / 21
            ema = btc_closes_1h[0]
            for val in btc_closes_1h[1:]: ema = alpha * val + (1 - alpha) * ema
            btc_price_1h = btc_closes_1h[-1]
            global_market_wind["btc_trend_1h"] = "BULL" if btc_price_1h > ema else "BEAR"
        else:
            global_market_wind["btc_trend_1h"] = "NEUTRAL"

        if len(btc_ohlcv_4h) >= 20:
            btc_closes_4h = [x[4] for x in btc_ohlcv_4h]
            alpha_4h = 2 / 21
            ema_4h = btc_closes_4h[0]
            for val in btc_closes_4h[1:]: ema_4h = alpha_4h * val + (1 - alpha_4h) * ema_4h
            btc_price_4h = btc_closes_4h[-1]
            global_market_wind["btc_trend_4h"] = "BULL" if btc_price_4h > ema_4h else "BEAR"
        else:
            global_market_wind["btc_trend_4h"] = "NEUTRAL"

        if len(btc_ohlcv) >= 20:
            btc_closes = np.array([x[4] for x in btc_ohlcv])
            btc_ema20 = calculate_ema(btc_closes, 20)
            btc_price = btc_closes[-1]
            btc_change_15m = (btc_price - btc_closes[-15]) / btc_closes[-15]

            global_market_wind["btc_trend"] = "BULL" if btc_price > btc_ema20 else "BEAR"
            global_market_wind["btc_change_15m"] = btc_change_15m
        else:
            btc_change_15m = 0.0

        if len(eth_ohlcv) >= 20:
            eth_closes = np.array([x[4] for x in eth_ohlcv])
            eth_price = eth_closes[-1]
            eth_change_15m = (eth_price - eth_closes[-15]) / eth_closes[-15]
            global_market_wind["eth_change_15m"] = eth_change_15m
        else:
            eth_change_15m = 0.0

        if btc_change_15m < -0.025 or eth_change_15m < -0.025:
            global_market_wind["allow_long"] = False
            logger.info(f"⚠️ [大盤瀑布風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣多單開倉！")
        elif btc_change_15m > 0.025 or eth_change_15m > 0.025:
            global_market_wind["allow_short"] = False
            logger.info(f"⚠️ [大盤暴漲風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣空單開倉！")

    except Exception as e:
        logger.info(f"⚠️ [更新大盤風向失敗]: {e}")


async def initialize_atr_history(exchange, batch_size: int = ATR_WARMUP_BATCH_SIZE, limit: int = ATR_WARMUP_LIMIT, pause_sec: float = ATR_WARMUP_PAUSE_SEC):
    from core import ctx
    target_symbols = ctx.ALL_SYMBOLS[:ATR_WARMUP_SYMBOL_COUNT]

    loaded_symbols = set()
    try:
        cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "atr_history_cache.json")
        if os.path.exists(cache_path):
            with open(cache_path, "r") as f:
                cache_data = json.load(f)
            for sym in cache_data:
                if sym in ctx.STATES and sym in target_symbols:
                    ctx.STATES[sym]["atr_history"] = cache_data[sym]
                    loaded_symbols.add(sym)
            if loaded_symbols:
                logger.info(f"💾 [快取] 成功從本地載入 {len(loaded_symbols)} 個幣種的 ATR 歷史資料！")
    except Exception as e:
        logger.info(f"⚠️ [快取] 讀取失敗: {e}")

    target_symbols = [sym for sym in target_symbols if sym not in loaded_symbols]
    if not target_symbols:
        logger.info("✅ [初始化] 所有幣種皆已從快取載入，跳過網路預熱！")
        return

    logger.info(f"⏳ [初始化] 尚有 {len(target_symbols)} 個幣種需要網路獲取，開始分批獲取 {limit} 根 {TIMEFRAME} K線...")
    total = len(target_symbols)

    for batch_index in range(0, total, batch_size):
        batch = target_symbols[batch_index:batch_index + batch_size]
        logger.info(f"⏳ [初始化] 進行第 {batch_index // batch_size + 1} 批：{len(batch)} 個幣種")
        tasks = [exchange.fetch_ohlcv(sym, '1m', limit=limit) for sym in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for sym, result in zip(batch, results):
            if not isinstance(result, Exception) and result:
                ohlcv = result
                tr_list = []
                for j in range(1, len(ohlcv)):
                    h = ohlcv[j][2]
                    l = ohlcv[j][3]
                    pc = ohlcv[j-1][4]
                    tr = max(h - l, abs(h - pc), abs(l - pc))
                    tr_list.append(tr)
                    if len(tr_list) >= 14:
                        atr = float(np.mean(tr_list[-14:]))
                        ctx.STATES[sym]["atr_history"].append(atr)
                logger.info(f"✅ [初始化] {sym} 歷史 ATR 預熱完成，載入 {len(ctx.STATES[sym]['atr_history'])} 筆數據")
            else:
                logger.info(f"⚠️ [初始化] {sym} 歷史 ATR 預熱失敗: {result}")

        if batch_index + batch_size < total:
            await asyncio.sleep(pause_sec)


async def fetch_all_klines(exchange):
    from core import ctx
    async def fetch_with_sem(sym):
        async with ctx.request_semaphore:
            return await exchange.fetch_ohlcv(sym, TIMEFRAME, limit=100)

    symbols = list(ctx.ALL_SYMBOLS)
    tasks = {sym: fetch_with_sem(sym) for sym in symbols}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for i, sym in enumerate(symbols):
        if not isinstance(results[i], Exception):
            ctx.STATES[sym]["ohlcv"] = results[i]
            ctx.STATES[sym]["close_price"] = results[i][-1][4]
        else:
            logger.info(f"⚠️ [K線獲取失敗] {sym}: {results[i]}")


async def fetch_sma200_15m(exchange, sym):
    from core import ctx
    try:
        async with ctx.request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '15m', limit=200)
        closes = np.array([x[4] for x in ohlcv])
        return float(np.mean(closes))
    except Exception as e:
        logger.info(f"⚠️ [SMA200獲取失敗] {sym}: {e}")
        return 0.0


async def fetch_all_sma200(exchange):
    from core import ctx
    symbols = list(ctx.ALL_SYMBOLS)
    tasks = [fetch_sma200_15m(exchange, sym) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(symbols):
        if not isinstance(results[i], Exception):
            ctx.STATES[sym]["sma200_15m"] = results[i]


async def fetch_ema_15m(exchange, sym):
    from core import ctx
    try:
        async with ctx.request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '15m', limit=100)
        if not ohlcv or len(ohlcv) == 0:
            return 0.0, 0.0
        closes = np.array([x[4] for x in ohlcv])
        ema20 = calculate_ema(closes, 20)
        ema50 = calculate_ema(closes, 50)
        return float(ema20), float(ema50)
    except Exception as e:
        logger.info(f"⚠️ [15m EMA獲取失敗] {sym}: {e}")
        return 0.0, 0.0


async def fetch_all_ema_15m(exchange):
    from core import ctx
    symbols = list(ctx.ALL_SYMBOLS)
    tasks = [fetch_ema_15m(exchange, sym) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(symbols):
        if not isinstance(results[i], Exception):
            ema20, ema50 = results[i]
            ctx.STATES[sym]["ema20_15m"] = ema20
            ctx.STATES[sym]["ema50_15m"] = ema50


async def fetch_ema50_1h(exchange, sym):
    from core import ctx
    try:
        async with ctx.request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '1h', limit=100)
        if not ohlcv or len(ohlcv) == 0:
            return 0.0
        closes = np.array([x[4] for x in ohlcv])
        ema50 = calculate_ema(closes, 50)
        return float(ema50)
    except Exception as e:
        logger.info(f"⚠️ [1H EMA50獲取失敗] {sym}: {e}")
        return 0.0


async def fetch_all_ema50_1h(exchange):
    from core import ctx
    symbols = list(ctx.ALL_SYMBOLS)
    tasks = [fetch_ema50_1h(exchange, sym) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(symbols):
        if not isinstance(results[i], Exception):
            ctx.STATES[sym]["ema50_1h"] = results[i]


async def fetch_bb_4h(exchange, sym):
    from core import ctx
    try:
        async with ctx.request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '4h', limit=50)
        if not ohlcv or len(ohlcv) == 0:
            return None, None
        closes = np.array([x[4] for x in ohlcv])
        mbb, upper, lower = calculate_bollinger_bands(closes, 20, 2)
        return float(upper[-1]), float(lower[-1])
    except Exception as e:
        logger.info(f"⚠️ [4H BB獲取失敗] {sym}: {e}")
        return None, None


async def fetch_all_bb_4h(exchange):
    from core import ctx
    symbols = list(ctx.ALL_SYMBOLS)
    tasks = [fetch_bb_4h(exchange, sym) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(symbols):
        if not isinstance(results[i], Exception):
            upper, lower = results[i]
            if upper is not None and lower is not None:
                ctx.STATES[sym]["bb_upper_4h"] = upper
                ctx.STATES[sym]["bb_lower_4h"] = lower


async def load_open_positions():
    from core import ctx
    from core.state_manager import build_symbol_state
    from core.symbol_profile import apply_symbol_profile, SYMBOL_PROFILES
    if not PAPER_TRADING:
        return
    try:
        state_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "paper_state.json")
        with open(state_path, "r") as f:
            state = json.load(f)

        current_time = time.time()

        positions_dict = state.get("positions", {})
        for pk, pos in positions_dict.items():
            qty = float(pos.get("qty", 0.0))
            if abs(qty) > 0.000001:
                sym = pk.replace(":", "")
                if sym not in ctx.ALL_SYMBOLS:
                    logger.info(f"⚠️ [發現未監控持倉] {sym} 仍有未平倉位，自動加回監控清單並在介面顯示！")
                    ctx.ALL_SYMBOLS.append(sym)
                    ctx.STATES[sym] = build_symbol_state(sym)
                    apply_symbol_profile(sym, SYMBOL_PROFILES.get(sym, {}))

                ctx.STATES[sym]["qty"] = qty
                ctx.STATES[sym]["avg_price"] = float(pos.get("avg_price", 0.0))
                ctx.STATES[sym]["entries"] = pos.get("entries", [])

        trades = state.get("trades", [])
        for t in reversed(trades):
            if t.get("is_close"):
                sym = t.get("symbol", "").replace(":USDT", "USDT")
                if sym in ctx.STATES:
                    trade_time_sec = t.get("time", 0) / 1000.0
                    if current_time - trade_time_sec < 300 and ctx.STATES[sym]["qty"] == 0:
                        if ctx.STATES[sym]["status"] != "COOLDOWN":
                            ctx.STATES[sym]["status"] = "COOLDOWN"
                            ctx.STATES[sym]["next_status_time"] = trade_time_sec + 300
    except Exception as e:
        logger.info(f"⚠️ [讀取持倉失敗] {e}")
