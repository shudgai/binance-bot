import asyncio
import ccxt
import ccxt.pro as ccxtpro
import numpy as np
import json
import os
import time
import datetime



class dotdict(dict):
    """dot.notation access to dictionary attributes"""
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

import logging

# Setup structured logging
logger = logging.getLogger("multi_coin_bot")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
ch = logging.StreamHandler()
ch.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(ch)


from dotenv import load_dotenv
from services.utils import paper_key
from update_paper_state import update_paper_state

load_dotenv()

exchange = ccxtpro.binance({
    'apiKey': os.getenv('BINANCE_API_KEY') or None,
    'secret': os.getenv('BINANCE_API_SECRET') or None,
    'enableRateLimit': True,
    'rateLimit': 1000,
    'options': {
        'defaultType': 'swap',
        'watchOrderBookSnapshot': True,
    },
})
USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
PAPER_TRADING = True
TIMEFRAME = '1m'
LEVERAGE = 5
RSI_PERIOD = 9
VOLUME_RATIO_THRESHOLD = 1.5

if USE_TESTNET:
    exchange.urls['api']['fapiPublic'] = 'https://testnet.binancefuture.com/fapi/v1'
    exchange.urls['api']['fapiPrivate'] = 'https://testnet.binancefuture.com/fapi/v1'

DEFAULT_SYMBOLS = [
    "SOLUSDT", "XRPUSDT", "DOGEUSDT", "SUIUSDT", "1000PEPEUSDT",
    "WIFUSDT", "JUPUSDT", "LINKUSDT", "BNBUSDT", "BTCUSDT"
]
SLOW_OR_LOW_QUALITY_SYMBOLS = {
    "ETH", "AERO", "ADA", "AVAX", "DOT", "UNI", "NEAR", "FET"
}
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "bot_symbols.json")


def normalize_symbol(sym):
    if sym is None:
        return ""
    sym = str(sym).strip().upper()
    if not sym:
        return ""
    if sym in ("PEPE", "PEPEUSDT"):
        return "1000PEPEUSDT"
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    return sym


BANNED_TOKENS = ['CROSS', 'HANA', 'COAI', 'PHA', 'BAN', 'FOGO', 'ESPORTS', 'PLAY', 'HOME', 'VELVET', 'AIO', 'ALLO']

def filter_short_term_symbols(symbols, max_count=10):
    normalized = []
    seen = set()
    for item in normalize_symbol_list(symbols, max_count=max_count * 2):
        sym = normalize_symbol(item)
        if not sym:
            continue
        base_asset = sym.replace('USDT', '')
        if base_asset in BANNED_TOKENS or sym in SLOW_OR_LOW_QUALITY_SYMBOLS or base_asset in SLOW_OR_LOW_QUALITY_SYMBOLS:
            continue
        if sym not in seen:
            normalized.append(sym)
            seen.add(sym)
    if not normalized:
        normalized = list(DEFAULT_SYMBOLS[:max_count])
    return normalized[:max_count]


def normalize_symbol_list(symbols, max_count=20):
    if isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        return list(DEFAULT_SYMBOLS[:max_count])

    seen = []
    for item in symbols:
        sym = normalize_symbol(item)
        if not sym:
            continue
        base_asset = sym.replace('USDT', '')
        if base_asset in BANNED_TOKENS:
            logger.info(f"🚫 [過濾] {sym} 被列在黑名單中，拒絕加入監控。")
            continue
            
        if sym not in seen:
            seen.append(sym)

    return seen[:max_count]


def load_symbol_pool():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return normalize_symbol_list(data.get("symbols", []))
        return normalize_symbol_list(data)
    except FileNotFoundError:
        return list(DEFAULT_SYMBOLS)
    except Exception as e:
        logger.warning(f"⚠️ 讀取幣種清單失敗: {e}")
        return list(DEFAULT_SYMBOLS)


def save_symbol_pool(symbols):
    normalized = normalize_symbol_list(symbols)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"symbols": normalized}, f, ensure_ascii=False)
    return normalized


ALL_SYMBOLS = load_symbol_pool()

MAX_POSITIONS = 5
COOLDOWN_SEC = 180
MAIN_LOOP_INTERVAL_SEC = 3
PENDING_CONFIRM_SEC = 1.0
BAN_WINDOW = 3600
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 3
SL_ATR_MULTIPLIER = 1.5
TP_ATR_MULTIPLIER = 1.8
HARD_STOP_LOSS_PCT = 0.01

HIGH_VOLATILITY_COINS = ["NEAR/USDT", "FET/USDT", "INJ/USDT", "WIF/USDT", "PEPE/USDT", "FLOKI/USDT", "BONK/USDT", "ORDI/USDT"]

def build_symbol_state(sym):
    state_dict = {
        "status": "ACTIVE",
        "status_reason": "",
        "next_status_time": 0,
        "stop_count": 0,
        "stop_timestamps": [],
        "is_breakeven_set": False,
        "first_stop_time": 0,
        "qty": 0.0,
        "avg_price": 0.0,
        "open_time": 0.0,
        "current_atr": 0.0,
        "atr_history": [],
        "atr_ma20": 0.0,
        "current_rsi": 50.0,
        "ema20": 0.0,
        "ema50": 0.0,
        "macd_line": 0.0,
        "macd_signal": 0.0,
        "macd_hist": 0.0,
        "prev_macd_line": 0.0,
        "prev_macd_signal": 0.0,
        "bb_up": 0.0,
        "bb_mid": 0.0,
        "bb_low": 0.0,
        "vol_ma20": 0.0,
        "current_vol": 0.0,
        "trailing_highest": 0.0,
        "trailing_lowest": float('inf'),
        "highest_profit_pct": 0.0,
        "has_partial_closed": False,
        "ohlcv": [],
        "closes": [],
        "tr_list": [],
        "prev_close": None,
        "last_trade_price": 0.0,
        "last_trade_qty": 0.0,
        "last_trade_side": "",
        "last_trade_time": 0.0,
        "trade_qty_history": [],
        "trade_price_history": [],
        "trade_signal_strength": 0.0,
        "trade_signal_reason": "",
        "pending_side": None,
        "pending_time": 0,
        "pending_confirm_high": 0,
        "pending_confirm_low": 0,
        "close_price": 0.0,
        "last_buy_time": 0,
        "signal_strength": 0.0,
        "pnl_history": [],
        "has_been_negative": False,
        "trail_tp_price": 0.0,
        "entry_count": 0,
        "avg_entry_price": 0.0,
        "max_additional_entries": 2,
        "entry_cooldown_sec": 45,
        "last_entry_time": 0.0,
        "htf_trend": None,
        "htf_ema20": 0.0,
        "rsis": [],
        "sma200_15m": 0.0,
        "max_strength": 0.0,
        "entry_route": "a",
        "last_stop_loss_side": "",
        "last_stop_loss_time": 0.0,
    }
    return dotdict(state_dict)

GLOBAL_STATE = {
    "daily_pnl": 0.0,
    "last_reset_day": "",
    "initial_daily_equity": 0.0,
    "consecutive_losses": 0,
    "trading_enabled": True,
    "route_stats": {"a": {"win": 0, "loss": 0}, "b": {"win": 0, "loss": 0}, "c": {"win": 0, "loss": 0}, "s": {"win": 0, "loss": 0}}
}

STATE_SYMBOLS = list(dict.fromkeys(DEFAULT_SYMBOLS + ALL_SYMBOLS))
STATES = {sym: build_symbol_state(sym) for sym in STATE_SYMBOLS}
WATCH_TASKS = {}
STATES_LOCK = None

# ── 指標計算函數 ──────────────────────────────────────────────

def calculate_ema(prices, period):
    if len(prices) == 0: return 0.0
    ema = np.zeros_like(prices)
    ema[0] = prices[0]
    multiplier = 2 / (period + 1)
    for i in range(1, len(prices)):
        ema[i] = (prices[i] - ema[i-1]) * multiplier + ema[i-1]
    return ema[-1]

def calculate_rsi_array(prices, period=14):
    if len(prices) <= period:
        return np.full_like(prices, 50.0)
    deltas = np.diff(prices)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down if down != 0 else 0
    rsi = np.zeros_like(prices)
    rsi[:period] = 100. - 100. / (1. + rs)
    for i in range(period, len(prices)):
        delta = deltas[i - 1]
        if delta > 0:
            upval = delta
            downval = 0.
        else:
            upval = 0.
            downval = -delta
        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period
        rs = up / down if down != 0 else 0
        rsi[i] = 100. - 100. / (1. + rs)
    return rsi

def calculate_macd(prices, slow=26, fast=12, signal=9):
    if len(prices) < slow + signal:
        return 0, 0, 0, 0, 0
    ema_fast = np.array([calculate_ema(prices[:i+1], fast) for i in range(fast-1, len(prices))])
    ema_slow = np.array([calculate_ema(prices[:i+1], slow) for i in range(slow-1, len(prices))])
    macd_line = ema_fast[-1] - ema_slow[-1]
    prev_macd_line = ema_fast[-2] - ema_slow[-2] if len(ema_fast) >= 2 and len(ema_slow) >= 2 else macd_line
    macd_vals = ema_fast[-signal*2:] - ema_slow[-signal*2:]
    signal_vals = np.array([calculate_ema(macd_vals[:i+1], signal) for i in range(signal-1, len(macd_vals))])
    macd_signal = signal_vals[-1] if len(signal_vals) > 0 else 0
    prev_macd_signal = signal_vals[-2] if len(signal_vals) >= 2 else macd_signal
    macd_hist = macd_line - macd_signal
    return macd_line, macd_signal, macd_hist, prev_macd_line, prev_macd_signal

def calculate_bollinger_bands(prices, period=20, std_dev=2.0):
    if len(prices) < period:
        return 0, 0, 0
    sma = np.mean(prices[-period:])
    std = np.std(prices[-period:])
    return sma + std_dev * std, sma, sma - std_dev * std

def calculate_adx(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        return 0
    tr_list, plus_dm_list, minus_dm_list = [], [], []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0
        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
    if len(tr_list) < period:
        return 0
    atr = np.mean(tr_list[-period:])
    if atr < 1e-10:
        return 0
    plus_di = 100 * np.mean(plus_dm_list[-period:]) / atr
    minus_di = 100 * np.mean(minus_dm_list[-period:]) / atr
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 1e-10 else 0
    return dx

def get_dynamic_stagnation_limit(current_atr, atr_ma20):
    if current_atr < atr_ma20:
        return 180
    return 300

def check_binance_weight():
    try:
        headers = getattr(exchange, 'last_response_headers', {})
        weight = None
        for k, v in headers.items():
            if k.lower() == 'x-mbx-used-weight-1m':
                weight = int(v)
                break
        if weight is not None:
            if weight > 900:
                logger.warning(f"⚠️ [API限流警報] 幣安目前權重已達 {weight}/1200，觸發重度防護，冷卻 10 秒")
                return 10.0
            elif weight > 700:
                logger.warning(f"⚠️ [API限流警報] 幣安目前權重已達 {weight}/1200，觸發輕度防護，冷卻 3 秒")
                return 3.0
    except Exception as e:
        logger.warning(f"⚠️ [API權重讀取失敗] {e}")
    return 0.0

CONSECUTIVE_ERRORS = 0

# ── 狀態管理 ──────────────────────────────────────────────────

def get_active_count():
    return sum(1 for s in STATES.values() if s.status == "ACTIVE")

def get_open_position_count():
    return sum(1 for s in STATES.values() if abs(s.qty) > 0.000001)

def get_open_symbols():
    return [sym for sym in ALL_SYMBOLS if abs(STATES[sym].qty) > 0.000001]


def is_symbol_locked(sym):
    STATES.setdefault(sym, build_symbol_state(sym))
    s = STATES[sym]
    return abs(s.qty) > 0.000001 or s.entry_count > 0 or s.open_time > 0 or s.status in ("COOLDOWN", "BANNED")


def filter_valid_symbols(symbols):
    if not exchange.markets:
        return list(symbols)
    valid = []
    for sym in symbols:
        found = False
        for m in exchange.markets.values():
            if m['id'] == sym or m['symbol'] == sym:
                found = True
                break
        if found:
            valid.append(sym)
        else:
            logger.warning(f"⚠️ [過濾無效幣種] 交易所目前不支援/已下架此幣種，已自動移出監聽清單: {sym}")
    return valid


def apply_symbol_pool_change(requested_symbols):
    global ALL_SYMBOLS
    desired = filter_valid_symbols(filter_short_term_symbols(requested_symbols))
    locked_symbols = [sym for sym in ALL_SYMBOLS if is_symbol_locked(sym)]

    for sym in desired + list(DEFAULT_SYMBOLS):
        STATES.setdefault(sym, build_symbol_state(sym))

    if not desired:
        desired = list(DEFAULT_SYMBOLS[:3])

    new_symbols = []
    used = set()
    target_count = min(20, max(len(desired), len(ALL_SYMBOLS)))

    for sym in locked_symbols:
        if sym not in used:
            new_symbols.append(sym)
            used.add(sym)
    for sym in desired:
        if sym in used or len(new_symbols) >= target_count:
            continue
        new_symbols.append(sym)
        used.add(sym)
    for sym in ALL_SYMBOLS:
        if sym in used or len(new_symbols) >= target_count:
            continue
        new_symbols.append(sym)
        used.add(sym)
    for sym in DEFAULT_SYMBOLS:
        if sym in used or len(new_symbols) >= target_count:
            continue
        new_symbols.append(sym)
        used.add(sym)

    if len(new_symbols) < target_count:
        for sym in desired:
            if sym in used or len(new_symbols) >= target_count:
                continue
            new_symbols.append(sym)
            used.add(sym)

    ALL_SYMBOLS = new_symbols[:target_count]
    for sym in ALL_SYMBOLS:
        STATES.setdefault(sym, build_symbol_state(sym))
    save_symbol_pool(ALL_SYMBOLS)
    return list(ALL_SYMBOLS)


def update_trade_signal(sym, trade):
    s = STATES[sym]
    price = float(trade.get("price", 0) or 0)
    amount = float(trade.get("amount", 0) or 0)
    if price <= 0 or amount <= 0:
        return

    ts = trade.get("timestamp", time.time() * 1000)
    if isinstance(ts, (int, float)):
        ts_value = float(ts) / 1000.0
    else:
        ts_value = time.time()

    s.last_trade_price = price
    s.last_trade_qty = amount
    s.last_trade_side = str(trade.get("side", "buy") or "buy")
    s.last_trade_time = ts_value
    s.trade_price_history.append(price)
    s.trade_qty_history.append(amount)

    if len(s.trade_price_history) > 20:
        s.trade_price_history = s.trade_price_history[-20:]
    if len(s.trade_qty_history) > 20:
        s.trade_qty_history = s.trade_qty_history[-20:]

    if len(s.trade_price_history) < 2:
        return

    prev_price = s.trade_price_history[-2]
    prev_qty = s.trade_qty_history[-2] if len(s.trade_qty_history) >= 2 else amount
    if prev_price <= 0:
        prev_price = price

    price_change_pct = abs(price - prev_price) / max(prev_price, 1e-8)
    avg_qty = float(np.mean(s.trade_qty_history[-5:])) if len(s.trade_qty_history) >= 5 else amount
    qty_ratio = amount / max(avg_qty, 1e-8)
    score = min(3.0, qty_ratio * 0.35 + price_change_pct * 25.0)

    if qty_ratio >= 4.0 and price_change_pct >= 0.004:
        s.trade_signal_strength = score
        s.trade_signal_reason = f"即時大額成交 {amount:.3f} / {qty_ratio:.1f}x 均量"
    else:
        s.trade_signal_strength = max(0.0, s.trade_signal_strength * 0.85 - 0.05)
        if s.trade_signal_strength < 0.15:
            s.trade_signal_strength = 0.0
            s.trade_signal_reason = ""


REAL_BALANCE = 150.0

async def fetch_real_balance():
    global REAL_BALANCE
    if PAPER_TRADING:
        return
    try:
        balance_info = await exchange.fetch_balance()
        usdt_balance = float(balance_info.get('USDT', {}).get('total', 150.0))
        REAL_BALANCE = usdt_balance
    except Exception as e:
        logger.warning(f"⚠️ [餘額獲取失敗] {e}")

def get_balance():
    if not PAPER_TRADING:
        return REAL_BALANCE
    try:
        with open("paper_state.json", "r") as f:
            state = json.load(f)
            return float(state.get("balance_usdt", 150.0))
    except:
        return 150.0

def compute_per_coin_margin():
    balance = get_balance()
    open_count = get_open_position_count()
    remaining_slots = MAX_POSITIONS - open_count
    if remaining_slots <= 0:
        return 0
    usable = balance * 0.95
    per_slot = usable / MAX_POSITIONS
    return per_slot

# ── 幣種狀態更新 ──────────────────────────────────────────────

def update_states():
    now = time.time()
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s.status == "COOLDOWN" and now >= s.next_status_time:
            s.status = "ACTIVE"
            s.status_reason = ""
            logger.info(f"🔄 [狀態] {sym} 冷卻結束 → ACTIVE")
        if s.status == "BANNED" and now >= s.next_status_time:
            s.status = "ACTIVE"
            s.status_reason = ""
            if not hasattr(s, 'stop_times'):
                s.stop_times = []
            s.stop_times.clear()
            s.stop_count = 0
            s.first_stop_time = 0
            logger.info(f"🔄 [狀態] {sym} 封禁解除 → ACTIVE")

def mark_exit(sym, is_stop_loss=False):
    s = STATES[sym]
    now = time.time()
    s.status = "COOLDOWN"
    s.next_status_time = now + COOLDOWN_SEC
    s.status_reason = "冷卻中 (5分鐘)"
    logger.info(f"⏳ [狀態] {sym} 平倉 → COOLDOWN 5分鐘")
    if is_stop_loss:
        s.stop_times.append(now)
        s.stop_times = [t for t in s.stop_times if now - t <= BAN_WINDOW]
        s.stop_count = len(s.stop_times)
        max_stops = 2 if sym in HIGH_VOLATILITY_COINS else MAX_STOPS_IN_WINDOW
        if s.stop_count >= max_stops:
            s.status = "BANNED"
            s.next_status_time = now + BAN_DURATION
            s.status_reason = f"封禁中 (24h，{max_stops}次停損)"
            logger.warning(f"🚫 [狀態] {sym} 1h內{max_stops}次停損 → BANNED 24h")

def reset_coin_state(sym):
    STATES.setdefault(sym, build_symbol_state(sym))
    s = STATES[sym]
    s.qty = 0.0
    s.avg_price = 0.0
    s.open_time = 0.0
    s.trailing_highest = 0.0
    s.trailing_lowest = float('inf')
    s.highest_profit_pct = 0.0
    s.has_partial_closed = False
    s.pending_side = None
    s.pending_time = 0
    s.pending_confirm_high = 0
    s.pending_confirm_low = 0
    s.has_been_negative = False
    s.trail_tp_price = 0.0
    s.entry_count = 0
    s.avg_entry_price = 0.0
    s.last_entry_time = 0.0

# ── 大盤與風向監控 (BTC & ETH Filter) ─────────────────────────

MARKET_WIND = {
    "btc_trend": "NEUTRAL",  # "BULL" or "BEAR"
    "allow_long": True,
    "allow_short": True,
    "btc_change_15m": 0.0,
    "eth_change_15m": 0.0
}

async def update_market_wind():
    global MARKET_WIND
    try:
        # 抓取 BTC 和 ETH
        btc_ohlcv = await exchange.fetch_ohlcv("BTCUSDT", TIMEFRAME, limit=100)
        eth_ohlcv = await exchange.fetch_ohlcv("ETHUSDT", TIMEFRAME, limit=100)
        
        MARKET_WIND["allow_long"] = True
        MARKET_WIND["allow_short"] = True
        
        if len(btc_ohlcv) >= 20:
            btc_closes = np.array([x[4] for x in btc_ohlcv])
            btc_ema20 = calculate_ema(btc_closes, 20)
            btc_price = btc_closes[-1]
            btc_change_15m = (btc_price - btc_closes[-15]) / btc_closes[-15]
            
            MARKET_WIND["btc_trend"] = "BULL" if btc_price > btc_ema20 else "BEAR"
            MARKET_WIND["btc_change_15m"] = btc_change_15m
        else:
            btc_change_15m = 0.0
            
        if len(eth_ohlcv) >= 20:
            eth_closes = np.array([x[4] for x in eth_ohlcv])
            eth_price = eth_closes[-1]
            eth_change_15m = (eth_price - eth_closes[-15]) / eth_closes[-15]
            MARKET_WIND["eth_change_15m"] = eth_change_15m
        else:
            eth_change_15m = 0.0
            
        # 1. 瀑布防護 (15m內跌超過1.2%暫停多單，漲超過1.2%暫停空單)
        if btc_change_15m < -0.012 or eth_change_15m < -0.015:
            MARKET_WIND["allow_long"] = False
            logger.warning(f"⚠️ [大盤瀑布風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣多單開倉！")
        elif btc_change_15m > 0.012 or eth_change_15m > 0.015:
            MARKET_WIND["allow_short"] = False
            logger.warning(f"⚠️ [大盤暴漲風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣空單開倉！")
            
    except Exception as e:
        logger.warning(f"⚠️ [更新大盤風向失敗]: {e}")

# ── 資料獲取 ──────────────────────────────────────────────────

async def initialize_atr_history():
    print("⏳ [初始化] 開始獲取 1000 根 1m K線以預熱 ATR 歷史...")
    results_map = {}
    chunk_size = 5
    for i in range(0, len(ALL_SYMBOLS), chunk_size):
        chunk = ALL_SYMBOLS[i:i+chunk_size]
        tasks = [exchange.fetch_ohlcv(sym, '1m', limit=1000) for sym in chunk]
        res = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, r in zip(chunk, res):
            results_map[sym] = r
        await asyncio.sleep(0.5)

    for sym in ALL_SYMBOLS:
        res = results_map.get(sym)
        if not isinstance(res, Exception) and res:
            ohlcv = res
            tr_list = []
            for j in range(1, len(ohlcv)):
                h = ohlcv[j][2]
                l = ohlcv[j][3]
                pc = ohlcv[j-1][4]
                tr = max(h - l, abs(h - pc), abs(l - pc))
                tr_list.append(tr)
                if len(tr_list) >= 14:
                    atr = float(np.mean(tr_list[-14:]))
                    STATES[sym].atr_history.append(atr)
            logger.info(f"✅ [初始化] {sym} 歷史 ATR 預熱完成，載入 {len(STATES[sym].atr_history)} 筆數據")
        else:
            logger.warning(f"⚠️ [初始化] {sym} 歷史 ATR 預熱失敗: {res}")

async def fetch_all_klines():
    tasks = {}
    for sym in ALL_SYMBOLS:
        tasks[sym] = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=100)
    results = await asyncio.gather(*[tasks[sym] for sym in ALL_SYMBOLS], return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym].ohlcv = results[i]
            STATES[sym].close_price = results[i][-1][4]
        else:
            logger.warning(f"⚠️ [K線獲取失敗] {sym}: {results[i]}")

async def fetch_sma200_15m(sym):
    try:
        ohlcv = await exchange.fetch_ohlcv(sym, '15m', limit=200)
        closes = np.array([x[4] for x in ohlcv])
        return float(np.mean(closes))
    except Exception as e:
        logger.warning(f"⚠️ [SMA200獲取失敗] {sym}: {e}")
        return 0.0

async def fetch_all_sma200():
    tasks = {sym: fetch_sma200_15m(sym) for sym in ALL_SYMBOLS}
    results = await asyncio.gather(*[tasks[sym] for sym in tasks], return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym].sma200_15m = results[i]

async def fetch_htf_trend(sym):
    try:
        ohlcv = await exchange.fetch_ohlcv(sym, '1h', limit=50)
        closes = np.array([float(x[4]) for x in ohlcv])
        ema20 = calculate_ema(closes, 20)
        current_close = closes[-1]
        trend = "long" if current_close > ema20 else "short"
        return trend, ema20
    except Exception as e:
        logger.warning(f"⚠️ [HTF獲取失敗] {sym}: {e}")
        return None, 0.0

async def fetch_all_htf_trend():
    tasks = {sym: fetch_htf_trend(sym) for sym in ALL_SYMBOLS}
    results = await asyncio.gather(*[tasks[sym] for sym in tasks], return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            trend, ema20 = results[i]
            if trend:
                STATES[sym].htf_trend = trend
                STATES[sym].htf_ema20 = ema20

async def load_open_positions():
    if not PAPER_TRADING:
        return
    try:
        with open("paper_state.json", "r") as f:
            state = json.load(f)
        for sym in ALL_SYMBOLS:
            pk = paper_key(sym)
            pos = state.get("positions", {}).get(pk, {})
            qty = float(pos.get("qty", 0.0))
            if abs(qty) > 0.000001:
                STATES[sym].qty = qty
                STATES[sym].avg_price = float(pos.get("avg_price", 0.0))
                # Restore open_time, if not present fallback to current time to avoid 9999s infinite hold bug
                stored_time = float(pos.get("open_time", 0.0))
                if stored_time > 1000000000:  # Valid timestamp
                    if stored_time > 1000000000000: # ms to s
                        stored_time /= 1000
                    STATES[sym].open_time = stored_time
                else:
                    STATES[sym].open_time = time.time()
    except Exception as e:
        logger.warning(f"⚠️ [讀取持倉失敗] {e}")

# ── 指標計算 ──────────────────────────────────────────────────

def compute_indicators(sym):
    s = STATES[sym]
    ohlcv = s.ohlcv
    if len(ohlcv) < 20:
        return
    closes = np.array([x[4] for x in ohlcv])
    highs = np.array([x[2] for x in ohlcv])
    lows = np.array([x[3] for x in ohlcv])
    volumes = np.array([x[5] for x in ohlcv])
    s.closes = closes
    prev = s.prev_close
    for i in range(len(ohlcv)):
        h, l, c = ohlcv[i][2], ohlcv[i][3], ohlcv[i][4]
        if i == 0 and prev is not None:
            tr = max(h - l, abs(h - prev), abs(l - prev))
        elif i > 0:
            tr = max(h - l, abs(h - ohlcv[i-1][4]), abs(l - ohlcv[i-1][4]))
        else:
            tr = h - l
        s.tr_list.append(tr)
    s.prev_close = ohlcv[-1][4]
    if len(s.tr_list) > 42:
        s.tr_list = s.tr_list[-42:]
    if len(s.tr_list) >= 14:
        s.current_atr = float(np.mean(s.tr_list[-14:]))
        s.atr_history.append(s.current_atr)
        if len(s.atr_history) > 1440:
            s.atr_history = s.atr_history[-1440:]
        s.atr_ma20 = float(np.mean(s.atr_history[-20:])) if len(s.atr_history) >= 20 else s.current_atr
    if len(closes) > RSI_PERIOD:
        s.rsis = calculate_rsi_array(closes, RSI_PERIOD)
        s.current_rsi = s.rsis[-1]
    s.vol_ma20 = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))
    s.current_vol = float(volumes[-1])
    if len(closes) >= 20:
        s.ema20 = calculate_ema(closes, 20)
    if len(closes) >= 50:
        s.ema50 = calculate_ema(closes, 50)
    if len(closes) >= 26:
        m_line, m_sig, m_hist, p_line, p_sig = calculate_macd(closes)
        s.macd_line = m_line
        s.macd_signal = m_sig
        s.macd_hist = m_hist
        s.prev_macd_line = p_line
        s.prev_macd_signal = p_sig
    if len(closes) >= 20:
        up, mid, low = calculate_bollinger_bands(closes)
        s.bb_up = up
        s.bb_mid = mid
        s.bb_low = low

# ── 出場邏輯 ──────────────────────────────────────────────────

def update_trailing_take_profit(sym, current_price, is_long):
    s = STATES[sym]
    avg_price = s.avg_price
    if avg_price <= 0:
        return False, 0.0

    if s.trail_tp_price <= 0:
        if is_long:
            s.trail_tp_price = avg_price * 1.003
        else:
            s.trail_tp_price = avg_price * 0.997

    if is_long:
        if current_price <= s.trail_tp_price:
            return True, s.trail_tp_price
        new_tp = current_price * 0.997
        s.trail_tp_price = min(s.trail_tp_price, new_tp)
        return False, s.trail_tp_price

    if current_price >= s.trail_tp_price:
        return True, s.trail_tp_price
    new_tp = current_price * 1.003
    s.trail_tp_price = max(s.trail_tp_price, new_tp)
    return False, s.trail_tp_price


def should_recover_from_reversal(sym, is_long):
    s = STATES[sym]
    if abs(s.qty) < 0.000001:
        return False

    macd_reversal = (is_long and s.prev_macd_line > s.prev_macd_signal and s.macd_line < s.macd_signal) or \
                    (not is_long and s.prev_macd_line < s.prev_macd_signal and s.macd_line > s.macd_signal)

    breakout_confirmed = False
    if s.prev_close and len(s.ohlcv) >= 1:
        prev_bar_idx = -2 if len(s.ohlcv) >= 2 else -1
        prev_bar_high = s.ohlcv[prev_bar_idx][2]
        prev_bar_low = s.ohlcv[prev_bar_idx][3]
        break_high = s.close_price > s.prev_close and s.close_price >= prev_bar_high
        break_low = s.close_price < s.prev_close and s.close_price <= prev_bar_low
        breakout_confirmed = (is_long and break_low) or (not is_long and break_high)

    volume_confirmed = s.current_vol > s.vol_ma20 * 2.0
    trade_signal = s.trade_signal_strength
    trade_confirmed = trade_signal >= 1.5

    if macd_reversal and breakout_confirmed and (volume_confirmed or trade_confirmed):
        return True

    return False


def detect_market_regime(sym, current_price, avg_price, is_long):
    s = STATES[sym]
    if len(s.ohlcv) < 20 or avg_price <= 0:
        return "HOLD", "資料不足"

    if s.trade_signal_strength >= 1.5 and s.prev_close:
        price_change_pct = (current_price - s.prev_close) / s.prev_close
        if is_long and price_change_pct > 0.01:
            return "BREAKOUT_REVERSAL", f"即時大額成交異常(逆向) {s.trade_signal_reason}"
        if not is_long and price_change_pct < -0.01:
            return "BREAKOUT_REVERSAL", f"即時大額成交異常(逆向) {s.trade_signal_reason}"

    recent_candles = s.ohlcv[-20:]
    highs = np.array([x[2] for x in recent_candles])
    lows = np.array([x[3] for x in recent_candles])
    closes = np.array([x[4] for x in recent_candles])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / recent_low if recent_low > 0 else 0

    atr_val = s.current_atr if s.current_atr > 0 else (current_price * 0.01)
    atr_pct = atr_val / current_price if current_price > 0 else 0

    # 1) 即時成交流監聽：大額成交且價格急速變動，優先判定為突破反轉
    trade_signal = s.trade_signal_strength
    is_adverse = False
    if s.prev_close:
        if is_long and current_price < s.prev_close:
            is_adverse = True
        elif not is_long and current_price > s.prev_close:
            is_adverse = True
            
    if trade_signal >= 1.1 and is_adverse:
        return "BREAKOUT_REVERSAL", f"即時大額成交異常(逆向) {s.trade_signal_reason}"

    # 2) 簡化的大單/突發行情判斷：放量且價格急速變動
    volume_surge = s.current_vol > s.vol_ma20 * 2.5
    adverse_price_jump = False
    if s.prev_close:
        price_change_pct = (current_price - s.prev_close) / s.prev_close
        if is_long and price_change_pct < -0.01:
            adverse_price_jump = True
        elif not is_long and price_change_pct > 0.01:
            adverse_price_jump = True
            
    if volume_surge and adverse_price_jump:
        return "BREAKOUT_REVERSAL", "放量突發且價格急速逆向變動"

    # 2) 盤整市場：價格被壓縮在狹窄區間內，且 ATR 也偏小
    is_ranging = range_width_pct < 0.025 and atr_pct < 0.015
    if is_ranging:
        profit_pct = (current_price - avg_price) / max(avg_price, 1e-8) if is_long else (avg_price - current_price) / max(avg_price, 1e-8)
        if profit_pct >= 0.003:
            return "RANGE_PROFIT_TAKE", f"盤整區間內已獲利 {profit_pct * 100:.2f}%"

    return "HOLD", "未達出場條件"

async def close_position(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = STATES[sym]
    s.adjusted_this_tick = True
    pk = paper_key(sym)
    qty = min(abs(qty), abs(s.qty))
    if qty < 0.000001:
        return
    close_qty = qty if close_side == 'sell' else -qty
    pnl = 0.0
    if PAPER_TRADING:
        if s.qty > 0:
            pnl = (price - avg_price) * qty
        else:
            pnl = (avg_price - price) * qty
        update_paper_state(pk, close_side, price, qty, is_close=True, pnl=pnl)
    else:
        try:
            await exchange.create_order(sym, type='market', side=close_side, amount=qty,
                                        params={'reduceOnly': True, 'marginMode': 'isolated'})
        except Exception as e:
            logger.error(f"🚨 [平倉錯誤] {sym}: {e}")
            return
    remaining = abs(s.qty) - qty
    if remaining < 0.01:
        if remaining > 0.000001:
            logger.info(f"🧹 [塵埃清理] {sym} 剩餘 {remaining:.6f} 視為已清")
        logger.info(f"🔴 [平倉] {sym} 全平 {reason} (PnL: {pnl:.2f})")
        mark_exit(sym, is_stop_loss=is_stop_loss)
        
        # 保存同向冷卻狀態
        old_side = 'buy' if s.qty > 0 else 'sell'
        ls_side = old_side if is_stop_loss else s.last_stop_loss_side
        ls_time = time.time() if is_stop_loss else s.last_stop_loss_time
        
        reset_coin_state(sym)
        STATES[sym].last_stop_loss_side = ls_side
        STATES[sym].last_stop_loss_time = ls_time
        
        # 更新全局風控與績效狀態
        current_day = time.strftime("%Y-%m-%d", time.gmtime())
        if GLOBAL_STATE["last_reset_day"] != current_day:
            GLOBAL_STATE["daily_pnl"] = 0.0
            GLOBAL_STATE["last_reset_day"] = current_day
            GLOBAL_STATE["trading_enabled"] = True

        GLOBAL_STATE["daily_pnl"] += pnl
        route = s.entry_route
        if route in GLOBAL_STATE["route_stats"]:
            if pnl > 0:
                GLOBAL_STATE["route_stats"][route]["win"] += 1
                GLOBAL_STATE["consecutive_losses"] = 0
            elif pnl < 0:
                GLOBAL_STATE["route_stats"][route]["loss"] += 1
                GLOBAL_STATE["consecutive_losses"] += 1

        MAX_DAILY_LOSS_USDT = 30.0
        if GLOBAL_STATE["daily_pnl"] <= -MAX_DAILY_LOSS_USDT:
            if GLOBAL_STATE["trading_enabled"]:
                logger.error(f"🚨 [全局風控] 當日累積虧損已達 {GLOBAL_STATE['daily_pnl']:.2f} USDT，超過每日最大虧損限制！關閉今日所有新開倉。")
                GLOBAL_STATE["trading_enabled"] = False
    else:
        s.qty = (abs(s.qty) - qty) * (1 if s.qty > 0 else -1)
        logger.info(f"✅ [部分平] {sym} 平{qty} 剩{abs(s.qty):.4f} {reason} (PnL: {pnl:.2f})")

async def check_position_exits(sym):
    s = STATES[sym]
    if s.adjusted_this_tick:
        return
    if abs(s.qty) < 0.000001 or s.avg_price <= 0:
        return
        
    p = s.close_price
    avg = s.avg_price
    is_long = s.qty > 0
    raw_profit_pct = (p - avg) / max(avg, 1e-8) if is_long else (avg - p) / max(avg, 1e-8)
    profit_pct = raw_profit_pct
    hold_sec = time.time() - s.open_time if s.open_time > 0 else 9999
    
    if profit_pct > s.highest_profit_pct:
        s.highest_profit_pct = profit_pct
    if profit_pct < 0:
        s.has_been_negative = True

    if p > s.trailing_highest:
        s.trailing_highest = p
    if p < s.trailing_lowest:
        s.trailing_lowest = p

    # 0. 強制保本機制 (Breakeven Protection)
    if profit_pct > 0.005:
        s.is_breakeven_set = True
    elif profit_pct > 0.002 and hold_sec > 180:
        s.is_breakeven_set = True

    if s.is_breakeven_set and profit_pct <= 0.001:
        cs = 'sell' if is_long else 'buy'
        print(f"🔒 [強制保本] {sym} 觸發保本機制，防止利潤回吐！")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="保本防護出場")
        return

    # 1. 絕對硬停損 (嚴格控管最大虧損，無視持倉時間！)
    usdt_pnl = profit_pct * avg * abs(s.qty)
    if profit_pct <= -0.02:
        cs = 'sell' if is_long else 'buy'
        reason_msg = "2%跌幅停損"
        print(f"🚨 [{reason_msg}] {sym} 觸發底線平倉！(目前虧損: {usdt_pnl:.2f} U)")
        await close_position(sym, cs, abs(s.qty), p, avg, reason=reason_msg, is_stop_loss=True)
        return

    # 1.5 加倉後的保本停損上移 (Pyramiding Break-Even Stop Loss)
    if s.entry_count >= 2:
        # 已加倉，將停損上移至新均價附近 (容忍 -0.1% 避免被點差洗掉)
        if profit_pct <= -0.001:
            cs = 'sell' if is_long else 'buy'
            print(f"🛡️ [加倉保本] {sym} 已加倉 {s.entry_count-1} 次，觸發新均價保本防護出場！")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="加倉保本出局")
            return

    # 2. 市場狀態偵測與補救 (BREAKOUT_REVERSAL / 反向補救)
    regime_decision, regime_reason = detect_market_regime(sym, p, avg, is_long)
    if regime_decision == "BREAKOUT_REVERSAL":
        cs = 'sell' if is_long else 'buy'
        logger.error(f"🚨 [市場 regime] {sym} {regime_reason}，立即平倉並反手")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="市場反轉/大單突破", is_stop_loss=True)
        s.highest_profit_pct = 0.0
        return

    if should_recover_from_reversal(sym, is_long):
        recovery_side = 'sell' if is_long else 'buy'
        logger.info(f"🔄 [反向補救] {sym} 方向錯誤且出現反轉訊號，直接反手")
        await close_position(sym, recovery_side, abs(s.qty), p, avg, reason="反轉補救", is_stop_loss=True)
        s.highest_profit_pct = 0.0
        return

    # 3. 大波段移動停利 (ATR Trailing Stop)
    if s.highest_profit_pct >= 0.018:
        trail_stop_dist = s.current_atr * 1.5
        if is_long and p <= s.trailing_highest - trail_stop_dist:
            cs = 'sell'
            print(f"🏃 [ATR移動停利] {sym} 最高獲利達 {s.highest_profit_pct*100:.2f}%，回落 1.5 ATR，觸發出場！")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="ATR移動停利")
            return
        elif not is_long and p >= s.trailing_lowest + trail_stop_dist:
            cs = 'buy'
            print(f"🏃 [ATR移動停利] {sym} 最高獲利達 {s.highest_profit_pct*100:.2f}%，回落 1.5 ATR，觸發出場！")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="ATR移動停利")
            return
    elif s.highest_profit_pct >= 0.008:
        # 保留一個較早的防護，高波動小幣稍微放寬防護網避免插針
        if profit_pct <= s.highest_profit_pct - 0.005:
            cs = 'sell' if is_long else 'buy'
            print(f"🏃 [初階移動停利] {sym} 最高獲利達 {s.highest_profit_pct*100:.2f}%，回落 0.5%，觸發出場！")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="初階移動停利")
            return

    # 4. 保本鎖利與利潤防護機制 (Break-even & Capital Protection Lock)
    if s.highest_profit_pct >= 0.015 and profit_pct < s.highest_profit_pct * 0.5:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [回撤鎖利] {sym} 獲利最高曾達 {s.highest_profit_pct*100:.3f}%，回撤已達50% (目前 {profit_pct*100:.3f}%)，觸發回撤平倉")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="回撤鎖利防護")
        s.highest_profit_pct = 0.0
        return
    elif s.highest_profit_pct >= 0.010 and profit_pct < 0.005:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [高利鎖利] {sym} 獲利最高曾達 {s.highest_profit_pct*100:.3f}%，目前回落至 {profit_pct*100:.3f}%，觸發高利保護平倉")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="高利鎖利防護")
        s.highest_profit_pct = 0.0
        return
    elif s.highest_profit_pct >= 0.008 and profit_pct < 0.003:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [中利鎖利] {sym} 獲利最高曾達 {s.highest_profit_pct*100:.3f}%，目前回落至 {profit_pct*100:.3f}%，觸發中利保護平倉")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="中利鎖利防護")
        s.highest_profit_pct = 0.0
        return
    elif s.highest_profit_pct >= 0.008 and profit_pct < 0.0015:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [微利鎖利] {sym} 獲利最高曾達 {s.highest_profit_pct*100:.3f}%，目前回落至 {profit_pct*100:.3f}%，觸發微利保護平倉")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="微利鎖利防護")
        s.highest_profit_pct = 0.0
        return

    # 5. MACD 反向交叉 (趨勢反轉停損)
    m_death = s.prev_macd_line > s.prev_macd_signal and s.macd_line < s.macd_signal
    m_golden = s.prev_macd_line < s.prev_macd_signal and s.macd_line > s.macd_signal
    if (is_long and m_death) or (not is_long and m_golden):
        if profit_pct < -0.005:  # 價格虧損 > 0.5% (ROE -2.5%) 才執行MACD反向停損
            cs = 'sell' if is_long else 'buy'
            is_sl = profit_pct < 0.0
            print(f"📉 [反轉出場] {sym} MACD反向交叉且達虧損門檻，立即平倉 (損益: {profit_pct*100:.2f}%)")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="趨勢反轉", is_stop_loss=is_sl)
            return

    # 動能衰減檢查：利潤溜滑梯
    s.pnl_history.append(profit_pct * 100)
    if len(s.pnl_history) > 5:
        s.pnl_history.pop(0)
    if len(s.pnl_history) == 5:
        is_decaying = all(s.pnl_history[i] > s.pnl_history[i+1] for i in range(4))
        if is_decaying and profit_pct * 100 > 0.5:
            cs = 'sell' if is_long else 'buy'
            print(f"📉 [動能衰減] {sym} 利潤連5次下滑 ({profit_pct*100:.2f}%)，即時出場")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="動能衰減")
            s.highest_profit_pct = 0.0
            return

    # 6. 動能停滯微利出場 (Stagnation Take-Profit) & 15分鐘時間停損
    if hold_sec > 600 and 0.001 < profit_pct < 0.006:
        cs = 'sell' if is_long else 'buy'
        print(f"⏱️ [動能停滯微利] {sym} 持倉達 {hold_sec/60:.1f} 分鐘，利潤停滯於 {profit_pct*100:.2f}%，提早落袋為安！")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="動能停滯微利")
        return

    if hold_sec > 900 and profit_pct < -0.01:
        cs = 'sell' if is_long else 'buy'
        print(f"⏱️ [時間停損] {sym} {hold_sec/60:.1f}分仍虧損")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="時間停損", is_stop_loss=True)
        return

    # 7. ATR TP/SL
    tp_dist = s.current_atr * 3.0
    if (is_long and p >= avg + tp_dist) or (not is_long and p <= avg - tp_dist):
        cs = 'sell' if is_long else 'buy'
        logger.info(f"🎯 [ATR停利K線] {sym}")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="ATR停利K線")
        return

# ── 進場邏輯 ──────────────────────────────────────────────────




async def execute_order(sym, side, price, route="a"):
    s = STATES[sym]
    pk = paper_key(sym)
    margin = compute_per_coin_margin()
    if margin <= 0:
        logger.warning(f"⚠️ [風控] {sym} 無可用保證金")
        return

    now = time.time()
    if s.entry_count > 0:
        if now - s.last_entry_time < s.entry_cooldown_sec:
            logger.info(f"⏳ [加倉冷卻] {sym} 距離上次加倉不足 {s.entry_cooldown_sec} 秒")
            return
        if s.entry_count >= s.max_additional_entries:
            logger.warning(f"⚠️ [加倉上限] {sym} 已達最大加倉次數")
            return
        if s.avg_price > 0 and price > 0:
            profit_pct = (price - s.avg_price) / max(s.avg_price, 1e-8) if side == 'buy' else (s.avg_price - price) / max(s.avg_price, 1e-8)
            required_profit = 0.006 if s.entry_count == 1 else 0.012
            if profit_pct < required_profit:
                logger.info(f"🛑 [順勢加碼風控] {sym} {'多' if side=='buy' else '空'}單獲利未達 {required_profit*100:.1f}% (目前: {profit_pct*100:.2f}%)，禁止過早加倉")
                return

    if GLOBAL_STATE["consecutive_losses"] >= 5:
        logger.warning(f"⚠️ [連敗風控] {sym} 全局已連續虧損 {GLOBAL_STATE['consecutive_losses']} 次，暫停開倉避免上頭")
        return

    # 深度與流動性防護 (Bid-Ask Spread & Volume)
    try:
        order_book = await exchange.fetch_bids_asks([sym])
        if sym in order_book:
            best_bid = order_book[sym]['info'].get('bidPrice')
            best_ask = order_book[sym]['info'].get('askPrice')
            bid_qty = order_book[sym]['info'].get('bidQty')
            ask_qty = order_book[sym]['info'].get('askQty')
            
            if best_bid and best_ask:
                best_bid = float(best_bid)
                best_ask = float(best_ask)
                spread = (best_ask - best_bid) / best_ask
                if spread > 0.001:
                    logger.warning(f"🛑 [流動性風控] {sym} 買賣價差 {spread*100:.2f}% > 0.1%，深度不足，拒絕開倉！")
                    return
            
            # 滑價保護: 訂單量不得超過最佳盤口掛單量的 1%
            if bid_qty and ask_qty:
                bid_qty = float(bid_qty)
                ask_qty = float(ask_qty)
                base_amt_est = (margin * LEVERAGE) / price
                target_qty = ask_qty if side == 'buy' else bid_qty
                if target_qty > 0 and base_amt_est > target_qty * 0.01:
                    logger.warning(f"🛑 [深度風控] {sym} 預估下單量 {base_amt_est:.4f} 超過盤口深度 1% (掛單量 {target_qty:.4f})，拒絕開倉避免滑價！")
                    return
    except Exception as e:
        logger.debug(f"⚠️ [流動性檢查失敗] {sym}: {e}")

    base_amt = (margin * LEVERAGE) / price
    if base_amt < 0.001:
        logger.warning(f"⚠️ [風控] {sym} 數量過小 {base_amt:.6f}")
        return

    if GLOBAL_STATE["consecutive_losses"] >= 3:
        logger.info(f"🛡️ [連敗降倉] 全局連輸 {GLOBAL_STATE['consecutive_losses']} 次，基礎倉位減半")
        base_amt *= 0.5

    if s.entry_count == 0:
        base_amt *= 1.0   # 首次進場 100%
    elif s.entry_count == 1:
        base_amt *= 0.5   # 第一次加倉 50%
    elif s.entry_count == 2:
        base_amt *= 0.25  # 第二次加倉 25%
    else:
        return

    if PAPER_TRADING:
        try:
            update_paper_state(pk, side, price, base_amt)
            if side == 'buy':
                s.qty += base_amt
            else:
                s.qty -= base_amt
            if s.avg_price <= 0:
                s.avg_price = price
            else:
                s.avg_price = ((s.avg_price * abs(s.qty - base_amt)) + (price * base_amt)) / abs(s.qty)
            s.open_time = now
            s.last_buy_time = now
            s.last_entry_time = now
            s.entry_count += 1
            s.entry_route = route  # 記錄是透過哪個策略進場的
            direction = "做多" if side == 'buy' else "做空"
            print(f"🟢 [{direction}] {sym} {base_amt:.4f} @ {price} (保證金:{margin:.2f} USDT)")
        except Exception as e:
            logger.info(f"🛑 [模擬開倉失敗] {sym}: {e}")
    else:
        try:
            order = await exchange.create_order(sym, type='market', side=side, amount=base_amt,
                                                params={'marginMode': 'isolated'})
            fill_price = float(order.get('price', 0) or price)
            if fill_price <= 0:
                fill_price = price
            
            old_qty = s.qty
            if side == 'buy':
                s.qty += base_amt
            else:
                s.qty -= base_amt
                
            if s.avg_price <= 0:
                s.avg_price = fill_price
            else:
                s.avg_price = ((s.avg_price * abs(old_qty)) + (fill_price * base_amt)) / abs(s.qty)
                
            s.open_time = now
            s.last_buy_time = now
            s.last_entry_time = now
            s.entry_count += 1
        except Exception as e:
            logger.error(f"🚨 [開倉錯誤] {sym}: {e}")

def is_entry_pin_safe(sym, side):
    s = STATES[sym]
    if len(s.ohlcv) < 2:
        return True

    candle = s.ohlcv[-1]
    open_price = float(candle[1])
    high = float(candle[2])
    low = float(candle[3])
    close_price = float(candle[4])
    body = abs(close_price - open_price)
    upper_wick = high - max(open_price, close_price)
    lower_wick = min(open_price, close_price) - low

    if body <= 0:
        return False

    if side == 'buy':
        # 若當前 K 線是明顯的反轉 pin bar，直接判定為不安全的多頭進場
        prev_close = float(s.ohlcv[-2][4]) if len(s.ohlcv) >= 2 else close_price
        if close_price < prev_close and upper_wick > body * 2.0:
            return False
        if close_price < prev_close and low < prev_close and upper_wick >= body:
            return False
        return True

    # side == 'sell'
    prev_close = float(s.ohlcv[-2][4]) if len(s.ohlcv) >= 2 else close_price
    if close_price > prev_close and lower_wick > body * 2.0:
        return False
    return True


def is_entry_volume_confirmed(sym, side):
    s = STATES[sym]
    if len(s.ohlcv) < 2:
        return False
    current_vol = s.current_vol
    prev_vol = s.ohlcv[-2][5]
    vol_ma20 = s.vol_ma20
    if vol_ma20 <= 0:
        return False
        
    # 基礎量能防護 (過濾極度死水)：量能不能過度縮水
    if current_vol < vol_ma20 * 0.7 and prev_vol < vol_ma20 * 0.7:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能不足] 連續兩根量能極低 < 0.7x")
        return False
    return True


def risk_guard(sym, side, route="a"):
    if not GLOBAL_STATE["trading_enabled"]:
        return False

    is_trend = route == "a"
    if side == 'buy' and not MARKET_WIND.get("allow_long", True) and is_trend:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤瀑布風控] 大盤異常跌勢，禁止順勢開多")
        return False
    if side == 'sell' and not MARKET_WIND.get("allow_short", True) and is_trend:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤上漲風控] 大盤異常漲勢，禁止順勢開空")
        return False

    s = STATES[sym]
    cp = s.close_price

    # 避免同方向連續追單 (15分鐘冷卻)
    if time.time() - s.last_stop_loss_time < 900:
        pos_side = 'buy' if side == 'buy' else 'sell'
        if s.last_stop_loss_side == pos_side:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [同向追單保護] 15分鐘內剛被停損 {pos_side}，冷卻中")
            return False

    # 波動率過濾 (Low Volatility)
    atr_pct = s.current_atr / cp if cp > 0 else 0
    if atr_pct < 0.003:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [低波動率過濾] ATR {atr_pct*100:.2f}% < 0.3%，盤整死水不交易")
        return False

    # 極端行情保護 (Extreme Volatility in 5m & Dynamic ATR Spike)
    if len(s.ohlcv) >= 5:
        recent_5 = s.ohlcv[-5:]
        highest_5m = max([x[2] for x in recent_5])
        lowest_5m = min([x[3] for x in recent_5])
        change_5m = (highest_5m - lowest_5m) / lowest_5m if lowest_5m > 0 else 0
        if change_5m > 0.03:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [極端行情保護] 5分鐘內震幅達 {change_5m*100:.2f}% > 3%，放棄進場")
            return False
            
    # 動態 ATR 異常飆升過濾 (當前 ATR 超過 20均線的 2.5倍)
    if s.atr_ma20 > 0 and s.current_atr > s.atr_ma20 * 2.5:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [ATR波動過濾] 當前 ATR {s.current_atr:.5f} 異常飆升 (超過均值 {s.atr_ma20:.5f} 2.5 倍)，暫停交易避險")
        return False
    
    # 均線過濾器：僅限制 Route A (順勢)
    if is_trend and s.sma200_15m > 0:
        ma200 = s.sma200_15m
        if side == 'buy' and cp <= ma200:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [MA200大勢保護] 順勢做多，但價格 {cp:.4f} <= MA200 {ma200:.4f}")
            return False
        if side == 'sell' and cp >= ma200:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [MA200大勢保護] 順勢做空，但價格 {cp:.4f} >= MA200 {ma200:.4f}")
            return False

    # 大週期 (HTF) 1H 趨勢過濾器：僅限制順勢策略
    htf_trend = s.htf_trend
    if is_trend and htf_trend:
        if side == 'buy' and htf_trend == 'short':
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [1H 大週期過濾] 1H 趨勢為空頭，禁止順勢做多")
            return False
        if side == 'sell' and htf_trend == 'long':
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [1H 大週期過濾] 1H 趨勢為多頭，禁止順勢做空")
            return False
            
    # 極端波動率過濾：當市場處於瘋狂洗盤、暴漲暴跌時，禁止任何逆勢搶短
    if not is_trend:
        atr_history = s.atr_history
        atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
        current_atr = s.current_atr
        
        if atr_24h_avg > 0 and current_atr > atr_24h_avg * 2.0:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [極端波動率保護] 目前 ATR {current_atr:.5f} > 均值兩倍 {atr_24h_avg*2.0:.5f} (禁止逆勢接刀/摸頭)")
            return False
            
    if len(s.ohlcv) < 20:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線不足] 當前長度 {len(s.ohlcv)} < 20")
        return False
    if not is_entry_pin_safe(sym, side):
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 反向長影線/方向未確認")
        return False
        
    # 量能確認過濾器 (Route S 背離經常伴隨縮量，因此不強制要求量能)
    if route != "s" and not is_entry_volume_confirmed(sym, side):
        return False
        
    # ADX 趨勢強度限制：僅限制順勢策略
    if is_trend:
        highs = np.array([x[2] for x in s.ohlcv])
        lows = np.array([x[3] for x in s.ohlcv])
        closes = np.array([x[4] for x in s.ohlcv])
        adx_val = calculate_adx(highs, lows, closes)
        if adx_val < 10:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [ADX過濾] 趨勢強度 ADX {adx_val:.1f} < 10")
            return False

    # 實盤最小量限制
    min_volume = max(1000.0, s.vol_ma20 * 0.1)
    if s.current_vol < min_volume:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [實盤最小量過濾]")
        return False
        
    return True

def check_rsi_divergence(closes, rsis, window=60):
    if len(closes) < window or len(rsis) < window:
        return False, False
        
    recent_closes = closes[-window:]
    recent_rsis = rsis[-window:]
    
    # 看漲背離 (Bullish Divergence): Price Lower Low, RSI Higher Low
    bullish_div = False
    current_close = recent_closes[-1]
    current_rsi = recent_rsis[-1]
    
    # 尋找前一個價格低點 (大約 5-50 根 k線前)
    lowest_idx = np.argmin(recent_closes[:-5])
    prev_low_close = recent_closes[lowest_idx]
    prev_low_rsi = recent_rsis[lowest_idx]
    
    if current_close < prev_low_close * 0.998 and current_rsi > prev_low_rsi + 5.0 and current_rsi < 45.0:
        bullish_div = True
        
    # 看跌背離 (Bearish Divergence): Price Higher High, RSI Lower High
    bearish_div = False
    highest_idx = np.argmax(recent_closes[:-5])
    prev_high_close = recent_closes[highest_idx]
    prev_high_rsi = recent_rsis[highest_idx]
    
    if current_close > prev_high_close * 1.002 and current_rsi < prev_high_rsi - 5.0 and current_rsi > 55.0:
        bearish_div = True
        
    return bullish_div, bearish_div

def compute_signal_strength(sym):
    s = STATES[sym]
    ohlcv = getattr(s, "ohlcv", None) or []
    if len(s.closes) > 0:
        closes = np.array(s.closes, dtype=float)
    elif ohlcv:
        closes = np.array([x[4] for x in ohlcv], dtype=float)
    else:
        closes = np.array([], dtype=float)
    if len(closes) < 20 and not ohlcv and s.close_price == 0.0 and (s.prev_close is None or s.prev_close == 0.0):
        return (None, 0, None)
    if len(closes) >= 20:
        s.closes = closes

    rsi = s.current_rsi
    rsis = s.rsis
    if len(rsis) == 0:
        rsis = [rsi]
        
    close = s.close_price
    prev_close = s.prev_close if s.prev_close is not None else close
    ema20 = s.ema20
    ema50 = s.ema50

    trend_long = ema20 > 0 and close > ema20
    trend_short = ema20 > 0 and close < ema20

    # 定義更嚴格的動態 RSI 門檻 (使用者建議加嚴)
    LONG_RSI_NORMAL = 35.0 # 原40，改為35
    SHORT_RSI_NORMAL = 65.0
    LONG_RSI_HIGH_VOL = 30.0 # 極端超賣才進場
    SHORT_RSI_HIGH_VOL = 70.0

    atr_history = s.atr_history
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.current_atr

    if current_atr > atr_24h_avg and atr_24h_avg > 0:
        long_rsi_threshold = LONG_RSI_HIGH_VOL
        short_rsi_threshold = SHORT_RSI_HIGH_VOL
        vol_mode = "高波動模式 (High Vol)"
    else:
        # 在低波動模式下，縮緊門檻，只做最穩定的訊號
        long_rsi_threshold = LONG_RSI_NORMAL
        short_rsi_threshold = SHORT_RSI_NORMAL
        vol_mode = "低波動模式 (Low Vol)"

    # 每個循環輸出當前指標數值，方便追蹤與除錯
    logger.debug(f"@@COIN_DEBUG@@ 🔍 {sym} | RSI: {rsi:.1f} | Price: {close:.4f} (BB: {s.bb_low:.4f} - {s.bb_up:.4f}) | MACD: {s.macd_line:.4f}/{s.macd_signal:.4f} | Trend (L/S): {trend_long}/{trend_short} | VolMode: {vol_mode} (ATR: {current_atr:.5f} / 24h Avg: {atr_24h_avg:.5f})")
    
    is_in_bb_zone_long = close <= s.bb_low * 1.005
    is_in_bb_zone_short = close >= s.bb_up * 0.995
    
    macd_line = s.macd_line
    macd_signal = s.macd_signal
    prev_macd_line = s.prev_macd_line
    prev_macd_signal = s.prev_macd_signal
    
    macd_hist = macd_line - macd_signal
    prev_macd_hist = prev_macd_line - prev_macd_signal
    
    long_macd_cross = prev_macd_line <= prev_macd_signal and macd_line > macd_signal
    short_macd_cross = prev_macd_line >= prev_macd_signal and macd_line < macd_signal
    
    long_macd_hist_aligned = macd_hist > prev_macd_hist
    short_macd_hist_aligned = macd_hist < prev_macd_hist
    
    long_macd_ok = long_macd_cross or long_macd_hist_aligned
    short_macd_ok = short_macd_cross or short_macd_hist_aligned

    # 嚴格收盤價確認：要求突破上一根的收盤價，確保動能反轉
    last_candle_confirmed_long = len(s.ohlcv) >= 2 and close > s.ohlcv[-2][4]
    last_candle_confirmed_short = len(s.ohlcv) >= 2 and close < s.ohlcv[-2][4]

    logger.debug(f"@@COIN_DEBUG@@ 🔍 {sym} 條件檢測 | RSI門檻(L/S: {long_rsi_threshold:.0f}/{short_rsi_threshold:.0f}): {rsi < long_rsi_threshold}/{rsi > short_rsi_threshold} | BB區間(L/S): {is_in_bb_zone_long}/{is_in_bb_zone_short} | MACD滿足(L/S): {long_macd_ok}/{short_macd_ok} (交叉:{long_macd_cross}/{short_macd_cross}, 柱狀體向上/下:{long_macd_hist_aligned}/{short_macd_hist_aligned}) | 收盤價確認(L/S): {last_candle_confirmed_long}/{last_candle_confirmed_short}")

    ema50 = s.ema50
    trend_confluence_long = ema50 == 0.0 or close > ema50
    trend_confluence_short = ema50 == 0.0 or close < ema50

    is_above_sma200 = s.sma200_15m > 0 and close > s.sma200_15m * 0.999
    is_below_sma200 = s.sma200_15m > 0 and close < s.sma200_15m * 1.001

    # Route A (Right Side / 順勢交易): 只要價格站上均線、MACD有方向、且最近一根K線向上/向下確認即可
    route_a_long = (is_above_sma200 or trend_long) and (long_macd_cross or long_macd_hist_aligned) and last_candle_confirmed_long
    route_a_short = (is_below_sma200 or trend_short) and (short_macd_cross or short_macd_hist_aligned) and last_candle_confirmed_short

    # Route B (Left Side / 逆勢反轉): 只要 RSI 進入極端區且價格在布林帶外，且有最近一根K線確認即可
    route_b_long = rsi < long_rsi_threshold and is_in_bb_zone_long and last_candle_confirmed_long
    route_b_short = rsi > short_rsi_threshold and is_in_bb_zone_short and last_candle_confirmed_short

    momentum_long = close > prev_close * 1.001 and (s.current_vol >= max(1000.0, s.vol_ma20 * 0.8) or s.trade_signal_strength > 0.2)
    momentum_short = close < prev_close * 0.999 and (s.current_vol >= max(1000.0, s.vol_ma20 * 0.8) or s.trade_signal_strength > 0.2)

    # Route C (Left Side / 搶極端反彈): 只有在有順勢或足夠動能支持時才允許進場
    route_c_long = rsi <= 25.0 and last_candle_confirmed_long and (trend_long or (momentum_long and s.current_vol >= max(1000.0, s.vol_ma20 * 0.8)))
    route_c_short = rsi >= 75.0 and last_candle_confirmed_short and (trend_short or (momentum_short and s.current_vol >= max(1000.0, s.vol_ma20 * 0.8)))

    # Route S (Divergence / 背離確認機制): 最強反轉訊號
    bullish_div, bearish_div = check_rsi_divergence(s.closes, s.rsis, window=60)
    route_s_long = bullish_div and last_candle_confirmed_long
    route_s_short = bearish_div and last_candle_confirmed_short
    if route_s_long:
        logger.info(f"🌟 [背離確認] {sym} 出現看漲背離 (Bullish Divergence)!")
    if route_s_short:
        logger.info(f"🌟 [背離確認] {sym} 出現看跌背離 (Bearish Divergence)!")

    # 計算當前全域有多少筆左側交易 (Route B, C, S) 正在進行
    left_side_positions = sum(1 for state in STATES.values() if state.qty != 0 and getattr(state, 'entry_route', 'a') in ['b', 'c', 's'])


    long_base_ok = route_s_long or route_a_long or route_b_long or route_c_long or (momentum_long and last_candle_confirmed_long and (long_macd_cross or long_macd_hist_aligned) and trend_long)
    short_base_ok = route_s_short or route_a_short or route_b_short or route_c_short or (momentum_short and last_candle_confirmed_short and (short_macd_cross or short_macd_hist_aligned) and trend_short)

    if long_base_ok:
        route = "s" if route_s_long else "c" if route_c_long else "b" if route_b_long else "a"
        # 左側交易併發控制：如果不允許太多左側交易同時發生
        if route in ['b', 'c', 's'] and left_side_positions >= 1:
            logger.info(f"🛑 [風控] {sym} 觸發左側做多({route})，但目前已有左側倉位，放棄開倉。")
            long_base_ok = False
        else:
            if route == "a":
                strength = 4.0 + ((close - ema20) / max(ema20, 1e-8) * 100)
            else:
                strength = max(0.0, long_rsi_threshold - rsi) + ((ema20 - close) / max(ema20, 1e-8) * 100) + 4.0
            if momentum_long:
                strength += 3.0
            if strength > s.max_strength:
                s.max_strength = strength
            
        if long_macd_cross:
            strength += 5.0
        if route == "a":
            strength += 10.0  # Extra score for trend confluence
        return ("buy", strength if strength >= 6.0 else 0.0, route)

    if short_base_ok:
        route = "s" if route_s_short else "c" if route_c_short else "b" if route_b_short else "a"
        # 左側交易併發控制
        if route in ['b', 'c', 's'] and left_side_positions >= 1:
            logger.info(f"🛑 [風控] {sym} 觸發左側做空({route})，但目前已有左側倉位，放棄開倉。")
            short_base_ok = False
        else:
            if route == "a":
                strength = 4.0 + ((ema20 - close) / max(ema20, 1e-8) * 100)
            else:
                strength = max(0.0, rsi - short_rsi_threshold) + ((close - ema20) / max(ema20, 1e-8) * 100) + 4.0
            if momentum_short:
                strength += 3.0
        if short_macd_cross:
            strength += 5.0
        if route == "a":
            strength += 10.0  # Extra score for trend confluence
        return ("sell", strength if strength >= 6.0 else 0.0, route)

    return (None, 0, None)

async def process_symbols():
    await fetch_all_klines()
    async with STATES_LOCK:
        # 優先硬停損防護 (在指標運算前執行，確保不被卡頓拖延)
        for sym in ALL_SYMBOLS:
            s = STATES[sym]
            if abs(s.qty) > 0 and s.avg_price > 0:
                p = s.close_price
                avg = s.avg_price
                is_long = s.qty > 0
                profit_pct = (p - avg) / max(avg, 1e-8) if is_long else (avg - p) / max(avg, 1e-8)
                usdt_pnl = profit_pct * avg * abs(s.qty)
                if profit_pct <= -0.02:
                    cs = 'sell' if is_long else 'buy'
                    reason_msg = "2%跌幅停損"
                    print(f"🚨 [{reason_msg}] {sym} 觸發底線平倉！(指標結算前攔截) 目前虧損: {usdt_pnl:.2f} U")
                    await close_position(sym, cs, abs(s.qty), p, avg, reason=reason_msg, is_stop_loss=True)

        for sym in ALL_SYMBOLS:
            compute_indicators(sym)
        update_states()
        for sym in ALL_SYMBOLS:
            await check_position_exits(sym)
        await check_entries()

async def check_entries():
    if not GLOBAL_STATE["trading_enabled"]:
        return
        
    open_count = get_open_position_count()
    if open_count >= MAX_POSITIONS:
        return
    remaining_slots = MAX_POSITIONS - open_count

    candidates = []
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s.status != "ACTIVE":
            continue
        if abs(s.qty) > 0.000001:
            continue
        side_strength = compute_signal_strength(sym)
        if side_strength[0] is None:
            continue
        side, strength, route = side_strength
        if strength <= 0.0:
            continue
            
        logger.debug(f"@@COIN_DEBUG@@ 🔎 [訊號過濾前] {sym} | 方向:{side} | 分數:{strength:.2f} | 策略路由:{route}")
            
        if not risk_guard(sym, side, route):
            continue
        candidates.append((sym, side, strength, route))

    if not candidates:
        return

    candidates.sort(key=lambda x: -x[2])
    logger.info(f"📊 [訊號排行] {' | '.join(f'{sym}:{side}({strength:.2f})' for sym, side, strength, _ in candidates[:3])}")

    for i in range(min(remaining_slots, len(candidates))):
        sym, side, _, route = candidates[i]
        s = STATES[sym]
        now = time.time()
        
        # 移除突破確認機制，只要有訊號 (且通過 risk_guard) 就立即開倉
        await execute_order(sym, side, s.close_price)

# ── 主循環 ──────────────────────────────────────────────────

async def watch_symbol_trades(sym):
    while True:
        try:
            trades = await exchange.watch_trades(sym)
            if trades:
                async with STATES_LOCK:
                    data = trades if isinstance(trades, list) else [trades]
                    for trade in data:
                        update_trade_signal(sym, trade)
        except Exception as e:
            logger.warning(f"⚠️ [成交流監聽異常] {sym}: {e}，5秒後重試")
            await asyncio.sleep(5)


async def ensure_watch_tasks():
    global WATCH_TASKS
    desired_symbols = set(ALL_SYMBOLS)
    current_symbols = set(WATCH_TASKS.keys())

    # 移除失效任務
    for sym in current_symbols - desired_symbols:
        task = WATCH_TASKS.pop(sym, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # 加入新任務
    for sym in desired_symbols - current_symbols:
        WATCH_TASKS[sym] = asyncio.create_task(watch_symbol_trades(sym))


async def watch_all_trades():
    while True:
        try:
            await ensure_watch_tasks()
            await asyncio.sleep(5)
        except Exception as e:
            print(f"❌ [成交流監聽錯誤] {e}")
            await asyncio.sleep(3)


async def main_loop():
    print("🚀 [多幣輪動] 啟動多幣種輪動交易機器人")
    logger.info(f"📊 最大同時持倉: {MAX_POSITIONS}")
    print(f"📡 模式: {'模擬' if PAPER_TRADING else '實盤'}")
    print(f"@@LEVERAGE@@{LEVERAGE}")
    try:
        await asyncio.wait_for(exchange.load_markets(), timeout=15)
    except Exception as e:
        logger.warning(f"⚠️ load_markets 失敗 ({e})，使用預設市場清單")
    
    global ALL_SYMBOLS
    ALL_SYMBOLS = filter_valid_symbols(ALL_SYMBOLS)
    save_symbol_pool(ALL_SYMBOLS)
    
    print(f"📋 監控幣種: {', '.join(ALL_SYMBOLS)}")
    try:
        await asyncio.wait_for(initialize_atr_history(), timeout=120)
    except (asyncio.TimeoutError, Exception) as e:
        logger.info(f"⏳ [初始化] ATR 歷史預熱超時或失敗 ({e})，將在運行中慢慢加熱")
    await fetch_real_balance()
    await load_open_positions()
    await fetch_all_sma200()
    await fetch_all_htf_trend()

    last_balance_update = time.time()
    last_htf_update = time.time()

    while True:
        try:
            loop_start = time.time()
            if loop_start - last_balance_update > 30:
                await fetch_real_balance()
                last_balance_update = loop_start
                current_balance = get_balance()
                
                # Daily Equity Drawdown Protection
                current_day = datetime.datetime.utcnow().strftime("%Y-%m-%d")
                if GLOBAL_STATE["last_reset_day"] != current_day or GLOBAL_STATE["initial_daily_equity"] == 0.0:
                    GLOBAL_STATE["last_reset_day"] = current_day
                    GLOBAL_STATE["initial_daily_equity"] = current_balance
                    logger.info(f"📅 [每日重置] 記錄初始本金: {current_balance:.2f} U")
                
                if GLOBAL_STATE["initial_daily_equity"] > 0 and GLOBAL_STATE["trading_enabled"]:
                    dd_pct = (current_balance - GLOBAL_STATE["initial_daily_equity"]) / GLOBAL_STATE["initial_daily_equity"]
                    if dd_pct <= -0.10:
                        logger.error(f"🚨🚨 [淨值回撤保護] 今日總淨值回撤達 {dd_pct*100:.2f}% (<-10%)，強制關閉交易至明日！")
                        GLOBAL_STATE["trading_enabled"] = False
                
            if loop_start - last_htf_update > 1800:  # 每 30 分鐘更新一次大週期
                await fetch_all_htf_trend()
                last_htf_update = loop_start

            for sym in ALL_SYMBOLS:
                STATES[sym].adjusted_this_tick = False
            if ALL_SYMBOLS != load_symbol_pool():
                apply_symbol_pool_change(load_symbol_pool())
                await fetch_all_sma200()
                await fetch_all_htf_trend()
            await ensure_watch_tasks()
            await update_market_wind()
            await process_symbols()
            
            # 成功執行，重置連續錯誤計數器
            global CONSECUTIVE_ERRORS
            CONSECUTIVE_ERRORS = 0
            
            # 權重節流檢測
            weight_sleep = check_binance_weight()
            
            elapsed = time.time() - loop_start
            sleep_time = max(1.5, MAIN_LOOP_INTERVAL_SEC - elapsed) + weight_sleep
            await asyncio.sleep(sleep_time)
        except ccxt.DDoSProtection as e:
            logger.error(f"🚨 [API限流 429] 檢測到 DDoSProtection 限流，冷卻 10 秒: {e}")
            await asyncio.sleep(10)
        except ccxt.RateLimitExceeded as e:
            logger.error(f"🚨 [API限流 429] 檢測到 RateLimitExceeded 限流，冷卻 10 秒: {e}")
            await asyncio.sleep(10)
        except Exception as e:
            if "429" in str(e):
                logger.error(f"🚨 [API限流 429] 檢測到 429 錯誤，冷卻 10 秒: {e}")
                await asyncio.sleep(10)
                continue
            import traceback
            CONSECUTIVE_ERRORS += 1
            print(f"❌ [主循環錯誤] 當前連續錯誤數: {CONSECUTIVE_ERRORS} | 錯誤: {e}")
            traceback.print_exc()
            
            # 連續錯誤防爆防封禁冷卻機制
            if CONSECUTIVE_ERRORS >= 10:
                logger.error(f"🚨🚨 [極端防護] 連續錯誤達 10 次，暫時關閉自動交易以防失控！")
                GLOBAL_STATE["trading_enabled"] = False
                # Optionally send line alert if implemented here, but we will just disable trading.
                await asyncio.sleep(60)
            elif CONSECUTIVE_ERRORS >= 3:
                cooldown = min(120, 15 * (CONSECUTIVE_ERRORS - 2))
                logger.error(f"🚨 [連續API錯誤風控] 已連續錯誤 {CONSECUTIVE_ERRORS} 次，觸發風控冷卻，暫停 {cooldown} 秒...")
                await asyncio.sleep(cooldown)
            else:
                await asyncio.sleep(5)

async def periodic_sma200_update():
    while True:
        await asyncio.sleep(900)
        await fetch_all_sma200()
        print("🔄 [SMA200] 已更新所有幣種15m SMA200")

async def periodic_status_log():
    while True:
        await asyncio.sleep(60)
        active = sum(1 for s in STATES.values() if s.status == "ACTIVE")
        cooldown = sum(1 for s in STATES.values() if s.status == "COOLDOWN")
        banned = sum(1 for s in STATES.values() if s.status == "BANNED")
        open_syms = get_open_symbols()
        open_str = ', '.join(f"{sym}({'多' if STATES[sym].qty>0 else '空'})" for sym in open_syms) if open_syms else "無"
        logger.info(f"📊 [狀態] ACTIVE={active} COOLDOWN={cooldown} BANNED={banned} | 持倉({len(open_syms)}): {open_str}")

async def export_states_to_json():
    while True:
        try:
            await asyncio.sleep(3)
            export_data = {}
            # We don't necessarily need STATES_LOCK for a simple read copy, but it's safer.
            # However, acquiring lock every 3 sec might block, so we just do a dirty read.
            for sym in list(STATES.keys()):
                s = STATES[sym]
                export_data[sym] = {
                    "status": s.status,
                    "qty": s.qty,
                    "avg_price": s.avg_price,
                    "current_price": s.close_price,
                    "rsi": s.current_rsi,
                    "atr": s.current_atr,
                    "trend": s.htf_trend
                }
            with open("bot_states.json", "w") as f:
                json.dump(export_data, f)
        except Exception as e:
            pass

async def main():
    global STATES_LOCK
    STATES_LOCK = asyncio.Lock()
    await asyncio.gather(
        main_loop(),
        periodic_sma200_update(),
        periodic_status_log(),
        watch_all_trades(),
        export_states_to_json(),
    )

if __name__ == "__main__":
    asyncio.run(main())
