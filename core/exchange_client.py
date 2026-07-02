import logging
import os
import math
import ccxt
import ccxt.pro as ccxtpro
from dotenv import load_dotenv
from core.config import USE_TESTNET

logger = logging.getLogger(__name__)

load_dotenv()

exchange_futures = ccxtpro.binance({
    'apiKey': os.getenv('BINANCE_API_KEY') or None,
    'secret': os.getenv('BINANCE_API_SECRET') or None,
    'enableRateLimit': True,
    'rateLimit': 200,
    'options': {
        'defaultType': 'future',
        'watchOrderBookSnapshot': True,
        # 這個機器人只交易 USDT 本位永續合約 (linear)，load_markets() 預設卻會同時抓
        # spot/linear/inverse 三種市場資料。Demo Trading 帳戶不支援現貨（spot）的
        # exchangeInfo 端點，一起抓的話會讓整個 load_markets() 失敗（ExchangeNotAvailable），
        # 進而讓某些幣種抓不到正確市場資訊、K線請求 fallback 到錯的網址。限定只抓 linear
        # 可以避開這個問題，兩邊環境（正式/Demo）都適用，也順便減少不必要的 API 呼叫。
        'fetchMarkets': ['linear'],
    },
})

if USE_TESTNET:
    # 用的是幣安「Demo Trading」網頁申請的金鑰，跟舊版期貨測試網（testnet.binancefuture.com）
    # 是不同系統、不同網址（demo-fapi.binance.com），要用 enable_demo_trading 對應到正確網址，
    # 這是 ccxt 官方目前支援的方式，不像舊版 set_sandbox_mode 需要額外繞過棄用警告。
    exchange_futures.enable_demo_trading(True)

    # Demo Trading 的行情資料（K線、ATR、掛單簿）是獨立模擬撮合的，跟真實市場的價格、量級
    # 都對不上（實測價格可以差到 0.8%，掛單簿量級能差到 2000 倍），拿來算 RSI/MACD/ATR 這些
    # 進出場指標會跟真實市場脫節。這裡另開一條「不啟用 Demo Trading」的連線，只用來查公開
    # 行情數據（不需要 API Key），讓訊號判斷跟 8005（紙上交易，讀真實市場）完全一致；
    # 真的下單、查餘額、查持倉還是走上面那條 Demo Trading 連線，一樣驗證真實下單流程。
    exchange_market_data = ccxtpro.binance({
        'enableRateLimit': True,
        'rateLimit': 200,
        'options': {
            'defaultType': 'future',
            'watchOrderBookSnapshot': True,
            'fetchMarkets': ['linear'],
        },
    })
else:
    # 正式環境本來就沒有開 Demo Trading，查行情跟下單本來就是同一條真實連線，不需要另開。
    exchange_market_data = exchange_futures

_PRECISION_CACHE = {}


def convert_to_ccxt_symbol(symbol: str) -> str:
    symbol = str(symbol).upper().strip()
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    return symbol


async def get_contract_precision(sym: str):
    if sym in _PRECISION_CACHE:
        return _PRECISION_CACHE[sym]

    ccxt_symbol = convert_to_ccxt_symbol(sym)
    if not exchange_futures.markets:
        try:
            await exchange_futures.load_markets()
        except Exception:
            pass

    try:
        market = exchange_futures.market(ccxt_symbol)
        amount_limits = market.get('limits', {}).get('amount', {})
        step_size = float(amount_limits.get('min', 0.001) or 0.001)
        min_qty = float(amount_limits.get('min', step_size) or step_size)
        precision = int(round(-math.log10(step_size))) if step_size > 0 else 8
        _PRECISION_CACHE[sym] = {
            'step_size': step_size,
            'min_qty': min_qty,
            'qty_prec': market.get('precision', {}).get('amount', precision),
            'price_prec': market.get('precision', {}).get('price', precision)
        }
    except Exception:
        _PRECISION_CACHE[sym] = {
            'step_size': 0.001,
            'min_qty': 0.001,
            'qty_prec': 3,
            'price_prec': 3
        }

    return _PRECISION_CACHE[sym]


def round_step(qty, step_size):
    if qty <= 0 or step_size <= 0:
        return 0.0
    precision = int(round(-math.log10(step_size)))
    rounded = round(qty / step_size) * step_size
    return round(rounded, precision)


async def sanitize_order_qty(sym: str, qty: float):
    prec = await get_contract_precision(sym)
    qty = round_step(qty, prec['step_size'])
    if qty < prec['min_qty']:
        return 0.0
    return qty


async def get_reference_price(sym: str, exchange=None) -> float:
    """挑選一個更貼近牌價的參考價：優先 mark price，其次委託簿中位數，最後回退最新成交價。
    exchange 參數預設用本模組的 exchange_futures；呼叫端可傳入自己 import 進去的實例，
    確保測試 mock 該呼叫端模組的 exchange_futures 時，這裡也會用到同一個 mock。"""
    ex = exchange if exchange is not None else exchange_futures
    try:
        mark = await ex.fetch_mark_price(sym)
        mark_price = float(mark.get("markPrice") or 0)
        if mark_price > 0:
            return mark_price
    except Exception:
        pass

    try:
        book = await ex.fetch_order_book(sym, limit=5)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            bid_price = float(bids[0][0])
            ask_price = float(asks[0][0])
            midpoint = (bid_price + ask_price) / 2.0
            if midpoint > 0:
                return midpoint
    except Exception:
        pass

    try:
        ticker = await ex.fetch_ticker(sym)
        return float(ticker.get("last") or 0)
    except Exception:
        return 0.0


def check_binance_weight():
    try:
        headers = getattr(exchange_futures, 'last_response_headers', {})
        weight = None
        for k, v in headers.items():
            if k.lower() == 'x-mbx-used-weight-1m':
                weight = int(v)
                break
        if weight is not None:
            # 幣安期貨真實權重上限是每分鐘 2400（不是 1200，那是下單次數的獨立限制），
            # 門檻對應調整，避免權重還有很多餘裕就誤觸發不必要的自我限速。
            if weight > 1800:
                logger.info(f"⚠️ [API限流警報] 幣安目前權重已達 {weight}/2400，觸發重度防護，冷卻 10 秒")
                return 10.0
            elif weight > 1400:
                logger.info(f"⚠️ [API限流警報] 幣安目前權重已達 {weight}/2400，觸發輕度防護，冷卻 3 秒")
                return 3.0
    except Exception as e:
        logger.info(f"⚠️ [API權重讀取失敗] {e}")
    return 0.0
