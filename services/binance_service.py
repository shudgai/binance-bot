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
# ping=False：python-binance 建構子預設會在啟動當下打一次 ping 測連線，且沒有
# 包 try/except，只要 demo-api.binance.com 短暫 502（實際發生過，幣安自己的
# Demo Trading 網域故障），整個 API process 會直接 crash-loop 起不來。這個 ping
# 只是「啟動時的連線小提示」，不影響後續實際 API 呼叫，關掉它讓啟動不受外部
# 短暫故障影響即可。
client = None
if api_key and api_key != "your_api_key_here":
    client = Client(api_key, api_secret, demo=use_testnet, ping=False)
else:
    client = Client(demo=use_testnet, ping=False)

# 報價/掃描一律走真實公開行情，避免 Demo Trading 價格與真實市場脫鉤。
# 交易與帳戶查詢仍維持使用 client（可依 USE_TESTNET 走 demo 或正式）。
market_client = client
if use_testnet:
    try:
        market_client = Client(ping=False)
    except Exception:
        market_client = client

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
    ticker = market_client.futures_symbol_ticker(symbol=symbol)
    return {
        "symbol": symbol,
        "price": float(ticker["price"]),
        "timestamp": ticker.get("time")
    }


def _get_entry_price(symbol: str, side: str):
    """選擇一個更貼近牌價的入場價格，優先使用 mark price，再回退到 order book 中位數，最後是最新成交價。"""
    try:
        mark = market_client.futures_mark_price(symbol=symbol)
        mark_price = float(mark.get("markPrice", 0))
        if mark_price > 0:
            return mark_price
    except Exception:
        pass

    try:
        book = market_client.futures_order_book(symbol=symbol, limit=5)
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

    ticker = market_client.futures_symbol_ticker(symbol=symbol)
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
        info = market_client.futures_exchange_info()
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


def get_atr_scan_universe(min_vol_usdt: float = 5_000_000,
                         max_candidates: int = 24,
                         ignore_list=None,
                         max_change_pct: float = 50.0,
                         min_price: float = 0.01,
                         min_orderbook_depth_usdt: float = 2_000.0,
                         max_spread_pct: float = 0.003) -> list:
    """從幣安永續合約市場即時抓取候選幣種清單（依24h成交量篩選/排序），供 ATR 雷達排名使用。
    取代寫死的固定清單，讓 ATR 雷達能發現真正在市場上活躍、但尚未寫進設定檔的永續合約。"""
    try:
        valid = _get_valid_futures_symbols()
        tickers = market_client.futures_ticker()
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
                chg = float(t.get("priceChangePercent", 0))
                price = float(t.get("lastPrice", 0))
            except (ValueError, TypeError):
                continue
            if q_vol < min_vol_usdt:
                continue
            # 排除 24h 漲跌幅過高或過低（急升/急跌）之標的，避免追高或追底
            if abs(chg) > max_change_pct:
                continue
            # 價格過低或過高過濾（避免超微幣或高價位超出策略範圍）
            if price < min_price or price == 0:
                continue
            candidates.append((sym, q_vol))

        # orderbook 深度檢查放在成交量排序、截斷到 max_candidates 之後才做，
        # 只對真正可能被選中的候選查委託簿，不是每個通過前面篩選的幣種都查——
        # 之前對全部候選都查，一次掃描要打幾十次委託簿 API，把幣安權重推到超標
        # （2400 上限一度打到 2449），連帶讓查真實餘額之類的其他請求間歇性失敗。
        candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = candidates[:max_candidates]

        filtered = []
        for sym, q_vol in top_candidates:
            try:
                ob = market_client.futures_order_book(symbol=sym, limit=5)
                bids = ob.get('bids', [])
                asks = ob.get('asks', [])
                if bids and asks:
                    best_bid_price = float(bids[0][0])
                    best_bid_qty = float(bids[0][1])
                    best_ask_price = float(asks[0][0])
                    best_ask_qty = float(asks[0][1])
                    bid_depth_usdt = best_bid_price * best_bid_qty
                    ask_depth_usdt = best_ask_price * best_ask_qty
                    if bid_depth_usdt < min_orderbook_depth_usdt or ask_depth_usdt < min_orderbook_depth_usdt:
                        continue
                    # 買賣價差過濾：即使深度夠，價差太大代表這個幣種交易成本高
                    # （一買一賣就先虧掉價差），用同一次委託簿查詢順便算，不用額外
                    # 呼叫 API。
                    mid_price = (best_bid_price + best_ask_price) / 2
                    spread_pct = (best_ask_price - best_bid_price) / mid_price if mid_price > 0 else 1.0
                    if spread_pct > max_spread_pct:
                        continue
            except Exception:
                # 若 orderbook 查詢失敗則跳過此檢查（不讓單點失敗阻塞整體掃描）
                pass
            filtered.append(sym)

        return filtered
    except Exception as e:
        print(f"[ATR掃描範圍] 抓取永續合約清單失敗: {e}")
        return []


def filter_symbols_by_daily_move(symbols: list[str], max_change_pct: float = 18.0) -> list[str]:
    """Filter a list of symbols by 24h price change percent."""
    if not symbols:
        return []

    try:
        tickers = client.futures_ticker()
        ticker_map = {t.get('symbol', ''): t for t in tickers}
        filtered = []
        for sym in symbols:
            ticker = ticker_map.get(sym)
            if not ticker:
                continue
            try:
                chg = float(ticker.get('priceChangePercent', 0.0))
            except (ValueError, TypeError):
                continue
            if abs(chg) <= max_change_pct:
                filtered.append(sym)
        return filtered
    except Exception as e:
        print(f"[DailyMoveFilter] 24h 變動過濾失敗: {e}")
        return symbols


def get_hot_movers(
    min_vol_usdt: float = 10_000_000,
    min_change_pct: float = 5.0,
    max_change_pct: float = 25.0,
    min_price: float = 0.01,
    limit: int = 2,
    ignore_list=None,
    min_orderbook_depth_usdt: float = 2_000.0,
    max_spread_pct: float = 0.003,
) -> list:
    """全市場掃描有動能但未過熱的合約幣種。
    防範機制：
    · min_vol_usdt   24h 成交量 ≥ $10M   — 過濾低流動性幣
    · max_change_pct 24h 漲幅 ≤ 25%       — 不追已過熱（防抄頂）
    · min_price      價格 ≥ $0.01          — 過濾超微幣（精度/點差風險）
    · valid_futures  確認為有效 USDT 永續合約
    · min_orderbook_depth_usdt  買一/賣一深度都要足夠，跟 get_atr_scan_universe 用同一套標準
      （熱門動能幣通常波動更大，委託簿更薄，之前沒做這項檢查反而比一般 ATR 候選更需要）
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

        # orderbook 深度檢查放在排序、截斷之後才做（只查真正可能被選中的候選），
        # 理由跟 get_atr_scan_universe 一樣：避免對每個通過前面篩選的幣種都打一次
        # 委託簿 API，一次掃描累積起來會把幣安權重推到超標。
        candidates.sort(key=lambda x: x["change_pct"], reverse=True)
        top_candidates = candidates[:max(limit * 5, 15)]

        filtered = []
        for c in top_candidates:
            sym = c["symbol"]
            try:
                ob = client.futures_order_book(symbol=sym, limit=5)
                bids = ob.get('bids', [])
                asks = ob.get('asks', [])
                if bids and asks:
                    best_bid_price = float(bids[0][0])
                    best_ask_price = float(asks[0][0])
                    bid_depth_usdt = best_bid_price * float(bids[0][1])
                    ask_depth_usdt = best_ask_price * float(asks[0][1])
                    if bid_depth_usdt < min_orderbook_depth_usdt or ask_depth_usdt < min_orderbook_depth_usdt:
                        continue
                    mid_price = (best_bid_price + best_ask_price) / 2
                    spread_pct = (best_ask_price - best_bid_price) / mid_price if mid_price > 0 else 1.0
                    if spread_pct > max_spread_pct:
                        continue
            except Exception:
                pass
            filtered.append(c)

        print(f"[HotMovers] 掃到 {len(filtered)} 個候選（漲{min_change_pct}-{max_change_pct}% vol>${min_vol_usdt/1e6:.0f}M），回傳前 {limit} 個")
        return filtered[:limit]
    except Exception as e:
        print(f"[HotMovers] 掃描失敗: {e}")
        return []

def get_all_prices():
    global _last_prices, _last_prices_time
    now = time.time()
    if now - _last_prices_time < 2:
        return _last_prices
    try:
        tickers = market_client.futures_ticker()
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


def get_account_balance_usdt() -> float | None:
    """即時查詢合約帳戶 USDT 餘額，給 API 進程自己直接查，不依賴 main.py 進程內快取的 REAL_BALANCE
    （main.py 和 API 是兩個獨立進程，各自的模組全域變數互不相通）。"""
    try:
        for b in client.futures_account_balance():
            if b.get("asset") == "USDT":
                return float(b.get("balance", 0.0))
    except Exception as e:
        print(f"[BalanceFetch] 讀取合約餘額失敗: {e}")
    return None


def get_total_realized_pnl_usdt() -> float:
    """用幣安 income history 加總帳戶累計已實現損益（含手續費），
    對應紙上交易那邊「total_realized_pnl」的概念，讓實體帳戶也能顯示總已實現利潤。"""
    total = 0.0
    try:
        records = client.futures_income_history(limit=1000)
        for r in records:
            if r.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                total += float(r.get("income", 0.0) or 0.0)
    except Exception:
        pass
    return total


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
        # 前端交易列表（買入/賣出方向、平倉損益顯示）是照紙上交易 paper_state.json 的欄位
        # 名稱寫的：isBuyer（不是 is_buyer）、is_close、realized_pnl、fee，符號也是用
        # "M:USDT" 這種冒號格式（跟 get_all_positions() 一致，這樣才能對到持倉抓到現價）。
        # 原本這裡欄位名稱、符號格式都對不起來，導致方向永遠顯示賣出、已實現損益永遠不顯示。
        # Binance 只有在成交會「減倉/平倉」時才會算出非 0 的 realizedPnl，開倉成交固定是 0，
        # 可以直接拿它來判斷這筆是不是平倉成交。
        formatted_trades.append({
            "id": t.get("id"),
            "order_id": t.get("orderId"),
            "symbol": str(t.get("symbol", "")).replace("USDT", ":USDT"),
            "price": float(t["price"]),
            "qty": qty,
            "time": timestamp,
            "isBuyer": is_buyer,
            "is_close": realized_pnl != 0,
            "realized_pnl": realized_pnl,
            "fee": float(t.get("commission", 0.0)),
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
            try:
                price = float([t for t in tickers if t['symbol'] == sym][0].get('lastPrice', 0))
            except Exception:
                price = 0
            # skip extremely low price
            if price == 0 or price < 0.01:
                continue
            # simple orderbook depth check
            try:
                ob = client.futures_order_book(symbol=sym, limit=5)
                bids = ob.get('bids', [])
                asks = ob.get('asks', [])
                if not bids or not asks:
                    continue
                bid_depth_usdt = float(bids[0][0]) * float(bids[0][1])
                ask_depth_usdt = float(asks[0][0]) * float(asks[0][1])
                if bid_depth_usdt < 1000 or ask_depth_usdt < 1000:
                    continue
            except Exception:
                pass

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
    # 前端 allPositions 是用「symbol -> 持倉」的物件（跟紙上交易 get_paper_positions() 一樣），
    # 用 `for (let sym in data)` 取 key 直接當幣種名稱。這裡原本回傳陣列，前端迴圈會拿到
    # 「0」「1」這種索引當 key，導致 getPositionInfo() 永遠對不到持倉，交易列表判斷不出
    # 現價、未實現損益。改成回傳用冒號格式符號（跟 get_trades() 一致）當 key 的字典。
    positions = client.futures_position_information()
    result = {}
    for pos in positions:
        qty = float(pos['positionAmt'])
        if abs(qty) > 0.000001:
            sym = pos['symbol']
            entry_price = float(pos['entryPrice'])
            unrealized_pnl = float(pos['unRealizedProfit'])
            mark_price = float(pos['markPrice'])
            total_cost = abs(qty) * entry_price
            pnl_percent = (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0.0
            # 原始持倉資料沒有直接的 leverage 欄位，但可以用 notional/initialMargin 反推
            # 出真實槓桿（名目倉位大小 ÷ 實際佔用保證金）。前端算「槓桿後損益%」原本沒有
            # 真實槓桿可用時會 fallback 到寫死的 20 倍，跟這個幣種實際設定的 2~5 倍差很多，
            # 導致百分比顯示被放大成不合理的數字（例如 -4.89% 顯示成 -97.78%）。
            initial_margin = float(pos.get('initialMargin', 0) or 0)
            leverage = round(abs(float(pos.get('notional', 0) or 0)) / initial_margin) if initial_margin > 0 else 0
            key = sym.replace('USDT', ':USDT')
            result[key] = {
                "symbol": key,
                "positionAmt": qty,
                "qty": qty,
                "entryPrice": entry_price,
                "avg_price": entry_price,
                "markPrice": mark_price,
                "current_price": mark_price,
                "leverage": leverage,
                "unRealizedProfit": unrealized_pnl,
                "pnl": unrealized_pnl,
                "pnl_percent": pnl_percent,
                "realized_pnl": 0
            }
    return result
