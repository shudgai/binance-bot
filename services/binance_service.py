import os
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
use_testnet = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")

# 用的是幣安「Demo Trading」網頁申請的金鑰，跟舊版 testnet 是不同網址系統，
# python-binance 用 demo=True（不是 testnet=True）才會打對網址。
client = None
if api_key and api_key != "your_api_key_here":
    client = Client(api_key, api_secret, demo=use_testnet)
else:
    client = Client(demo=use_testnet)

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


def _get_entry_price(symbol: str, side: str):
    """選擇一個更貼近牌價的入場價格，優先使用 mark price，再回退到 order book 中位數，最後是最新成交價。"""
    try:
        mark = client.futures_mark_price(symbol=symbol)
        mark_price = float(mark.get("markPrice", 0))
        if mark_price > 0:
            return mark_price
    except Exception:
        pass

    try:
        book = client.futures_order_book(symbol=symbol, limit=5)
        if isinstance(book, dict):
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if isinstance(bids, list) and isinstance(asks, list) and bids and asks:
                bid_entry = bids[0]
                ask_entry = asks[0]
                if isinstance(bid_entry, (list, tuple)) and len(bid_entry) >= 1 and isinstance(ask_entry, (list, tuple)) and len(ask_entry) >= 1:
                    bid_price = float(bid_entry[0])
                    ask_price = float(ask_entry[0])
                    midpoint = (bid_price + ask_price) / 2.0
                    if midpoint > 0:
                        return midpoint
    except Exception:
        pass

    ticker = client.futures_symbol_ticker(symbol=symbol)
    price = float(ticker.get("price", 0))
    return price

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


def get_atr_scan_universe(min_vol_usdt: float = 5_000_000, max_candidates: int = 60, ignore_list=None) -> list:
    """從幣安永續合約市場即時抓取候選幣種清單（依24h成交量篩選/排序），供 ATR 雷達排名使用。
    取代寫死的固定清單，讓 ATR 雷達能發現真正在市場上活躍、但尚未寫進設定檔的永續合約。"""
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
                q_vol = float(t.get("quoteVolume", 0))
            except (ValueError, TypeError):
                continue
            if q_vol < min_vol_usdt:
                continue
            candidates.append((sym, q_vol))

        candidates.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in candidates[:max_candidates]]
    except Exception as e:
        print(f"[ATR掃描範圍] 抓取永續合約清單失敗: {e}")
        return []


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
        print(f"[HotMovers] 掃到 {len(candidates)} 個候選（漲{min_change_pct}-{max_change_pct}% vol>${min_vol_usdt/1e6:.0f}M），回傳前 {limit} 個")
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


def get_account_balance_usdt() -> float:
    """即時查詢合約帳戶 USDT 餘額，給 API 進程自己直接查，不依賴 main.py 進程內快取的 REAL_BALANCE
    （main.py 和 API 是兩個獨立進程，各自的模組全域變數互不相通）。"""
    for b in client.futures_account_balance():
        if b.get("asset") == "USDT":
            return float(b.get("balance", 0.0))
    return 0.0


def get_position(symbol: str, quote_asset: str, base_asset: str):
    positions = client.futures_position_information(symbol=symbol)
    if not positions:
        # 從沒交易過的幣種查不到部位資料是正常情況（沒有持倉），不是錯誤
        return {
            "asset": base_asset,
            "quote_asset": quote_asset,
            "qty": 0.0,
            "avg_price": 0.0,
            "total_cost": 0.0,
            "current_price": 0.0,
            "current_value": 0.0,
            "pnl": 0.0,
            "pnl_percent": 0.0,
            "realized_pnl": 0.0
        }

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
    if symbol == "ALL":
        # 幣安沒有「查所有幣種成交」的單一端點，逐一查目前監控的幣種再合併排序。
        # 只查目前監控池會漏掉已經輪替出池子的幣種（例如雷達換幣後），導致之前明明
        # 有成交的幣種從清單消失，所以額外併入本機 trade_history.json 記錄過的幣種，
        # 確保歷史成交不會因為幣種被換出監控池就從畫面上憑空消失。
        from services.bot_manager_service import load_symbol_config
        import json as _json
        from core.config import TRADE_HISTORY_FILE
        query_symbols = set(load_symbol_config())
        try:
            with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = _json.load(f)
            query_symbols.update(t.get("symbol", "") for t in history if t.get("symbol"))
        except Exception:
            pass
        all_trades = []
        for sym in query_symbols:
            try:
                all_trades.extend(client.futures_account_trades(symbol=sym, limit=15))
            except Exception:
                continue
        all_trades.sort(key=lambda t: t.get("time", 0), reverse=True)
        trades = list(reversed(all_trades[:30]))
    else:
        trades = client.futures_account_trades(symbol=symbol, limit=15)
    formatted_trades = []
    for t in reversed(trades):
        qty = float(t["qty"])
        is_buyer = (t["side"] == "BUY")
        realized_pnl = float(t.get("realizedPnl", 0.0))
        # 前端 formatTradeTime() 是拿 ms 直接餵 new Date()（跟紙上交易 paper_state.json
        # 的時間格式一致），這裡原本除以 1000 轉成秒，會讓顯示的成交時間跑到 1970 年附近。
        timestamp = t.get("time")
        formatted_trades.append({
            "id": t.get("id"),
            "order_id": t.get("orderId"),
            "price": float(t["price"]),
            "qty": qty,
            "time": timestamp,
            "is_buyer": is_buyer,
            "symbol": t.get("symbol"),
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
    price = _get_entry_price(symbol, "BUY")
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
    price = _get_entry_price(symbol, "SELL")
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

def get_all_positions():
    positions = client.futures_position_information()
    formatted = []
    for pos in positions:
        qty = float(pos['positionAmt'])
        if abs(qty) > 0.000001:
            sym = pos['symbol']
            entry_price = float(pos['entryPrice'])
            unrealized_pnl = float(pos['unRealizedProfit'])
            mark_price = float(pos['markPrice'])
            total_cost = abs(qty) * entry_price
            pnl_percent = (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0.0
            formatted.append({
                "symbol": sym.replace('USDT', ':USDT'),
                "positionAmt": qty,
                "qty": qty,
                "entryPrice": entry_price,
                "avg_price": entry_price,
                "markPrice": mark_price,
                "current_price": mark_price,
                "unRealizedProfit": unrealized_pnl,
                "pnl": unrealized_pnl,
                "pnl_percent": pnl_percent
            })
    return formatted
