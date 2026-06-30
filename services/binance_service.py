import os
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
use_testnet = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")

client = None
if api_key and api_key != "your_api_key_here":
    client = Client(api_key, api_secret, testnet=use_testnet)
else:
    client = Client(testnet=use_testnet)

_contract_precisions = {}

def get_contract_step(symbol):
    if symbol in _contract_precisions:
        return _contract_precisions[symbol]
    try:
        info = client.futures_exchange_info()
        for s in info.get('symbols', []):
            if s['symbol'] == symbol:
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        step = float(f['stepSize'])
                        _contract_precisions[symbol] = step
                        return step
    except Exception as e:
        pass
    return 0.001

def round_step(qty, step):
    if qty <= 0 or step <= 0:
        return 0.0
    precision = int(round(-__import__('math').log10(step)))
    return round(round(qty / step) * step, precision)

def get_price(symbol: str):
    ticker = client.futures_symbol_ticker(symbol=symbol)
    return {
        "symbol": symbol,
        "price": float(ticker["price"]),
        "timestamp": ticker.get("time")
    }

import time
_last_prices = {}
_last_prices_time = 0

_valid_futures_symbols: set = set()
_valid_futures_cache_time: float = 0.0


def _get_valid_futures_symbols() -> set:
    """取得所有 USDT 永續合約幣種，快取 1 小時。"""
    global _valid_futures_symbols, _valid_futures_cache_time
    if time.time() - _valid_futures_cache_time < 3600:
        return _valid_futures_symbols
    try:
        info = client.futures_exchange_info()
        syms = {
            s["symbol"]
            for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        }
        _valid_futures_symbols = syms
        _valid_futures_cache_time = time.time()
    except Exception as e:
        print(f"[FuturesInfo] 取得合約清單失敗: {e}")
    return _valid_futures_symbols


def get_hot_movers(
    min_vol_usdt: float = 10_000_000,
    min_change_pct: float = 5.0,
    max_change_pct: float = 25.0,
    min_price: float = 0.01,
    limit: int = 3,
    ignore_list=None,
) -> list:
    """全市場掃描有動能但未過熱的合約幣種。
    防範機制：
    · min_vol_usdt   24h 成交量 ≥ $10M   — 過濾低流動性幣
    · max_change_pct 24h 漲幅 ≤ 25%       — 不追已過熱（防抄頂）
    · min_price      價格 ≥ $0.01          — 過濾超微幣（精度/點差風險）
    · valid_futures  確認為有效 USDT 永續合約
    """
    try:
        valid = _get_valid_futures_symbols()
        tickers = client.futures_ticker()
        exclude = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "USDCUSDT", "BTCDOMUSDT"}
        if ignore_list:
            exclude.update(ignore_list)

        candidates = []
        for t in tickers:
            sym = t["symbol"]
            if sym in exclude or not sym.endswith("USDT") or sym not in valid:
                continue
            try:
                price = float(t.get("lastPrice",          0))
                q_vol = float(t.get("quoteVolume",        0))
                chg   = float(t.get("priceChangePercent", 0))
            except (ValueError, TypeError):
                continue

            if price < min_price:
                continue
            if q_vol < min_vol_usdt:
                continue
            if not (min_change_pct <= chg <= max_change_pct):
                continue

            candidates.append({"symbol": sym, "price": price, "q_vol": q_vol, "change_pct": chg})

        candidates.sort(key=lambda x: x["change_pct"], reverse=True)
        return candidates[:limit]
    except Exception as e:
        print(f"[HotMovers] 掃描失敗: {e}")
        return []

def get_all_prices():
    global _last_prices, _last_prices_time
    now = time.time()
    if now - _last_prices_time < 2:
        return _last_prices
    try:
        tickers = client.futures_ticker()
        prices = {}
        for t in tickers:
            prices[t['symbol']] = float(t.get('lastPrice', 0))
        _last_prices = prices
        _last_prices_time = now
        return prices
    except Exception as e:
        if _last_prices:
            return _last_prices
        raise e


def get_position(symbol: str, quote_asset: str, base_asset: str):
    positions = client.futures_position_information(symbol=symbol)
    if not positions:
        raise Exception("No position data returned")
        
    pos = positions[0]
    qty = float(pos['positionAmt'])
    unrealized_pnl = float(pos['unRealizedProfit'])
    entry_price = float(pos['entryPrice'])
    mark_price = float(pos['markPrice'])
    
    abs_qty = abs(qty)
    total_cost = abs_qty * entry_price
    current_value = abs_qty * mark_price
    pnl_percent = (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0.0
    
    return {
        "asset": base_asset,
        "quote_asset": quote_asset,
        "qty": qty,
        "avg_price": entry_price,
        "total_cost": total_cost,
        "current_price": mark_price,
        "current_value": current_value,
        "pnl": unrealized_pnl,
        "pnl_percent": pnl_percent,
        "realized_pnl": 0.0
    }

def get_trades(symbol: str):
    trades = client.futures_account_trades(symbol=symbol, limit=15)
    formatted_trades = []
    for t in reversed(trades):
        qty = float(t["qty"])
        is_buyer = (t["side"] == "BUY")
        realized_pnl = float(t.get("realizedPnl", 0.0))
        timestamp = t.get("time") / 1000.0
        formatted_trades.append({
            "id": t.get("id"),
            "order_id": t.get("orderId"),
            "price": float(t["price"]),
            "qty": qty,
            "time": timestamp,
            "is_buyer": is_buyer,
            "pnl": realized_pnl if realized_pnl != 0 else None
        })
    return formatted_trades

def get_klines(symbol: str, interval: str, limit: int):
    klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    result = []
    for k in klines:
        result.append({
            "open_time": k[0] / 1000.0,
            "time": k[0] / 1000.0,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": k[6] / 1000.0,
        })
    return result

def get_1h_volatility(symbol: str):
    try:
        klines = client.futures_klines(symbol=symbol, interval='15m', limit=4)
        if not klines:
            return symbol, 0
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        vols = [float(k[7]) for k in klines] 
        
        h = max(highs)
        l = min(lows)
        q_vol = sum(vols)
        
        if l > 0 and q_vol > 1_000_000:
            volatility = ((h - l) / l) * 100
            return symbol, volatility
    except:
        pass
    return symbol, 0

def get_atr_ranked_coins(symbols, limit=8):
    """Rank given symbols by 14-day ATR% (ATR / price). Returns (selected_list, full_ranked_list)."""
    ranked = []
    for sym in symbols:
        try:
            klines = client.futures_klines(symbol=sym, interval='1d', limit=16)
            if not klines or len(klines) < 2:
                continue
            trs = []
            for i in range(1, len(klines)):
                high = float(klines[i][2])
                low  = float(klines[i][3])
                prev_close = float(klines[i - 1][4])
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)
            atr = sum(trs[-14:]) / min(len(trs), 14)
            price = float(klines[-1][4])
            atr_pct = round(atr / price * 100, 3) if price > 0 else 0.0
            ranked.append({"symbol": sym, "atr_pct": atr_pct, "price": price})
        except Exception as e:
            print(f"[ATR Rank] {sym} error: {e}")
    ranked.sort(key=lambda x: x["atr_pct"], reverse=True)
    selected = [r["symbol"] for r in ranked[:limit]]
    return selected, ranked

def get_top_volume_altcoins(limit=12, ignore_list=None):
    try:
        tickers = client.futures_ticker()
        exclude_list = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "USDCUSDT"]
        if ignore_list:
            exclude_list.extend(ignore_list)
        candidates = []
        for t in tickers:
            sym = t['symbol']
            if not sym.endswith('USDT'):
                continue
            if sym in exclude_list:
                continue
            try:
                price = float(t.get('lastPrice', 0))
                q_vol = float(t.get('quoteVolume', 0))
            except (ValueError, TypeError):
                continue

            # Filter for "small coins": price under $5.0
            if price > 5.0 or price == 0:
                continue

            if q_vol > 0:
                candidates.append((sym, q_vol))

        # Sort by quoteVolume descending and compute volatility-based score for the top candidates
        candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = candidates[: max(limit * 4, 20)]
        scored = []
        for sym, q_vol in top_candidates:
            _, volatility = get_1h_volatility(sym)
            # Combine volume and short-term volatility into a single ranking score
            vol_factor = 1.0 + min(max(volatility, 0.0), 50.0) / 20.0
            score = q_vol * vol_factor
            scored.append((sym, score, q_vol, volatility))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, *_ in scored[:limit]]
    except Exception as e:
        print(f"Error fetching top volume altcoins: {e}")
        return []

def market_buy(symbol: str, amount: float):
    ticker = client.futures_symbol_ticker(symbol=symbol)
    price = float(ticker['price'])
    qty = amount / price
    step = get_contract_step(symbol)
    qty_str = str(round_step(qty, step))

    if symbol == 'USDCUSDT':
        order = client.futures_create_order(
            symbol=symbol,
            side=Client.SIDE_BUY,
            type=Client.ORDER_TYPE_LIMIT,
            timeInForce='GTC',
            price='0.9999',
            quantity=qty_str
        )
    else:
        order = client.futures_create_order(
            symbol=symbol,
            side=Client.SIDE_BUY,
            type=Client.ORDER_TYPE_MARKET,
            quantity=qty_str
        )
    return order

def market_short(symbol: str, amount: float):
    ticker = client.futures_symbol_ticker(symbol=symbol)
    price = float(ticker['price'])
    qty = amount / price
    step = get_contract_step(symbol)
    qty_str = str(round_step(qty, step))

    order = client.futures_create_order(
        symbol=symbol,
        side=Client.SIDE_SELL,
        type=Client.ORDER_TYPE_MARKET,
        quantity=qty_str
    )
    return order

def market_sell(symbol: str, base_asset: str):
    positions = client.futures_position_information(symbol=symbol)
    if not positions:
        raise Exception("找不到合約倉位資訊")
        
    qty = float(positions[0]['positionAmt'])
    if qty == 0:
        raise Exception("當前無合約倉位可平倉")

    side = Client.SIDE_SELL if qty > 0 else Client.SIDE_BUY
    step = get_contract_step(symbol)
    qty_str = str(round_step(abs(qty), step))
    
    if symbol == 'USDCUSDT':
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type=Client.ORDER_TYPE_LIMIT,
            timeInForce='GTC',
            price='1.0000',
            quantity=qty_str
        )
    else:
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type=Client.ORDER_TYPE_MARKET,
            quantity=abs(qty)
        )
    return order
