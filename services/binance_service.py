import os
import re
import time
import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv
from core import ctx

load_dotenv()

# 幣安 IP 封鎖防護：-1003 錯誤本身會夾帶「banned until <timestamp>」的到期時間，
# 且明確警告持續呼叫只會讓封鎖時間被往後延——實際發生過前端每 3~5 秒輪詢一次
# 持倉/餘額，即使已經被封鎖也照樣繼續打，導致封鎖時間被自己越滾越長，一路延到
# 服務被手動停下來才停止惡化。這裡記錄封鎖到期時間，封鎖期間內直接跳過真正的
# API 呼叫、回傳空結果，不要再打過去。
_binance_ban_until = 0.0

def _binance_banned() -> bool:
    return time.time() < _binance_ban_until

def _note_binance_ban(exc) -> None:
    global _binance_ban_until
    msg = str(exc)
    if "-1003" not in msg and "banned until" not in msg and "teapot" not in msg.lower():
        return
    m = re.search(r"banned until (\d+)", msg)
    ban_ts = int(m.group(1)) / 1000.0 if m else time.time() + 60
    if ban_ts > _binance_ban_until:
        _binance_ban_until = ban_ts

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
def _is_placeholder_key(key):
    if not key:
        return True
    k = str(key).lower()
    return "your_" in k or "api_key" in k or "placeholder" in k or k == "your_api_key_here"

client = None
if api_key and not _is_placeholder_key(api_key):
    client = Client(api_key, api_secret, demo=use_testnet, ping=False)
else:
    client = Client(demo=use_testnet, ping=False)

# 純市場資訊查詢（成交量、委託簿深度/價差、K線掃描）改用真實幣安市場，不透過
# Demo Trading 環境。Demo Trading 的 24hr 成交量/漲跌幅統計看起來與真實市場接近，
# 但即時委託簿是模擬撮合、深度極薄——實測 ADA/LINK/AVAX/ZEC 這種真實世界流動性
# 極好的主流幣，在 Demo 環境委託簿是模擬撮合、深度極薄，導致 ATR
# 雷達的流動性過濾把幾乎所有主流幣都踢除，只剩下少數剛好在 Demo 環境委託簿較深的
# 冷門幣。這些查詢都是公開行情、不需要帳號驗證，也不會下單，改用真實市場的
# 客戶端才能反映真正的流動性。實際交易下單仍然全部走上面的 Demo Trading client。
market_client = Client(ping=False)

_contract_precisions = {}

DASHBOARD_PRICE_CACHE_SEC = float(os.getenv("DASHBOARD_PRICE_CACHE_SEC", "15"))
DASHBOARD_POSITION_CACHE_SEC = float(os.getenv("DASHBOARD_POSITION_CACHE_SEC", "10"))
DASHBOARD_TRADE_CACHE_SEC = float(os.getenv("DASHBOARD_TRADE_CACHE_SEC", "30"))
DASHBOARD_KLINE_CACHE_SEC = float(os.getenv("DASHBOARD_KLINE_CACHE_SEC", "30"))
_single_price_cache = {}
_kline_cache = {}

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

_market_max_qty_cache = {}

def get_market_max_qty(symbol):
    """幣安期貨的 MARKET_LOT_SIZE 過濾器對市價單另外設有比一般 LOT_SIZE 更低的單筆
    數量上限（實測 KAITOUSDT：LOT_SIZE 上限 100 萬，MARKET_LOT_SIZE 卻只有 500）。
    倉位若是用限價單建立，可能建到遠超過這個上限，之後市價平倉若整包數量一次送出
    會被直接拒絕（-4005 Quantity greater than max quantity），導致部位卡住平不掉。"""
    if symbol in _market_max_qty_cache:
        return _market_max_qty_cache[symbol]
    try:
        info = client.futures_exchange_info()
        for s in info.get('symbols', []):
            if s['symbol'] == symbol:
                for f in s.get('filters', []):
                    if f['filterType'] == 'MARKET_LOT_SIZE':
                        max_qty = float(f['maxQty'])
                        _market_max_qty_cache[symbol] = max_qty
                        return max_qty
    except Exception:
        pass
    return None

def round_step(qty, step):
    if qty <= 0 or step <= 0:
        return 0.0
    precision = int(round(-__import__('math').log10(step)))
    return round(round(qty / step) * step, precision)

def get_price(symbol: str):
    """使用快取獲取價格，降低 API 權重消耗"""
    ticker_data = ctx.CACHE.get_ticker(symbol) if ctx.CACHE else None
    if ticker_data:
        return ticker_data
    
    # 如果快取中沒有（例如還沒初始化或抓取失敗），回退到原本的邏輯
    now = time.time()
    cached = _single_price_cache.get(symbol)
    if cached and now - cached[0] < DASHBOARD_PRICE_CACHE_SEC:
        return cached[1]
    if _binance_banned() and cached:
        return cached[1]
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        result = {
            "symbol": symbol,
            "price": float(ticker["price"]),
            "timestamp": ticker.get("time"),
        }
        _single_price_cache[symbol] = (now, result)
        return result
    except Exception as e:
        _note_binance_ban(e)
        if cached:
            return cached[1]
        raise

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

_tradable_futures_symbols: set = set()
_tradable_futures_cache_time: float = 0.0

def _get_tradable_futures_symbols() -> set:
    """取得實際下單帳戶（Demo Trading／正式帳戶）支援交易的合約幣種，快取 1 小時。
    市場資料改用 market_client（真實市場）掃描候選幣種後，仍須用實際下單的 client
    查一次交易所資訊做交集過濾——Demo Trading 的可交易合約清單比真實市場小，選到
    只存在真實市場、Demo 沒有的幣種（例如 EDGEUSDT）會在下單時吃到 binance -1121
    Invalid symbol 錯誤。"""
    global _tradable_futures_symbols, _tradable_futures_cache_time
    if time.time() - _tradable_futures_cache_time < 3600:
        return _tradable_futures_symbols
    try:
        info = client.futures_exchange_info()
        syms = {
            s["symbol"]
            for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        }
        _tradable_futures_symbols = syms
        _tradable_futures_cache_time = time.time()
    except Exception as e:
        print(f"[TradableFuturesInfo] 取得下單帳戶合約清單失敗: {e}")
    return _tradable_futures_symbols

_atr_scan_universe_cache = {}

def get_atr_scan_universe(min_vol_usdt: float = 5_000_000,
                           max_candidates: int = 48,
                           ignore_list=None,
                           max_change_pct: float = 50.0,
                           min_price: float = 0.01,
                           min_orderbook_depth_usdt: float = 100.0,
                           max_spread_pct: float = 0.003) -> list:
    """從幣安永續合約市場即時抓取候選幣種清單（依24h成交量篩選/排序），供 ATR 雷達排名使用。
    取代寫死的固定清單，讓 ATR 雷達能發現真正在市場上活躍、但尚未寫進設定檔的永續合約。
    成交量/委託簿查詢都改用 market_client（真實市場），不是 Demo Trading——實測發現
    Demo Trading 的即時委託簿是模擬撮合、深度極薄，導致 ATR
    雷達的流動性過濾把幾乎所有主流幣都踢除，只剩下少數剛好在 Demo 環境委託簿較深的
    冷門幣。這裡只是查詢公開行情，不涉及帳號或下單，改用真實市場才能反映真正的流動性。"""
    if _binance_banned():
        return []
    import time as _time
    now = _time.time()
    cache_key = (min_vol_usdt, max_candidates, tuple(sorted(ignore_list or [])), max_change_pct, min_price, min_orderbook_depth_usdt, max_spread_pct)
    if cache_key in _atr_scan_universe_cache:
        cache_time, cached_val = _atr_scan_universe_cache[cache_key]
        if now - cache_time < 600:  # 快取 10 分鐘，降低委託簿權重消耗
            return cached_val

    try:
        valid = _get_valid_symbols() if hasattr(market_client, "_get_valid_symbols") else []
        if not valid:
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
        # （2400 上限一度打到 2449），連帶讓其他請求間歇性失敗。
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

        _atr_scan_universe_cache[cache_key] = (now, filtered)
        return filtered
    except Exception as e:
        print(f"[ATR掃描範圍] 抓取永續合約清單失敗: {e}")
        return []

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
    if _binance_banned():
        return []
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
        # 委託簿 API，一次掃描累積起來會把幣安權重推到超標
        # （2400 上限一度打到 2449），連帶讓其他請求間歇性失敗。
        candidates.sort(key=lambda x: x["change_pct"], reverse=True)
        top_candidates = candidates[:max(limit * 5, 15)]

        filtered = []
        for c in top_candidates:
            sym = c["symbol"]
            try:
                ob = market_client.futures_order_book(symbol=sym, limit=5)
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

def get_top_volume_altcoins(limit=12, ignore_list=None):
    if _binance_banned():
        return []
    try:
        tickers = market_client.futures_ticker()
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
                ob = market_client.futures_order_book(symbol=sym, limit=5)
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

        return scored
    except Exception as e:
        print(f"[TopVolumeAltcoins] Error: {e}")
        return []

_DYNAMIC_SELECTION_MAX_CHANGE_PCT = 15.0
_DYNAMIC_SELECTION_MIN_DEPTH_USDT = 1000.0

def get_dynamic_top_15_coins():
    """
    動態選幣：
    1. 抓所有 ticker，篩 USDT 交易對，且僅保留下單帳戶（Demo Trading／正式帳戶）也
       支援交易的幣種（避免選到 -1121 Invalid symbol）。
    2. 排除 24h 漲跌幅過大（>15%）的幣種——已經暴衝暴殺過的幣接下來容易劇烈回吐，
       追進去容易變成套在阻力/支撐區的最後一隻老鼠（實測 US/THE/POWER/EDGE 這類
       靠 DEFAULT_NEW_COIN_PROFILE 進場的新幣，一進場就被巴的案例多半屬於這類）。
    3. 依成交量取前 50 名，再做委託簿深度檢查，過濾掉深度太薄、容易滑價/雜訊震盪
       的幣種（仿照 get_top_volume_altcoins 的做法）。
    4. 對通過深度檢查的候選抓 K 線，計算 ATR% 與 ADX（趨勢明確度）。
    5. 用 ATR%（波動度）與 ADX（趨勢明確度）的綜合分數排序，不再只挑 ATR% 最高的——
       高 ATR% 不代表有方向，常常只是雜訊大、容易上沖下洗，加入 ADX 才能篩掉「波動大
       但沒有方向感」的幣種，取分數最高的前 15 檔。
    """
    if _binance_banned():
        return []

    try:
        from core.indicators import calculate_adx

        tickers = market_client.futures_ticker()

        tradable = _get_tradable_futures_symbols()
        candidates = []
        for t in tickers:
            sym = t['symbol']
            if not sym.endswith('USDT') or (tradable and sym not in tradable):
                continue
            try:
                change_pct = float(t.get('priceChangePercent', 0) or 0)
            except (TypeError, ValueError):
                change_pct = 0.0
            if abs(change_pct) > _DYNAMIC_SELECTION_MAX_CHANGE_PCT:
                continue
            candidates.append({
                'symbol': sym,
                'quoteVolume': float(t.get('quoteVolume', 0)),
                'lastPrice': float(t.get('lastPrice', 0))
            })

        # 依成交量排序取前 50 名
        candidates.sort(key=lambda x: x['quoteVolume'], reverse=True)
        top_50 = candidates[:50]

        # 委託簿深度檢查：只放行深度足夠、不容易滑價的候選
        depth_checked = []
        for item in top_50:
            sym = item['symbol']
            try:
                ob = market_client.futures_order_book(symbol=sym, limit=5)
                bids = ob.get('bids', [])
                asks = ob.get('asks', [])
                if not bids or not asks:
                    continue
                bid_depth_usdt = float(bids[0][0]) * float(bids[0][1])
                ask_depth_usdt = float(asks[0][0]) * float(asks[0][1])
                if bid_depth_usdt < _DYNAMIC_SELECTION_MIN_DEPTH_USDT or ask_depth_usdt < _DYNAMIC_SELECTION_MIN_DEPTH_USDT:
                    continue
            except Exception:
                continue
            depth_checked.append(item)

        # 抓 K 線計算 ATR% 與 ADX（趨勢明確度）
        results = []
        for item in depth_checked:
            sym = item['symbol']
            try:
                klines = market_client.futures_klines(symbol=sym, interval='1d', limit=30)
                if not klines or len(klines) < 15:
                    continue

                highs = [float(k[2]) for k in klines]
                lows = [float(k[3]) for k in klines]
                closes = [float(k[4]) for k in klines]

                trs = []
                for i in range(1, len(klines)):
                    tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                    trs.append(tr)

                atr = sum(trs[-14:]) / min(len(trs), 14)
                price = closes[-1]
                atr_pct = round(atr / price * 100, 3) if price > 0 else 0.0
                adx = float(calculate_adx(np.array(highs), np.array(lows), np.array(closes), 14))

                # 波動度與趨勢明確度並重：純波動大但 ADX 低（沒有方向感、容易雙巴）的
                # 幣種分數會被壓低，不會再單純因為 ATR% 最高就雀屏中選。
                atr_component = min(atr_pct, 15.0) / 15.0
                adx_component = min(adx, 50.0) / 50.0
                score = atr_component * 0.5 + adx_component * 0.5

                results.append({
                    'symbol': sym,
                    'quoteVolume': item['quoteVolume'],
                    'atr_pct': atr_pct,
                    'adx': round(adx, 2),
                    'score': round(score, 4),
                    'lastPrice': item['lastPrice']
                })
            except Exception as e:
                print(f"Error fetching data for {sym} during dynamic selection: {e}")
                continue

        results.sort(key=lambda x: x['score'], reverse=True)
        final_selection = results[:15]

        return [r['symbol'] for r in final_selection]

    except Exception as e:
        print(f"Error in get_dynamic_top_15_coins: {e}")
        return []

def get_all_prices():
    """前端「/api/prices」輪詢用，只需要目前監控池幾檔幣的最新價。原本呼叫
    client.futures_ticker() 不帶 symbol 會查全市場（近400檔合約）24hr行情，
    這支端點權重高達 40，前端每 5 秒輪詢一次、內部又只快取 2 秒，等於幾乎每次
    輪詢都真的打一次權重40的重量級請求，是 API 權重衝高的主要來源之一。改成只
    查監控池內的幣種（逐檔權重僅 1），並把快取拉長到可設定的 15 秒，進一步降低儀表板權重。"""
    global _last_prices, _last_prices_time
    now = time.time()
    if now - _last_prices_time < DASHBOARD_PRICE_CACHE_SEC:
        return _last_prices
    if _binance_banned():
        return _last_prices
    try:
        from services.bot_manager_service import load_symbol_config
        symbols = load_symbol_config()
        prices = dict(_last_prices)
        all_tickers = ctx.CACHE.get_all_tickers() if ctx.CACHE else {}
        for sym in symbols:
            if _binance_banned():
                break
            if sym in all_tickers:
                prices[sym] = float(all_tickers[sym].get('price', 0))
            else:
                # Fallback to direct call if not in cache (though it should be)
                try:
                    ticker = client.futures_symbol_ticker(symbol=sym)
                    prices[sym] = float(ticker.get('price', 0))
                except Exception as e:
                    _note_binance_ban(e)
                    continue
        _last_prices = prices
        _last_prices_time = now
        return prices
    except Exception as e:
        if _last_prices:
            return _last_prices
        raise e

_account_balance_cache = (0.0, None)


def get_account_balance_usdt() -> float | None:
    """即時查詢合約帳戶 USDT 餘額，給 API 進程自己直接查，不依賴 main.py 進程內快取的 REAL_BALANCE
    （main.py 和 API 是兩個獨立進程，各自的模組全域變數互不相通）。"""
    global _account_balance_cache
    now = time.time()
    if now - _account_balance_cache[0] < 5:  # 快取 5 秒，避免頻繁查詢合約餘額
        return _account_balance_cache[1]
    if _binance_banned():
        return None
    try:
        val = None
        for b in client.futures_account_balance():
            if b.get("asset") == "USDT":
                val = float(b.get("balance", 0.0))
                break
        _account_balance_cache = (now, val)
        return val
    except Exception as e:
        _note_binance_ban(e)
        print(f"[BalanceFetch] 讀取合約餘額失敗: {e}")
    return None

_total_pnl_cache = (0, None)

from core.config import get_data_file_path
PNL_BASELINE_PATH = get_data_file_path("pnl_baseline.json")
PNL_SYMBOL_REGISTRY_PATH = get_data_file_path("pnl_symbol_registry.json")


def _normalize_symbol_for_pnl(raw: str) -> str:
    return str(raw or "").upper().replace(":", "").replace("/", "")


def _load_pnl_symbol_registry() -> set:
    try:
        if os.path.exists(PNL_SYMBOL_REGISTRY_PATH):
            import json as _json
            with open(PNL_SYMBOL_REGISTRY_PATH, "r", encoding="utf-8") as f:
                return set(_json.load(f))
    except Exception:
        pass
    return set()


def _save_pnl_symbol_registry(symbols: set) -> None:
    try:
        import json as _json
        with open(PNL_SYMBOL_REGISTRY_PATH, "w", encoding="utf-8") as f:
            _json.dump(sorted(symbols), f)
    except Exception:
        pass

def _compute_raw_realized_pnl_by_symbol() -> dict:
    """回傳 {symbol: 該幣種歷史全部已實現損益}。逐幣種算而不是直接加總成單一數字，
    是為了讓 baseline 也能逐幣種記錄——見下方 get_total_realized_pnl_usdt() 的說明。"""
    result = {}
    try:
        from services.bot_manager_service import load_symbol_config
        from services.radar_service import CORE_SYMBOLS

        # 核心防護：將所有 CORE_SYMBOLS 強制加入查詢名單，
        # 確保任何幣種只要有過手動平倉，就絕對會被算入總已實現損益。
        query_symbols = {_normalize_symbol_for_pnl(s) for s in CORE_SYMBOLS if s}
        query_symbols.update(_normalize_symbol_for_pnl(s) for s in load_symbol_config() if s)
        query_symbols.update(_load_pnl_symbol_registry())
        try:
            from core.config import TRADE_HISTORY_FILE
            import json as _json
            with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = _json.load(f)
            query_symbols.update(_normalize_symbol_for_pnl(t.get("symbol", "")) for t in history if t.get("symbol"))
        except Exception:
            pass
        try:
            for pos in client.futures_position_information():
                qty = float(pos.get("positionAmt", 0) or 0)
                if abs(qty) > 0.000001:
                    query_symbols.add(_normalize_symbol_for_pnl(pos.get("symbol", "")))
        except Exception:
            pass

        query_symbols = {s for s in query_symbols if s}
        _save_pnl_symbol_registry(query_symbols)

        # 逐一向幣安查詢所有幣種成交
        _invalid_syms = []
        for sym in query_symbols:
            if _binance_banned():
                break
            try:
                # 幣安 API 呼叫，若限流或出錯直接拋出，不回傳不完整加總
                trades = client.futures_account_trades(symbol=sym, limit=1000)
                sym_total = 0.0
                for t in trades:
                    fee = float(t.get("commission", 0.0) or 0.0)
                    if fee == 0.0:
                        qty = float(t.get("qty", 0.0) or 0.0)
                        price = float(t.get("price", 0.0) or 0.0)
                        fee = qty * price * 0.0005
                    sym_total += float(t.get("realizedPnl", 0.0) or 0.0) - fee
                result[sym] = sym_total
            except Exception as e:
                _note_binance_ban(e)
                # -1121 代表這個代號在幣安根本不存在（例如登記進 registry 時打錯字、或殘留
                # 非法代號），這種錯誤永遠不會自己好——之前整個函式遇到任何錯誤都直接拋出
                # 讓上層用舊快取，結果 registry 裡一旦混進一個永遠查不到的爛代號（實測
                # NATGASUSDT、OPGUSDT 兩個根本不是幣安合約），總已實現利潤就永遠卡在拋錯
                # 那一刻的舊快取，也就是「一直跑掉」的真正原因。
                # 這種明確的無效代號直接跳過並記錄，其餘可能是限流/網路的錯誤才維持原本
                # 「直接拋出讓上層用快取」的保守作法，避免把不完整的加總誤當成正確結果。
                if "-1121" in str(e) or "Invalid symbol" in str(e):
                    _invalid_syms.append(sym)
                    continue
                raise RuntimeError(f"查詢 {sym} 成交失敗: {e}")

        if _invalid_syms:
            query_symbols -= set(_invalid_syms)
            _save_pnl_symbol_registry(query_symbols)

    except Exception as e:
        # 拋回給上層，由 get_total_realized_pnl_usdt() 決定是否使用舊快取
        raise e
    return result

def _load_pnl_baseline_by_symbol() -> dict:
    try:
        import json as _json
        if os.path.exists(PNL_BASELINE_PATH):
            with open(PNL_BASELINE_PATH, "r", encoding="utf-8") as f:
                data = _json.load(f)
            by_symbol = data.get("baseline_by_symbol")
            if isinstance(by_symbol, dict):
                return {k: float(v) for k, v in by_symbol.items()}
    except Exception:
        pass
    return {}

def _get_pnl_baseline_start_ms() -> int:
    """Use saved set_at, or legacy reset-file mtime when the file is an empty object."""
    try:
        import json as _json
        with open(PNL_BASELINE_PATH, "r", encoding="utf-8") as f:
            data = _json.load(f)
        set_at = float(data.get("set_at", 0.0) or 0.0) if isinstance(data, dict) else 0.0
        if set_at <= 0:
            set_at = os.path.getmtime(PNL_BASELINE_PATH)
        return int(set_at * 1000)
    except Exception:
        return 0


def _save_pnl_baseline_by_symbol(baseline_by_symbol: dict) -> None:
    try:
        import json as _json
        with open(PNL_BASELINE_PATH, "w", encoding="utf-8") as f:
            _json.dump({"baseline_by_symbol": baseline_by_symbol, "set_at": time.time()}, f)
    except Exception:
        pass

def _compute_realized_pnl_since(start_ms: int) -> dict:
    """按 baseline 時間加總成交淨損益，避免 limit=1000 的滾動窗口污染歷史差分。"""
    from services.bot_manager_service import load_symbol_config
    from services.radar_service import CORE_SYMBOLS
    import json as _json
    from core.config import TRADE_HISTORY_FILE

    query_symbols = {_normalize_symbol_for_pnl(s) for s in CORE_SYMBOLS if s}
    query_symbols.update(_normalize_symbol_for_pnl(s) for s in load_symbol_config() if s)
    query_symbols.update(_load_pnl_symbol_registry())
    try:
        with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
            history = _json.load(f)
        query_symbols.update(_normalize_symbol_for_pnl(t.get("symbol", "")) for t in history if t.get("symbol"))
    except Exception:
        pass
    query_symbols = {s for s in query_symbols if s}
    _save_pnl_symbol_registry(query_symbols)

    result = {}
    for sym in query_symbols:
        if _binance_banned():
            raise RuntimeError("Binance API temporarily unavailable")
        total = 0.0
        params = {"symbol": sym, "startTime": int(start_ms), "limit": 1000}
        last_id = None
        while True:
            try:
                trades = client.futures_account_trades(**params)
            except Exception as e:
                _note_binance_ban(e)
                if "-1121" in str(e) or "Invalid symbol" in str(e):
                    break
                raise RuntimeError(f"查詢 {sym} baseline 後成交失敗: {e}")
            for t in trades:
                fee = float(t.get("commission", 0.0) or 0.0)
                if fee == 0.0:
                    qty = float(t.get("qty", 0.0) or 0.0)
                    price = float(t.get("price", 0.0) or 0.0)
                    fee = qty * price * 0.0005
                total += float(t.get("realizedPnl", 0.0) or 0.0) - fee
            if len(trades) < 1000:
                break
            ids = [int(t.get("id")) for t in trades if t.get("id") is not None]
            if not ids:
                break
            next_id = max(ids) + 1
            if last_id is not None and next_id <= last_id:
                break
            last_id = next_id
            params = {"symbol": sym, "fromId": next_id, "limit": 1000}
        result[sym] = total
    return result


def get_realized_pnl_trades_since_baseline() -> list:
    """回傳 baseline 後的幣安原始成交，供歷史筆記本用同一資料源精確加總。"""
    start_ms = _get_pnl_baseline_start_ms()
    if start_ms <= 0:
        return []

    from services.bot_manager_service import load_symbol_config
    from services.radar_service import CORE_SYMBOLS
    query_symbols = {_normalize_symbol_for_pnl(s) for s in CORE_SYMBOLS if s}
    query_symbols.update(_normalize_symbol_for_pnl(s) for s in load_symbol_config() if s)
    query_symbols.update(_load_pnl_symbol_registry())
    query_symbols = {s for s in query_symbols if s}
    _save_pnl_symbol_registry(query_symbols)

    all_trades = []
    for sym in sorted(query_symbols):
        params = {"symbol": sym, "startTime": start_ms, "limit": 1000}
        last_id = None
        while True:
            try:
                trades = client.futures_account_trades(**params)
            except Exception as e:
                _note_binance_ban(e)
                if "-1121" in str(e) or "Invalid symbol" in str(e):
                    break
                raise RuntimeError(f"查詢 {sym} 歷史成交失敗: {e}")
            all_trades.extend(trades)
            if len(trades) < 1000:
                break
            ids = [int(t.get("id")) for t in trades if t.get("id") is not None]
            if not ids:
                break
            next_id = max(ids) + 1
            if last_id is not None and next_id <= last_id:
                break
            last_id = next_id
            params = {"symbol": sym, "fromId": next_id, "limit": 1000}
    return all_trades

def get_total_realized_pnl_usdt() -> float:
    """回傳 baseline 設定時間之後的已實現損益，含所有成交手續費。"""
    global _total_pnl_cache
    start_ms = _get_pnl_baseline_start_ms()
    if start_ms <= 0:
        return 0.0

    now = time.time()
    if now - _total_pnl_cache[0] < 30 and _total_pnl_cache[1] is not None:
        since_by_symbol = _total_pnl_cache[1]
    else:
        try:
            since_by_symbol = _compute_realized_pnl_since(start_ms)
            _total_pnl_cache = (now, since_by_symbol)
        except Exception:
            since_by_symbol = _total_pnl_cache[1] if _total_pnl_cache[1] is not None else {}
    return sum(float(v) for v in since_by_symbol.values())


def reset_total_realized_pnl_baseline() -> float:
    """重置 baseline 並清空快取變數：把「現在每個幣種各自的原始損益」存成新的
    逐幣種 baseline，之後 get_total_realized_pnl_usdt() 只會計算這之後的變化量。"""
    global _total_pnl_cache
    try:
        # 強制重新拉取最新完整數據
        raw_by_symbol = _compute_raw_realized_pnl_by_symbol()
    except Exception:
        # 若當下失敗，使用舊快取，沒有快取就設為空字典
        raw_by_symbol = _total_pnl_cache[1] if _total_pnl_cache[1] is not None else {}

    # 重置快取為當前時間與新值
    _save_pnl_baseline_by_symbol(dict(raw_by_symbol))
    # 新算法的快取是 baseline 後增量，不能沿用重置前的累計 raw map。
    _total_pnl_cache = (0.0, None)
    return sum(raw_by_symbol.values())

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

_all_positions_cache = (0, {})

def get_all_positions():
    # 前端 allPositions 是用「symbol -> 持倉」的物件（跟紙上交易 get_paper_positions() 一樣），
    # 用 `for (let sym in data)` 取 key 直接當幣種名稱。這裡原本回傳陣列，前端迴圈會拿到
    # 「0」「1」這種索引當 key，導致 getPositionInfo() 永遠對不到持倉，交易列表判斷不出
    # 現價、未實現損益。改成回傳用冒號格式符號（跟 get_trades() 一致）當 key 的字典。
    global _all_positions_cache
    now = time.time()
    if now - _all_positions_cache[0] < DASHBOARD_POSITION_CACHE_SEC:
        return _all_positions_cache[1]

    if _binance_banned():
        return _all_positions_cache[1]
    try:
        positions = client.futures_position_information()
    except Exception as e:
        _note_binance_ban(e)
        if _all_positions_cache[1]:
            return _all_positions_cache[1]
        raise
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
            # 真實槓桿可用時會 fallback 到寫死的 20 倍，跟這個幣種實際設定 2~5 倍差很多，
            # 導致百分比顯示被放大成不合理的數字（例如 -4.89% 顯示成 -97.78%）。
            initial_margin = float(pos.get('initialMargin', 0) or 0)
            leverage = round(abs(float(pos.get('notional', 0) or 0)) / initial_margin) if initial_margin > 0 else 0
            key = sym.replace('USDT', ':USDT')
            update_time_ms = int(pos.get('updateTime', 0) or 0)
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
                "realized_pnl": 0,
                "open_time_ms": update_time_ms,
            }
    _all_positions_cache = (now, result)
    return result

def market_buy(symbol: str, amount: float, signal_price: float = None):
    """
    Execute a market buy order with slippage protection and pre-flight price check.
    """
    # Layer 2: Pre-flight Price Check
    ticker = client.futures_symbol_ticker(symbol=symbol)
    current_price = float(ticker["price"])

    # Layer 1: Slippage Filter
    if signal_price is not None:
        slippage_pct = abs(current_price - signal_price) / signal_price
        
        # Threshold: 0.5% default. (1.0% for high volatility coins - logic to be refined)
        threshold = 0.005
        
        if slippage_pct > threshold:
            print(f"[Slippage Filter] Order cancelled for {symbol}: "
                  f"Signal {signal_price}, Current {current_price} (Slippage {slippage_pct:.2%})")
            return None

    # Use current_price for quantity calculation to ensure it matches what we see now
    qty = amount / current_price
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
    
    # Capture the actual fill price (avgPrice) from the exchange response
    # For USDCUSDT limit order, avgPrice might not be in the immediate response, 
    # so we fall back to the order price or current_price.
    fill_price = float(order.get("avgPrice", current_price))
    if symbol == 'USDCUSDT' and "avgPrice" not in order:
        fill_price = 0.9999

    return {
        "order": order,
        "fill_price": fill_price
    }

def market_short(symbol: str, amount: float, signal_price: float = None):
    """
    Execute a market short order with slippage protection and pre-flight price check.
    """
    # Layer 2: Pre-flight Price Check - 使用快取獲取當前價格
    ticker_data = ctx.CACHE.get_ticker(symbol) if ctx.CACHE else None
    if ticker_data:
        current_price = float(ticker_data.get("price", 0))
    else:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        current_price = float(ticker["price"])

    # Layer 1: Slippage Filter
    if signal_price is not None:
        slippage_pct = abs(current_price - signal_price) / signal_price
        
        # Threshold: 0.5% default. (1.0% for high volatility coins - logic to be refined)
        threshold = 0.005
        
        if slippage_pct > threshold:
            print(f"[Slippage Filter] Order cancelled for {symbol}: "
                  f"Signal {signal_price}, Current {current_price} (Slippage {slippage_pct:.2%})")
            return None

    qty = amount / current_price
    step = get_contract_step(symbol)
    qty_str = str(round_step(qty, step))

    order = client.futures_create_order(
        symbol=symbol,
        side=Client.SIDE_SELL,
        type=Client.ORDER_TYPE_MARKET,
        quantity=qty_str
    )
    
    # Capture the actual fill price (avgPrice) from the exchange response
    fill_price = float(order.get("avgPrice", current_price))
    return {
        "order": order,
        "fill_price": fill_price
    }

def market_sell(symbol: str, base_asset: str):
    positions = client.futures_position_information(symbol=symbol)
    if not positions:
        raise Exception("找不到合約倉位資訊")

    qty = float(positions[0]['positionAmt'])
    if qty == 0:
        raise Exception("當前無合約倉位可平倉")

    side = Client.SIDE_SELL if qty > 0 else Client.SIDE_BUY
    step = get_contract_step(symbol)
    abs_qty = abs(qty)
    qty_str = str(round_step(abs_qty, step))

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
        # 市價單另有比一般 LOT_SIZE 更低的 MARKET_LOT_SIZE 單筆數量上限（見
        # get_market_max_qty 說明），超過就直接被拒單、部位卡住平不掉。這裡切成
        # 多筆市價單分批送出；回傳最後一筆的訂單資訊供呼叫端查詢成交價使用。
        max_qty = get_market_max_qty(symbol)
        if max_qty and max_qty > 0 and abs_qty > max_qty:
            remaining = abs_qty
            order = None
            while remaining > 0.000001:
                chunk = min(remaining, max_qty)
                chunk = round_step(chunk, step) if step > 0 else chunk
                if chunk <= 0:
                    break
                order = client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=chunk
                )
                remaining -= chunk
        else:
            order = client.futures_create_order(
                symbol=symbol,
                side=side,
                type=Client.ORDER_TYPE_MARKET,
                quantity=abs_qty
            )
    # 返回完整訂單資訊，以便後續更新真實成交價與數量
    return order

_trades_cache = {}

def _aggregate_fills_by_order(raw_trades: list) -> list:
    """把同一張委託單（同一個 orderId）底下的多筆分批成交合併成一筆。
    幣安的市價/帳戶單常常不是跟單一對手方一次成交完，而是依序吃掉委託簿上好幾個
    價位，一張委託單因此會產生好幾筆各自獨立的原始成交紀錄——這在交易列表上會讓
    使用者以為同一次進出場「分好幾批下單」，也讓 get_trades("ALL") 那個「全部幣種
    合計最新 30 筆」的裁切機制被灌爆：一次補倉/進場動輒拆成 10~20 筆小額成交，
    多佔用好幾個名額，導致真正重要、稍早一點（甚至只是幾十分鐘前）的其他平倉紀錄
    被擠出前 30 筆，使用者自己手動平倉的紀錄反而在畫面上找不到。合併後同一張委託
    只算一筆，數量加總、價格用成交金額加權平均、已實現損益與手續費加總。"""
    if not raw_trades:
        return []
    groups = {}
    order_ids = []
    for t in raw_trades:
        key = t.get("orderId")
        if key is None:
            key = f"_no_order_{t.get('id')}"
        if key not in groups:
            groups[key] = []
            order_ids.append(key)
        groups[key].append(t)

    merged = []
    for key in order_ids:
        fills = groups[key]
        if len(fills) == 1:
            merged.append(fills[0])
            continue
        total_qty = sum(float(f["qty"]) for f in fills)
        total_notional = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        avg_price = total_notional / total_qty if total_qty > 0 else float(fills[0]["price"])
        merged.append({
            **fills[-1],
            "price": avg_price,
            "qty": total_qty,
            "time": max(f.get("time", 0) for f in fills),
            "realizedPnl": sum(float(f.get("realizedPnl", 0.0) or 0.0) for f in fills),
            "commission": sum(float(f.get("commission", 0.0) or 0.0) for f in fills),
        })
    return merged

def get_trades(symbol: str):
    if _binance_banned():
        return []

    import time as _time
    now = _time.time()
    if symbol in _trades_cache:
        cache_time, cached_val = _trades_cache[symbol]
        if now - cache_time < DASHBOARD_TRADE_CACHE_SEC:
            return cached_val

    if symbol == "ALL":
        # 幣安沒有「查所有幣種成交」的單一端點，逐一查目前監控的幣種再合併排序。
        # 只查目前監控池會漏掉已經輪替出池子的幣種（例如雷達換幣後），導致之前明明
        # 有成交的幣種從交易列表看不到，所以額外併入本機 trade_history.json 記錄過的幣種，
        # 確保歷史成交不會因為幣種被換出監控池就從畫面上憑空消失。
        from services.bot_manager_service import load_symbol_config
        import json as _json
        from core.config import TRADE_HISTORY_FILE
        def _normalize_symbol(raw: str) -> str:
            s = str(raw or "").upper().replace(":", "").replace("/", "")
            return s

        query_symbols = {_normalize_symbol(s) for s in load_symbol_config() if s}
        try:
            with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = _json.load(f)
            query_symbols.update(
                _normalize_symbol(t.get("symbol", ""))
                for t in history
                if t.get("symbol")
            )
        except Exception:
            pass

        try:
            # 目前仍有持倉的幣種必須一併查成交，避免幣種已移出監控池後
            # 交易列表看不到該幣，導致前端無法從交易紀錄執行手動平倉。
            for pos in client.futures_position_information():
                qty = float(pos.get("positionAmt", 0) or 0)
                if abs(qty) > 0.000001:
                    query_symbols.add(_normalize_symbol(pos.get("symbol", "")))
        except Exception:
            pass

        query_symbols = {s for s in query_symbols if s}
        all_trades = []
        for sym in query_symbols:
            if _binance_banned():
                break
            try:
                all_trades.extend(client.futures_account_trades(symbol=sym, limit=15))
            except Exception as e:
                _note_binance_ban(e)
                continue
        all_trades = _aggregate_fills_by_order(all_trades)
        all_trades.sort(key=lambda t: t.get("time", 0), reverse=True)
        # 目前仍有真實持倉的幣種，其真實成交（含正確手續費）一定要保留，不能被 30 筆
        # 上限擠掉——之前發生過某幣種的入場成交超過 30 筆之外的幣種，導致後面「補入未列出持倉」那段只能拿部位資訊湊一筆假紀錄，手續費/已實現
        # 損益全部顯示 0，使用者看到的手續費永遠是 0.0000。改成：目前持倉的幣種永遠
        # 保留其真實成交，其餘幣種的成交才受 30 筆上限限制。
        try:
            open_syms_for_cap = {
                str(sym or "").replace(":", "").replace("/", "").upper()
                for sym in get_all_positions().keys()
            }
        except Exception:
            open_syms_for_cap = set()
        open_pos_trades = [t for t in all_trades if str(t.get("symbol", "")).upper() in open_syms_for_cap]
        other_trades = [t for t in all_trades if str(t.get("symbol", "")).upper() not in open_syms_for_cap]
        remaining_slots = max(0, 30 - len(open_pos_trades))
        capped = open_pos_trades + other_trades[:remaining_slots]
        capped.sort(key=lambda t: t.get("time", 0), reverse=True)
        trades = list(reversed(capped))
    else:
        trades = _aggregate_fills_by_order(client.futures_account_trades(symbol=symbol, limit=15))
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
    # ── 補入「有持倉但不在成交列表」的幣種 ──────────────────────────────────
    # 目的：確保 AVAX/HBAR 等已開倉但入場成交超過 30 筆之外的幣種，
    #       仍然能以「開倉中」狀態顯示在前端交易記錄最頂端。
    if symbol == "ALL":
        try:
            existing_syms = {t["symbol"] for t in formatted_trades if not t.get("is_close")}
            all_pos = get_all_positions()
            import time as _time_mod
            now_ms = int(_time_mod.time() * 1000)
            for pos_key, pos in all_pos.items():
                if pos_key not in existing_syms:
                    qty = float(pos.get("qty", 0))
                    if abs(qty) < 0.000001:
                        continue
                    entry_price = float(pos.get("entryPrice", 0))
                    # 判斷方向：qty > 0 → 做多 (買入)，qty < 0 → 做空 (賣出)
                    is_long = qty > 0
                    # 以幣安的入場時間為準（若有），否則用當前時間
                    formatted_trades.append({
                        "id": f"pos_{pos_key}",
                        "order_id": None,
                        "symbol": pos_key,
                        "price": entry_price,
                        "qty": abs(qty),
                        "time": now_ms,        # 佔位；確保排在最前面由前端處理
                        "isBuyer": is_long,
                        "is_close": False,     # 開倉中
                        "realized_pnl": 0.0,
                        "fee": 0.0,
                        "_is_open_position": True,  # 標記為補入的持倉記錄
                    })
        except Exception:
            pass
    # ─────────────────────────────────────────────────────────────────────────
    _trades_cache[symbol] = (now, formatted_trades)
    return formatted_trades

def get_klines(symbol: str, interval: str, limit: int):
    cache_key = (symbol, interval, int(limit))
    now = time.time()
    cached = _kline_cache.get(cache_key)
    if cached and now - cached[0] < DASHBOARD_KLINE_CACHE_SEC:
        return cached[1]
    if _binance_banned() and cached:
        return cached[1]
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    except Exception as e:
        _note_binance_ban(e)
        if cached:
            return cached[1]
        raise
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
    _kline_cache[cache_key] = (now, result)
    return result

def calculate_entry_readiness(klines):
    """Score proximity to the closed-candle MA7/25/99 entry setups."""
    empty = {"score": 0.0, "direction": "none", "long_score": 0.0, "short_score": 0.0}
    if not klines or len(klines) < 101:
        return empty

    completed = klines[:-1]
    closes = np.asarray([float(k[4]) for k in completed], dtype=float)
    highs = np.asarray([float(k[2]) for k in completed], dtype=float)
    lows = np.asarray([float(k[3]) for k in completed], dtype=float)
    volumes = np.asarray([float(k[5]) for k in completed], dtype=float)
    if closes.size < 100 or np.any(closes <= 0):
        return empty

    price = float(closes[-1])
    ma7, ma25, ma99 = (float(np.mean(closes[-period:])) for period in (7, 25, 99))
    prev_ma7 = float(np.mean(closes[-8:-1]))
    prev_ma25 = float(np.mean(closes[-26:-1]))
    gap = ma7 - ma25
    prev_gap = prev_ma7 - prev_ma25
    gap_pct = abs(gap) / price
    true_ranges = np.maximum(
        highs[-14:] - lows[-14:],
        np.maximum(abs(highs[-14:] - closes[-15:-1]), abs(lows[-14:] - closes[-15:-1])),
    )
    atr = max(float(np.mean(true_ranges)), price * 0.001)
    vol_ma20 = max(float(np.mean(volumes[-21:-1])), 1e-12)
    volume_ratio = float(volumes[-1] / vol_ma20)
    prior_high = float(np.max(highs[-21:-1]))
    prior_low = float(np.min(lows[-21:-1]))

    golden_cross = prev_ma7 <= prev_ma25 and ma7 > ma25
    death_cross = prev_ma7 >= prev_ma25 and ma7 < ma25
    near_cross = gap_pct <= max(0.0015, atr / price * 0.35)
    long_approaching = ma7 <= ma25 and ma7 > prev_ma7 and gap > prev_gap
    short_approaching = ma7 >= ma25 and ma7 < prev_ma7 and gap < prev_gap
    long_spreading = ma7 > ma25 and ma7 > prev_ma7 and gap > max(prev_gap, 0.0)
    short_spreading = ma7 < ma25 and ma7 < prev_ma7 and gap < min(prev_gap, 0.0)
    near_ma25 = abs(price - ma25) <= atr * 0.8
    near_high = prior_high - atr * 0.5 <= price <= prior_high + atr * 0.2
    near_low = prior_low - atr * 0.2 <= price <= prior_low + atr * 0.5
    volume_ready = min(volume_ratio / 0.8, 1.0)

    long_score = (
        0.25 * bool(price > ma99)
        + 0.20 * bool(ma7 > ma25 or (near_cross and long_approaching))
        + 0.15 * bool(golden_cross or long_spreading or (near_cross and long_approaching))
        + 0.20 * bool(near_ma25 and price >= ma25)
        + 0.10 * bool(near_high)
        + 0.10 * volume_ready
    )
    short_score = (
        0.25 * bool(price < ma99)
        + 0.20 * bool(ma7 < ma25 or (near_cross and short_approaching))
        + 0.15 * bool(death_cross or short_spreading or (near_cross and short_approaching))
        + 0.20 * bool(near_ma25 and price <= ma25)
        + 0.10 * bool(near_low)
        + 0.10 * volume_ready
    )
    direction = "long" if long_score >= short_score else "short"
    score = max(long_score, short_score)
    setup = (
        "cross" if golden_cross or death_cross
        else "ma25_pullback" if near_ma25
        else "breakout" if near_high or near_low
        else "trend_wait"
    )
    return {
        "score": round(float(score), 4),
        "direction": direction if score >= 0.5 else "none",
        "long_score": round(float(long_score), 4),
        "short_score": round(float(short_score), 4),
        "setup": setup,
        "volume_ratio": round(volume_ratio, 3),
        "ma_gap_pct": round(gap_pct, 5),
    }


def get_1h_market_features(symbol: str):
    try:
        # One 5m request supplies both the last-hour movement and MA7/25/99 readiness.
        klines = market_client.futures_klines(symbol=symbol, interval='5m', limit=105)
        if not klines:
            return symbol, 0.0, calculate_entry_readiness([])
        recent = klines[-13:-1] if len(klines) >= 13 else klines[:-1]
        highs = [float(k[2]) for k in recent]
        lows = [float(k[3]) for k in recent]
        vols = [float(k[7]) for k in recent]

        h = max(highs)
        l = min(lows)
        q_vol = sum(vols)
        volatility = 0.0
        if l > 0 and q_vol > 1_000_000:
            volatility = ((h - l) / l) * 100
        return symbol, volatility, calculate_entry_readiness(klines)
    except:
        pass
    return symbol, 0.0, calculate_entry_readiness([])


def get_1h_volatility(symbol: str):
    symbol, volatility, _ = get_1h_market_features(symbol)
    return symbol, volatility

_atr_rankings_cache = {}

def get_atr_ranked_coins(symbols=None, limit=10, blacklist=None):
    """Rank symbols by tradable momentum: medium-high daily ATR plus recent 1h movement.
    
    Daily ATR alone tends to select coins that were violent yesterday but are flat now.
    Add 1h volatility and 24h change so radar can prefer active-but-not-chaotic markets.
    """
    if _binance_banned():
        return [], []
    import time as _time
    now = _time.time()

    ticker_map = {}
    try:
        ticker_map = {t.get("symbol"): t for t in market_client.futures_ticker()}
    except Exception:
        ticker_map = {}

    if not symbols:
        symbols = []
        try:
            from core.exchange_client import exchange_market_data, convert_to_ccxt_symbol
            ccxt_markets = exchange_market_data.markets
            if not ccxt_markets:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if not loop.is_running():
                        loop.run_until_complete(exchange_market_data.load_markets())
                        ccxt_markets = exchange_market_data.markets
                except Exception:
                    pass
            if not ccxt_markets:
                try:
                    import ccxt
                    sync_exchange = ccxt.binance({'options': {'defaultType': 'future', 'fetchMarkets': ['linear']}})
                    sync_exchange.load_markets()
                    ccxt_markets = sync_exchange.markets
                except Exception as e:
                    print(f"[ATR Rank] Sync exchange load markets failed: {e}")
            ccxt_markets = ccxt_markets or {}
            
            candidates = []
            for t in ticker_map.values():
                sym = t.get("symbol", "")
                if sym.endswith("USDT") and "_" not in sym:
                    if blacklist and sym in blacklist:
                        continue
                    # 確保該幣種存在於 CCXT 的可交易期貨清單中，避開 BZUSDT / CLUSDT 等商品期貨
                    ccxt_sym = convert_to_ccxt_symbol(sym)
                    ccxt_perp_sym = f"{ccxt_sym}:{sym[-4:]}"
                    if ccxt_perp_sym not in ccxt_markets:
                        continue
                    
                    market_info = ccxt_markets.get(ccxt_perp_sym, {})
                    info_dict = market_info.get("info", {}) if isinstance(market_info, dict) else {}
                    # 過濾非加密貨幣合約 (例如 underlyingType: COMMODITY, contractType: TRADIFI_PERPETUAL)
                    if info_dict.get("underlyingType") == "COMMODITY" or "TRADIFI" in str(info_dict.get("contractType", "")):
                        continue
                        
                    q_vol = float(t.get("quoteVolume", 0.0) or 0.0)
                    # 確保交易量足夠大以避免小幣/土狗
                    if q_vol >= 15000000.0:
                        candidates.append((sym, q_vol))
            candidates.sort(key=lambda x: x[1], reverse=True)
            # 取最活躍的 50 個幣種做 ATR 排行，防止呼叫過多 klines 觸發 429 限流
            symbols = [x[0] for x in candidates[:50]]
        except Exception as e:
            print(f"[ATR Rank] Error getting all futures tickers: {e}")
            symbols = ["NEARUSDT", "AVAXUSDT", "UNIUSDT", "ETCUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT", "LINKUSDT", "AAVEUSDT"]

    cache_key = tuple(sorted(symbols))
    if cache_key in _atr_rankings_cache:
        cached_at, cached_val = _atr_rankings_cache[cache_key]
        if now - cached_at < 300:
            selected = [r["symbol"] for r in cached_val[:limit]]
            return selected, cached_val
        _atr_rankings_cache.pop(cache_key, None)

    ranked = []
    for sym in symbols:
        try:
            klines = market_client.futures_klines(symbol=sym, interval='1d', limit=16)
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
            _, one_h_vol, readiness = get_1h_market_features(sym)
            ticker = ticker_map.get(sym, {})
            try:
                change_pct = float(ticker.get("priceChangePercent", 0.0) or 0.0)
                q_vol = float(ticker.get("quoteVolume", 0.0) or 0.0)
            except (TypeError, ValueError):
                change_pct = 0.0
                q_vol = 0.0
            # Market quality plus live entry readiness. This keeps ATR selection aligned
            # with the actual entry engine instead of choosing yesterday's volatile coin.
            # readiness_component 內建的 volume_ratio 子分只佔它自己 10%（換算到總分
            # 不到 5%），90% 都是結構位置（MA排列、靠不靠近前高低點）——這些條件在
            # 盤整安靜期一樣能成立，選進來的幣「結構就緒」但沒有真的量能推動，實際
            # 進場後常常在鎖利/止損線附近反覆拉鋸，變成一連串小虧（實測 HYPEUSDT/
            # ADAUSDT 案例）。把即時量能比獨立拉成一個頂層「活躍度」因子並加重
            # one_h_component 權重，降低結構就緒度的主導地位，讓選幣更看重「現在是
            # 不是真的在動」，不只是「結構位置對不對」。
            one_h_component = min(max(one_h_vol, 0.0), 2.5) / 2.5
            atr_component = min(max(atr_pct, 0.0), 6.0) / 6.0
            volume_component = min(q_vol / 100_000_000, 1.0)
            readiness_component = float(readiness.get("score", 0.0) or 0.0)
            activity_component = min(max(float(readiness.get("volume_ratio", 0.0) or 0.0), 0.0) / 1.5, 1.0)
            score = (
                atr_component * 0.25
                + one_h_component * 0.30
                + volume_component * 0.10
                + activity_component * 0.15
                + readiness_component * 0.20
            )
            ranked.append({
                "symbol": sym,
                "atr_pct": atr_pct,
                "price": price,
                "one_h_vol_pct": round(one_h_vol, 3),
                "change_pct": round(change_pct, 3),
                "q_vol": q_vol,
                "entry_readiness_score": round(readiness_component, 4),
                "entry_direction": readiness.get("direction", "none"),
                "entry_long_score": readiness.get("long_score", 0.0),
                "entry_short_score": readiness.get("short_score", 0.0),
                "entry_setup": readiness.get("setup", "trend_wait"),
                "entry_volume_ratio": readiness.get("volume_ratio", 0.0),
                "momentum_score": round(score, 4),
            })
        except Exception as e:
            print(f"[ATR Rank] {sym} error: {e}")
    ranked.sort(key=lambda x: x["momentum_score"], reverse=True)
    _atr_rankings_cache[cache_key] = (now, ranked)
    selected = [r["symbol"] for r in ranked[:limit]]
    return selected, ranked
