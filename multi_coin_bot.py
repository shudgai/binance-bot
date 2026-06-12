import asyncio
import ccxt
import ccxt.pro as ccxtpro
import numpy as np
import json
import os
import time
import sys
import uuid
import fcntl
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
VOLUME_RATIO_THRESHOLD = 0.7

if USE_TESTNET:
    exchange.urls['api']['fapiPublic'] = 'https://testnet.binancefuture.com/fapi/v1'
    exchange.urls['api']['fapiPrivate'] = 'https://testnet.binancefuture.com/fapi/v1'

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT",
    "NEARUSDT", "TONUSDT", "SUIUSDT", "BNBUSDT", "LINKUSDT",
    "AVAXUSDT", "INJUSDT"
]
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


def normalize_symbol_list(symbols, max_count=20):
    if isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        return list(DEFAULT_SYMBOLS[:max_count])

    seen = []
    for item in symbols:
        sym = normalize_symbol(item)
        if sym and sym not in seen:
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
        print(f"⚠️ 讀取幣種清單失敗: {e}")
        return list(DEFAULT_SYMBOLS)


def save_symbol_pool(symbols):
    normalized = normalize_symbol_list(symbols)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"symbols": normalized}, f, ensure_ascii=False)
    return normalized


ALL_SYMBOLS = load_symbol_pool()

MAX_POSITIONS = 2
COOLDOWN_SEC = 300
MAIN_LOOP_INTERVAL_SEC = 6
PENDING_CONFIRM_SEC = 2
BAN_WINDOW = 3600
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 3
SL_ATR_MULTIPLIER = 2.5
TP_ATR_MULTIPLIER = 2.5
HARD_STOP_LOSS_PCT = 0.02

def build_symbol_state(sym):
    return {
        "status": "ACTIVE",
        "status_reason": "",
        "next_status_time": 0,
        "stop_count": 0,
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
        "entry_cooldown_sec": 90,
        "last_entry_time": 0.0,
        "htf_trend": None,
        "htf_ema20": 0.0,
        "rsis": [],
    }

STATES = {sym: build_symbol_state(sym) for sym in ALL_SYMBOLS}
WATCH_TASKS = {}

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
                print(f"⚠️ [API限流警報] 幣安目前權重已達 {weight}/1200，觸發重度防護，冷卻 10 秒")
                return 10.0
            elif weight > 700:
                print(f"⚠️ [API限流警報] 幣安目前權重已達 {weight}/1200，觸發輕度防護，冷卻 3 秒")
                return 3.0
    except Exception as e:
        print(f"⚠️ [API權重讀取失敗] {e}")
    return 0.0

CONSECUTIVE_ERRORS = 0

# ── 狀態管理 ──────────────────────────────────────────────────

def get_active_count():
    return sum(1 for s in STATES.values() if s["status"] == "ACTIVE")

def get_open_position_count():
    return sum(1 for s in STATES.values() if abs(s["qty"]) > 0.000001)

def get_open_symbols():
    return [sym for sym in ALL_SYMBOLS if abs(STATES[sym]["qty"]) > 0.000001]


def is_symbol_locked(sym):
    s = STATES[sym]
    return abs(s["qty"]) > 0.000001 or s["entry_count"] > 0 or s["open_time"] > 0 or s["status"] in ("COOLDOWN", "BANNED")


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
            print(f"⚠️ [過濾無效幣種] 交易所目前不支援/已下架此幣種，已自動移出監聽清單: {sym}")
    return valid


def apply_symbol_pool_change(requested_symbols):
    global ALL_SYMBOLS
    desired = filter_valid_symbols(normalize_symbol_list(requested_symbols))
    locked_symbols = [sym for sym in ALL_SYMBOLS if is_symbol_locked(sym)]

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

    s["last_trade_price"] = price
    s["last_trade_qty"] = amount
    s["last_trade_side"] = str(trade.get("side", "buy") or "buy")
    s["last_trade_time"] = ts_value
    s["trade_price_history"].append(price)
    s["trade_qty_history"].append(amount)

    if len(s["trade_price_history"]) > 20:
        s["trade_price_history"] = s["trade_price_history"][-20:]
    if len(s["trade_qty_history"]) > 20:
        s["trade_qty_history"] = s["trade_qty_history"][-20:]

    if len(s["trade_price_history"]) < 2:
        return

    prev_price = s["trade_price_history"][-2]
    prev_qty = s["trade_qty_history"][-2] if len(s["trade_qty_history"]) >= 2 else amount
    if prev_price <= 0:
        prev_price = price

    price_change_pct = abs(price - prev_price) / max(prev_price, 1e-8)
    avg_qty = float(np.mean(s["trade_qty_history"][-5:])) if len(s["trade_qty_history"]) >= 5 else amount
    qty_ratio = amount / max(avg_qty, 1e-8)
    score = min(3.0, qty_ratio * 0.35 + price_change_pct * 25.0)

    if qty_ratio >= 4.0 and price_change_pct >= 0.004:
        s["trade_signal_strength"] = score
        s["trade_signal_reason"] = f"即時大額成交 {amount:.3f} / {qty_ratio:.1f}x 均量"
    else:
        s["trade_signal_strength"] = max(0.0, s["trade_signal_strength"] * 0.85 - 0.05)
        if s["trade_signal_strength"] < 0.15:
            s["trade_signal_strength"] = 0.0
            s["trade_signal_reason"] = ""


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
        print(f"⚠️ [餘額獲取失敗] {e}")

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
    # 避免使用過大槓桿額度嚇到使用者，此處改為本金直下 (不乘上 LEVERAGE)
    usable = balance * 0.95
    per_slot = usable / MAX_POSITIONS
    return per_slot

# ── 幣種狀態更新 ──────────────────────────────────────────────

def update_states():
    now = time.time()
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s["status"] == "COOLDOWN" and now >= s["next_status_time"]:
            s["status"] = "ACTIVE"
            s["status_reason"] = ""
            print(f"🔄 [狀態] {sym} 冷卻結束 → ACTIVE")
        if s["status"] == "BANNED" and now >= s["next_status_time"]:
            s["status"] = "ACTIVE"
            s["status_reason"] = ""
            s["stop_count"] = 0
            s["first_stop_time"] = 0
            print(f"🔄 [狀態] {sym} 封禁解除 → ACTIVE")

def mark_exit(sym, is_stop_loss=False):
    s = STATES[sym]
    now = time.time()
    s["status"] = "COOLDOWN"
    s["next_status_time"] = now + COOLDOWN_SEC
    s["status_reason"] = "冷卻中 (5分鐘)"
    print(f"⏳ [狀態] {sym} 平倉 → COOLDOWN 5分鐘")
    if is_stop_loss:
        s["stop_count"] += 1
        if s["stop_count"] == 1:
            s["first_stop_time"] = now
        if s["stop_count"] >= MAX_STOPS_IN_WINDOW and (now - s["first_stop_time"]) <= BAN_WINDOW:
            s["status"] = "BANNED"
            s["next_status_time"] = now + BAN_DURATION
            s["status_reason"] = f"封禁中 (24h，{MAX_STOPS_IN_WINDOW}次停損)"
            print(f"🚫 [狀態] {sym} 1h內{MAX_STOPS_IN_WINDOW}次停損 → BANNED 24h")
        elif s["stop_count"] >= MAX_STOPS_IN_WINDOW:
            s["stop_count"] = 1
            s["first_stop_time"] = now

def reset_coin_state(sym):
    s = STATES[sym]
    s["qty"] = 0.0
    s["avg_price"] = 0.0
    s["open_time"] = 0.0
    s["trailing_highest"] = 0.0
    s["trailing_lowest"] = float('inf')
    s["highest_profit_pct"] = 0.0
    s["has_partial_closed"] = False
    s["pending_side"] = None
    s["pending_time"] = 0
    s["pending_confirm_high"] = 0
    s["pending_confirm_low"] = 0
    s["has_been_negative"] = False
    s["trail_tp_price"] = 0.0
    s["entry_count"] = 0
    s["avg_entry_price"] = 0.0
    s["last_entry_time"] = 0.0

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
            print(f"⚠️ [大盤瀑布風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣多單開倉！")
        elif btc_change_15m > 0.012 or eth_change_15m > 0.015:
            MARKET_WIND["allow_short"] = False
            print(f"⚠️ [大盤暴漲風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣空單開倉！")
            
    except Exception as e:
        print(f"⚠️ [更新大盤風向失敗]: {e}")

# ── 資料獲取 ──────────────────────────────────────────────────

async def initialize_atr_history():
    print("⏳ [初始化] 開始獲取 1000 根 1m K線以預熱 ATR 歷史...")
    tasks = {}
    for sym in ALL_SYMBOLS:
        tasks[sym] = exchange.fetch_ohlcv(sym, '1m', limit=1000)
    results = await asyncio.gather(*[tasks[sym] for sym in ALL_SYMBOLS], return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception) and results[i]:
            ohlcv = results[i]
            tr_list = []
            for j in range(1, len(ohlcv)):
                h = ohlcv[j][2]
                l = ohlcv[j][3]
                pc = ohlcv[j-1][4]
                tr = max(h - l, abs(h - pc), abs(l - pc))
                tr_list.append(tr)
                if len(tr_list) >= 14:
                    atr = float(np.mean(tr_list[-14:]))
                    STATES[sym]["atr_history"].append(atr)
            print(f"✅ [初始化] {sym} 歷史 ATR 預熱完成，載入 {len(STATES[sym]['atr_history'])} 筆數據")
        else:
            print(f"⚠️ [初始化] {sym} 歷史 ATR 預熱失敗: {results[i]}")

async def fetch_all_klines():
    tasks = {}
    for sym in ALL_SYMBOLS:
        tasks[sym] = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=100)
    results = await asyncio.gather(*[tasks[sym] for sym in ALL_SYMBOLS], return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym]["ohlcv"] = results[i]
            STATES[sym]["close_price"] = results[i][-1][4]
        else:
            print(f"⚠️ [K線獲取失敗] {sym}: {results[i]}")

async def fetch_sma200_15m(sym):
    try:
        ohlcv = await exchange.fetch_ohlcv(sym, '15m', limit=200)
        closes = np.array([x[4] for x in ohlcv])
        return float(np.mean(closes))
    except Exception as e:
        print(f"⚠️ [SMA200獲取失敗] {sym}: {e}")
        return 0.0

async def fetch_all_sma200():
    tasks = {sym: fetch_sma200_15m(sym) for sym in ALL_SYMBOLS}
    results = await asyncio.gather(*[tasks[sym] for sym in tasks], return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym]["sma200_15m"] = results[i]

async def fetch_htf_trend(sym):
    try:
        ohlcv = await exchange.fetch_ohlcv(sym, '1h', limit=50)
        closes = np.array([float(x[4]) for x in ohlcv])
        ema20 = calculate_ema(closes, 20)
        current_close = closes[-1]
        trend = "long" if current_close > ema20 else "short"
        return trend, ema20
    except Exception as e:
        print(f"⚠️ [HTF獲取失敗] {sym}: {e}")
        return None, 0.0

async def fetch_all_htf_trend():
    tasks = {sym: fetch_htf_trend(sym) for sym in ALL_SYMBOLS}
    results = await asyncio.gather(*[tasks[sym] for sym in tasks], return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            trend, ema20 = results[i]
            if trend:
                STATES[sym]["htf_trend"] = trend
                STATES[sym]["htf_ema20"] = ema20

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
                STATES[sym]["qty"] = qty
                STATES[sym]["avg_price"] = float(pos.get("avg_price", 0.0))
    except Exception as e:
        print(f"⚠️ [讀取持倉失敗] {e}")

# ── 指標計算 ──────────────────────────────────────────────────

def compute_indicators(sym):
    s = STATES[sym]
    ohlcv = s["ohlcv"]
    if len(ohlcv) < 20:
        return
    closes = np.array([x[4] for x in ohlcv])
    highs = np.array([x[2] for x in ohlcv])
    lows = np.array([x[3] for x in ohlcv])
    volumes = np.array([x[5] for x in ohlcv])
    s["closes"] = closes
    prev = s["prev_close"]
    for i in range(len(ohlcv)):
        h, l, c = ohlcv[i][2], ohlcv[i][3], ohlcv[i][4]
        if i == 0 and prev is not None:
            tr = max(h - l, abs(h - prev), abs(l - prev))
        elif i > 0:
            tr = max(h - l, abs(h - ohlcv[i-1][4]), abs(l - ohlcv[i-1][4]))
        else:
            tr = h - l
        s["tr_list"].append(tr)
    s["prev_close"] = ohlcv[-1][4]
    if len(s["tr_list"]) > 42:
        s["tr_list"] = s["tr_list"][-42:]
    if len(s["tr_list"]) >= 14:
        s["current_atr"] = float(np.mean(s["tr_list"][-14:]))
        s["atr_history"].append(s["current_atr"])
        if len(s["atr_history"]) > 1440:
            s["atr_history"] = s["atr_history"][-1440:]
        s["atr_ma20"] = float(np.mean(s["atr_history"][-20:])) if len(s["atr_history"]) >= 20 else s["current_atr"]
    if len(closes) > RSI_PERIOD:
        s["rsis"] = calculate_rsi_array(closes, RSI_PERIOD)
        s["current_rsi"] = s["rsis"][-1]
    s["vol_ma20"] = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))
    s["current_vol"] = float(volumes[-1])
    if len(closes) >= 20:
        s["ema20"] = calculate_ema(closes, 20)
    if len(closes) >= 50:
        s["ema50"] = calculate_ema(closes, 50)
    if len(closes) >= 26:
        m_line, m_sig, m_hist, p_line, p_sig = calculate_macd(closes)
        s["macd_line"] = m_line
        s["macd_signal"] = m_sig
        s["macd_hist"] = m_hist
        s["prev_macd_line"] = p_line
        s["prev_macd_signal"] = p_sig
    if len(closes) >= 20:
        up, mid, low = calculate_bollinger_bands(closes)
        s["bb_up"] = up
        s["bb_mid"] = mid
        s["bb_low"] = low

# ── 出場邏輯 ──────────────────────────────────────────────────

def update_trailing_take_profit(sym, current_price, is_long):
    s = STATES[sym]
    avg_price = s["avg_price"]
    if avg_price <= 0:
        return False, 0.0

    if s.get("trail_tp_price", 0.0) <= 0:
        if is_long:
            s["trail_tp_price"] = avg_price * 1.003
        else:
            s["trail_tp_price"] = avg_price * 0.997

    if is_long:
        if current_price <= s["trail_tp_price"]:
            return True, s["trail_tp_price"]
        new_tp = current_price * 0.997
        s["trail_tp_price"] = max(s["trail_tp_price"], new_tp)
        return False, s["trail_tp_price"]

    if current_price >= s["trail_tp_price"]:
        return True, s["trail_tp_price"]
    new_tp = current_price * 1.003
    s["trail_tp_price"] = min(s["trail_tp_price"], new_tp)
    return False, s["trail_tp_price"]


def should_recover_from_reversal(sym, is_long):
    s = STATES[sym]
    if abs(s["qty"]) < 0.000001:
        return False

    macd_reversal = (is_long and s["prev_macd_line"] > s["prev_macd_signal"] and s["macd_line"] < s["macd_signal"]) or \
                    (not is_long and s["prev_macd_line"] < s["prev_macd_signal"] and s["macd_line"] > s["macd_signal"])

    breakout_confirmed = False
    if s["prev_close"] and len(s["ohlcv"]) >= 1:
        prev_bar_idx = -2 if len(s["ohlcv"]) >= 2 else -1
        prev_bar_high = s["ohlcv"][prev_bar_idx][2]
        prev_bar_low = s["ohlcv"][prev_bar_idx][3]
        break_high = s["close_price"] > s["prev_close"] and s["close_price"] >= prev_bar_high
        break_low = s["close_price"] < s["prev_close"] and s["close_price"] <= prev_bar_low
        breakout_confirmed = (is_long and break_low) or (not is_long and break_high)

    volume_confirmed = s["current_vol"] > s["vol_ma20"] * 2.0
    trade_signal = s.get("trade_signal_strength", 0.0)
    trade_confirmed = trade_signal >= 1.5

    if macd_reversal and breakout_confirmed and (volume_confirmed or trade_confirmed):
        return True

    return False


def detect_market_regime(sym, current_price, avg_price, is_long):
    s = STATES[sym]
    if len(s["ohlcv"]) < 20 or avg_price <= 0:
        return "HOLD", "資料不足"

    recent_candles = s["ohlcv"][-20:]
    highs = np.array([x[2] for x in recent_candles])
    lows = np.array([x[3] for x in recent_candles])
    closes = np.array([x[4] for x in recent_candles])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / recent_low if recent_low > 0 else 0

    atr_val = s["current_atr"] if s["current_atr"] > 0 else (current_price * 0.01)
    atr_pct = atr_val / current_price if current_price > 0 else 0

    # 1) 即時成交流監聽：大額成交且價格急速變動，優先判定為突破反轉
    trade_signal = s.get("trade_signal_strength", 0.0)
    if trade_signal >= 1.1:
        return "BREAKOUT_REVERSAL", f"即時大額成交異常 {s['trade_signal_reason']}"

    # 2) 簡化的大單/突發行情判斷：放量且價格急速變動
    volume_surge = s["current_vol"] > s["vol_ma20"] * 2.5
    price_jump = abs(current_price - s["prev_close"]) / max(s["prev_close"], 1e-8) > 0.01 if s["prev_close"] else False
    if volume_surge and price_jump:
        return "BREAKOUT_REVERSAL", "放量突發且價格急速變動"

    # 2) 盤整市場：價格被壓縮在狹窄區間內，且 ATR 也偏小
    is_ranging = range_width_pct < 0.025 and atr_pct < 0.015
    if is_ranging:
        profit_pct = (current_price - avg_price) / max(avg_price, 1e-8) if is_long else (avg_price - current_price) / max(avg_price, 1e-8)
        if profit_pct >= 0.003:
            return "RANGE_PROFIT_TAKE", f"盤整區間內已獲利 {profit_pct * 100:.2f}%"

    return "HOLD", "未達出場條件"

async def close_position(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = STATES[sym]
    s["adjusted_this_tick"] = True
    pk = paper_key(sym)
    qty = min(abs(qty), abs(s["qty"]))
    if qty < 0.000001:
        return
    close_qty = qty if close_side == 'sell' else -qty
    if PAPER_TRADING:
        if s["qty"] > 0:
            pnl = (price - avg_price) * qty
        else:
            pnl = (avg_price - price) * qty
        update_paper_state(pk, close_side, price, qty, is_close=True, pnl=pnl)
    else:
        try:
            await exchange.create_order(sym, type='market', side=close_side, amount=qty,
                                        params={'reduceOnly': True, 'marginMode': 'isolated'})
        except Exception as e:
            print(f"🚨 [平倉錯誤] {sym}: {e}")
            return
    remaining = abs(s["qty"]) - qty
    if remaining < 0.01:
        if remaining > 0.000001:
            print(f"🧹 [塵埃清理] {sym} 剩餘 {remaining:.6f} 視為已清")
        mark_exit(sym, is_stop_loss=is_stop_loss)
        reset_coin_state(sym)
    else:
        s["qty"] = (abs(s["qty"]) - qty) * (1 if s["qty"] > 0 else -1)
        print(f"✅ [部分平] {sym} 平{qty} 剩{abs(s['qty']):.4f} {reason}")

async def check_exits(sym):
    s = STATES[sym]
    if abs(s["qty"]) < 0.000001 or s["avg_price"] <= 0:
        return
    hold_sec = time.time() - s["open_time"] if s["open_time"] > 0 else 9999
    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)
    cooldown_limit = 3.0 if (current_atr > atr_24h_avg and atr_24h_avg > 0) else 10.0
    if hold_sec < cooldown_limit:
        return

    p = s["close_price"]
    avg = s["avg_price"]
    is_long = s["qty"] > 0
    profit_pct = (p - avg) / max(avg, 1e-8) if is_long else (avg - p) / max(avg, 1e-8)


    sl_mult = SL_ATR_MULTIPLIER * 2 if hold_sec < 120 else SL_ATR_MULTIPLIER
    atr_val = s["current_atr"] if s["current_atr"] > 0 else (p * 0.01)

    if profit_pct > s["highest_profit_pct"]:
        s["highest_profit_pct"] = profit_pct
    if profit_pct < 0:
        s["has_been_negative"] = True

    regime_decision, regime_reason = detect_market_regime(sym, p, avg, is_long)
    if regime_decision == "BREAKOUT_REVERSAL":
        cs = 'sell' if is_long else 'buy'
        print(f"🚨 [市場 regime] {sym} {regime_reason}，立即平倉並反手")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="市場反轉/大單突破", is_stop_loss=True)
        s["highest_profit_pct"] = 0.0
        return

    if should_recover_from_reversal(sym, is_long):
        recovery_side = 'sell' if is_long else 'buy'
        print(f"🔄 [反向補救] {sym} 方向錯誤且出現反轉訊號，直接反手")
        await close_position(sym, recovery_side, abs(s["qty"]), p, avg, reason="反轉補救", is_stop_loss=True)
        s["highest_profit_pct"] = 0.0
        return
    if regime_decision == "RANGE_PROFIT_TAKE":
        cs = 'sell' if is_long else 'buy'
        print(f"📈 [盤整獲利] {sym} {regime_reason}，提前獲利了結")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="盤整獲利了結")
        s["highest_profit_pct"] = 0.0
        return

    # 動能衰減檢查：利潤溜滑梯
    s["pnl_history"].append(profit_pct * 100)
    if len(s["pnl_history"]) > 5:
        s["pnl_history"].pop(0)
    if len(s["pnl_history"]) == 5:
        is_decaying = all(s["pnl_history"][i] > s["pnl_history"][i+1] for i in range(4))
        if is_decaying and profit_pct * 100 > 0.5:
            cs = 'sell' if is_long else 'buy'
            print(f"📉 [動能衰減] {sym} 利潤連5次下滑 ({profit_pct*100:.2f}%)，即時出場")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="動能衰減")
            s["highest_profit_pct"] = 0.0
            return
    if p > s["trailing_highest"]:
        s["trailing_highest"] = p
    if p < s["trailing_lowest"]:
        s["trailing_lowest"] = p

    # ── 三叉決策樹出場邏輯 ─────────────────────────────────────
    # 1. 趨勢反轉：MACD 反向交叉 → 立即認賠出場 (無視盈虧)
    m_death = s["prev_macd_line"] > s["prev_macd_signal"] and s["macd_line"] < s["macd_signal"]
    m_golden = s["prev_macd_line"] < s["prev_macd_signal"] and s["macd_line"] > s["macd_signal"]
    if (is_long and m_death) or (not is_long and m_golden):
        cs = 'sell' if is_long else 'buy'
        is_sl = profit_pct < 0.0
        print(f"📉 [反轉出場] {sym} MACD反向交叉，立即平倉 (損益: {profit_pct*100:.2f}%)")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="趨勢反轉", is_stop_loss=is_sl)
        return

    # 2. 判斷市場狀態：強勢 / 弱勢
    is_strong = (is_long and s["current_rsi"] > 50) or (not is_long and s["current_rsi"] <= 50)

    # ATR TP/SL 價格 (兩條路線共用)
    if is_long:
        tp = avg + max(atr_val * TP_ATR_MULTIPLIER, avg * 0.003)
        sl = avg - (atr_val * sl_mult)
    else:
        tp = avg - max(atr_val * TP_ATR_MULTIPLIER, avg * 0.003)
        sl = avg + (atr_val * sl_mult)

    # ── 保本鎖利與利潤防護機制 (Break-even & Capital Protection Lock) ──
    # 實施分級保本與回撤防護，防止「有利潤不平倉，最後被打到停損」
    if s["highest_profit_pct"] >= 0.015 and profit_pct < s["highest_profit_pct"] * 0.5:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [回撤鎖利] {sym} 獲利最高曾達 {s['highest_profit_pct']*100:.3f}%，回撤已達50% (目前 {profit_pct*100:.3f}%)，觸發回撤平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="回撤鎖利防護")
        s["highest_profit_pct"] = 0.0
        return
    elif s["highest_profit_pct"] >= 0.010 and profit_pct < 0.005:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [高利鎖利] {sym} 獲利最高曾達 {s['highest_profit_pct']*100:.3f}%，目前回落至 {profit_pct*100:.3f}%，觸發高利保護平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="高利鎖利防護")
        s["highest_profit_pct"] = 0.0
        return
    elif s["highest_profit_pct"] >= 0.008 and profit_pct < 0.003:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [中利鎖利] {sym} 獲利最高曾達 {s['highest_profit_pct']*100:.3f}%，目前回落至 {profit_pct*100:.3f}%，觸發中利保護平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="中利鎖利防護")
        s["highest_profit_pct"] = 0.0
        return
    elif s["highest_profit_pct"] >= 0.006 and profit_pct < 0.0015:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [微利鎖利] {sym} 獲利最高曾達 {s['highest_profit_pct']*100:.3f}%，目前回落至 {profit_pct*100:.3f}%，觸發微利保護平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="微利鎖利防護")
        s["highest_profit_pct"] = 0.0
        return
    elif s["highest_profit_pct"] >= 0.003 and profit_pct < 0.0005:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [強制保本] {sym} 獲利最高曾達 {s['highest_profit_pct']*100:.3f}%，不允許轉盈為虧，觸發強制保本平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="強制保本防護")
        s["highest_profit_pct"] = 0.0
        return

    # 見好就收：先以 0.4% (原本 0.8%) 進行停利，若後續價格再創新高，就把停利線往上移動
    early_take_profit_pct = 0.004
    if s["highest_profit_pct"] >= early_take_profit_pct and profit_pct >= 0.003:
        should_exit, trail_tp = update_trailing_take_profit(sym, p, is_long)
        if should_exit:
            cs = 'sell' if is_long else 'buy'
            print(f"🎯 [移動停利] {sym} 已達到 {early_take_profit_pct*100:.1f}% 目標，按移動停利出場")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="移動停利")
            s["highest_profit_pct"] = 0.0
            return

    if not is_strong:
        # ── 盤整／弱勢路線 ────────────────────────────────
        # 僵局一階：時間到 → 只有在真實波動率萎縮時才執行
        stagnation_limit = get_dynamic_stagnation_limit(s["current_atr"], s["atr_ma20"])
        if hold_sec > stagnation_limit and profit_pct > 0:
            if s["current_atr"] < s["atr_ma20"] * 0.8:
                if not s["has_partial_closed"] and 0.002 <= profit_pct < 0.005:
                    half = abs(s["qty"]) * 0.5
                    cs = 'sell' if is_long else 'buy'
                    print(f"⏳ [僵局一階] {sym} 波動率萎縮，持倉{stagnation_limit//60}分利潤{profit_pct*100:.2f}%，平50%")
                    await close_position(sym, cs, half, p, avg, reason="僵局一階")
                    s["has_partial_closed"] = True
                    return
                if profit_pct < 0.002:
                    cs = 'sell' if is_long else 'buy'
                    print(f"⏳ [僵局平倉] {sym} 波動率萎縮，持倉{stagnation_limit//60}分利潤僅{profit_pct*100:.2f}%，全平")
                    await close_position(sym, cs, abs(s["qty"]), p, avg, reason="僵局平倉")
                    s["highest_profit_pct"] = 0.0
                    return
        # 僵局二階：平過50% + 8分仍未突破1% → 全平
        if s["has_partial_closed"] and hold_sec > 480 and profit_pct < 0.01:
            cs = 'sell' if is_long else 'buy'
            print(f"⏳ [僵局二階] {sym} 剩餘50%持倉8分仍未突破1%，全平")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="僵局二階")
            s["highest_profit_pct"] = 0.0
            s["has_partial_closed"] = False
            return
        # 弱勢快速停利：0.8% 就走
        weak_tp = 0.008
        if s["highest_profit_pct"] >= weak_tp:
            cs = 'sell' if is_long else 'buy'
            print(f"🎯 [快速停利] {sym} 弱勢利潤達{weak_tp*100:.1f}%")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="快速停利")
            s["highest_profit_pct"] = 0.0
            return
        # 弱勢 ATR 停損直接觸發 (停利在弱勢下先抓快速停利)
        if (is_long and p <= sl) or (not is_long and p >= sl):
            cs = 'sell' if is_long else 'buy'
            sl_pct = abs(sl - avg) / avg * 100
            print(f"🛑 [ATR停損] {sym} -{sl_pct:.1f}%")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="ATR停損", is_stop_loss=True)
            return
    else:
        # ── 強勢路線 ────────────────────────────────────
        # 強勢動態停利：高點回撤 0.5%
        # 改為當利潤達到 1.5 倍 ATR 距離時啟動
        atr_profit_pct = (s["current_atr"] / avg) * 1.5 if avg > 0 else 0.015
        if s["highest_profit_pct"] >= atr_profit_pct:
            if (is_long and p <= s["trailing_highest"] * 0.995) or (not is_long and p >= s["trailing_lowest"] * 1.005):
                cs = 'sell' if is_long else 'buy'
                print(f"🏃 [動態停利] {sym} 利潤達 {s['highest_profit_pct']*100:.2f}% (超過1.5ATR)，高點回撤0.5%，果斷收割")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="動態停利")
                s["highest_profit_pct"] = 0.0
                return
        # 強勢 ATR TP/SL
        if (is_long and p >= tp) or (not is_long and p <= tp):
            cs = 'sell' if is_long else 'buy'
            tp_pct = abs(tp - avg) / avg * 100
            print(f"🎯 [ATR停利] {sym} {tp_pct:.1f}%")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="ATR停利")
            return
        if (is_long and p <= sl) or (not is_long and p >= sl):
            cs = 'sell' if is_long else 'buy'
            sl_pct = abs(sl - avg) / avg * 100
            print(f"🛑 [ATR停損] {sym} -{sl_pct:.1f}%")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="ATR停損", is_stop_loss=True)
            return

async def check_position_exits(sym):
    s = STATES[sym]
    if s.get("adjusted_this_tick", False):
        return
    if abs(s["qty"]) < 0.000001:
        return
    p = s["close_price"]
    avg = s["avg_price"]
    is_long = s["qty"] > 0
    profit_pct = (p - avg) / max(avg, 1e-8) if is_long else (avg - p) / max(avg, 1e-8)
    hold_sec = time.time() - s["open_time"] if s["open_time"] > 0 else 9999

    if hold_sec < 120:
        return

    # 2% 硬停損
    hard_sl = avg * HARD_STOP_LOSS_PCT
    if (is_long and p <= avg - hard_sl) or (not is_long and p >= avg + hard_sl):
        cs = 'sell' if is_long else 'buy'
        print(f"⛔ [2%硬停損] {sym}")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="2%硬停損", is_stop_loss=True)
        return

    # 3x ATR 停利
    tp_dist = s["current_atr"] * 3.0
    if (is_long and p >= avg + tp_dist) or (not is_long and p <= avg - tp_dist):
        cs = 'sell' if is_long else 'buy'
        print(f"🎯 [ATR停利K線] {sym}")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="ATR停利K線")
        return

    # MACD 反轉搶救 (已在 check_exits 三叉樹中處理，此處保留 10% 硬停損與 3x ATR 停利)
    # 15分鐘時間停損
    if hold_sec > 900 and profit_pct < -0.01:
        cs = 'sell' if is_long else 'buy'
        print(f"⏱️ [時間停損] {sym} {hold_sec/60:.1f}分仍虧損")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="時間停損", is_stop_loss=True)
        return

# ── 進場邏輯 ──────────────────────────────────────────────────

async def execute_order(sym, side, price):
    s = STATES[sym]
    pk = paper_key(sym)
    margin = compute_per_coin_margin()
    if margin <= 0:
        print(f"⚠️ [風控] {sym} 無可用保證金")
        return

    now = time.time()
    if s["entry_count"] > 0:
        if now - s["last_entry_time"] < s["entry_cooldown_sec"]:
            print(f"⏳ [加倉冷卻] {sym} 距離上次加倉不足 {s['entry_cooldown_sec']} 秒")
            return
        if s["entry_count"] >= s["max_additional_entries"]:
            print(f"⚠️ [加倉上限] {sym} 已達最大加倉次數")
            return
        if s["avg_price"] > 0 and price > 0:
            if side == 'buy':
                # 多單攤平：越跌越買。要求價格至少低於均價 0.5% 才允許加倉
                drop_pct = (s["avg_price"] - price) / s["avg_price"]
                if drop_pct < 0.005:
                    print(f"🛑 [攤平風控] {sym} 多單跌幅未達 0.5% (目前: {drop_pct*100:.2f}%)，不急著買")
                    return
            else:
                # 空單攤平：越漲越空。要求價格至少高於均價 0.5% 才允許加倉
                rise_pct = (price - s["avg_price"]) / s["avg_price"]
                if rise_pct < 0.005:
                    print(f"🛑 [攤平風控] {sym} 空單漲幅未達 0.5% (目前: {rise_pct*100:.2f}%)，不急著空")
                    return

    base_amt = margin / price
    if base_amt < 0.001:
        print(f"⚠️ [風控] {sym} 數量過小 {base_amt:.6f}")
        return

    if side == 'buy':
        base_amt *= 0.5 if s["entry_count"] == 0 else 0.25
    else:
        base_amt *= 0.5 if s["entry_count"] == 0 else 0.25

    if PAPER_TRADING:
        try:
            update_paper_state(pk, side, price, base_amt)
            if side == 'buy':
                s["qty"] += base_amt
            else:
                s["qty"] -= base_amt
            if s["avg_price"] <= 0:
                s["avg_price"] = price
            else:
                s["avg_price"] = ((s["avg_price"] * abs(s["qty"] - base_amt)) + (price * base_amt)) / abs(s["qty"])
            s["open_time"] = now
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["entry_count"] += 1
            direction = "做多" if side == 'buy' else "做空"
            print(f"🟢 [{direction}] {sym} {base_amt:.4f} @ {price} (保證金:{margin:.2f} USDT)")
        except Exception as e:
            print(f"🛑 [模擬開倉失敗] {sym}: {e}")
    else:
        try:
            order = await exchange.create_order(sym, type='market', side=side, amount=base_amt,
                                                params={'marginMode': 'isolated'})
            fill_price = float(order.get('price', 0) or price)
            if fill_price <= 0:
                fill_price = price
            
            old_qty = s["qty"]
            if side == 'buy':
                s["qty"] += base_amt
            else:
                s["qty"] -= base_amt
                
            if s["avg_price"] <= 0:
                s["avg_price"] = fill_price
            else:
                s["avg_price"] = ((s["avg_price"] * abs(old_qty)) + (fill_price * base_amt)) / abs(s["qty"])
                
            s["open_time"] = now
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["entry_count"] += 1
        except Exception as e:
            print(f"🚨 [開倉錯誤] {sym}: {e}")

def is_entry_pin_safe(sym, side):
    s = STATES[sym]
    if len(s["ohlcv"]) < 2:
        return True

    candle = s["ohlcv"][-1]
    prev_candle = s["ohlcv"][-2]
    open_price = float(candle[1])
    high = float(candle[2])
    low = float(candle[3])
    close_price = float(candle[4])
    prev_close = float(prev_candle[4])
    body = abs(close_price - open_price)
    upper_wick = high - max(open_price, close_price)
    lower_wick = min(open_price, close_price) - low

    if side == 'buy':
        # 放寬條件：只要求不要收在最低點附近，或者長上影線不要太誇張
        if upper_wick > body * 3.0:
            return False
        return True

    # side == 'sell'
    if lower_wick > body * 3.0:
        return False
    return True


def is_entry_volume_confirmed(sym, side):
    s = STATES[sym]
    if len(s["ohlcv"]) < 2:
        return False
    current_vol = s["current_vol"]
    prev_vol = s["ohlcv"][-2][5]
    vol_ma20 = s["vol_ma20"]
    if vol_ma20 <= 0:
        return False
        
    # 如果「當前 K 線」跟「上一根 K 線」的量都不到均量的 70%，才視為量能不足
    # 這是為了避免在剛換線（前幾秒）時，當前 K 線量能必然偏低而導致誤判
    if current_vol < vol_ma20 * VOLUME_RATIO_THRESHOLD and prev_vol < vol_ma20 * VOLUME_RATIO_THRESHOLD:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能不足] 當前量 {current_vol:.2f} 且上一根量 {prev_vol:.2f} 均 < 均量門檻 {vol_ma20 * VOLUME_RATIO_THRESHOLD:.2f}")
        return False
    return True


def is_entry_allowed(sym, side, route="a"):
    is_trend = route == "a"
    if side == 'buy' and not MARKET_WIND.get("allow_long", True) and is_trend:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤瀑布風控] 大盤異常跌勢，禁止開多")
        return False
    if side == 'sell' and not MARKET_WIND.get("allow_short", True) and is_trend:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤上漲風控] 大盤異常漲勢，禁止開空")
        return False

    s = STATES[sym]
    cp = s["close_price"]
    
    # 均線過濾器：原本所有策略皆受 15m SMA200 限制，現放寬僅限制 Route A (順勢)
    # 讓 Route B/C (搶短反彈) 可以在大勢逆風時抓取極端超賣/超買的反彈機會
    if is_trend and s.get("sma200_15m", 0) > 0:
        ma200 = s["sma200_15m"]
        if side == 'buy' and cp <= ma200:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [MA200大勢保護] 順勢做多，但價格 {cp:.4f} <= MA200 {ma200:.4f}")
            return False
        if side == 'sell' and cp >= ma200:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [MA200大勢保護] 順勢做空，但價格 {cp:.4f} >= MA200 {ma200:.4f}")
            return False

    # 大週期 (HTF) 1H 趨勢過濾器
    # 嚴格要求不能逆著 1H 趨勢做單 (除非是背離 Route S)
    htf_trend = s.get("htf_trend")
    if route != "s" and htf_trend:
        if side == 'buy' and htf_trend == 'short':
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [1H 大週期過濾] 1H 趨勢為空頭，禁止做多")
            return False
        if side == 'sell' and htf_trend == 'long':
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [1H 大週期過濾] 1H 趨勢為多頭，禁止做空")
            return False
            
    # 極端波動率過濾：當市場處於瘋狂洗盤、暴漲暴跌 (ATR > 24h均值兩倍) 時，禁止任何逆勢搶短 (Route B/C)
    if not is_trend:
        atr_history = s.get("atr_history", [])
        atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
        current_atr = s.get("current_atr", 0.0)
        
        if atr_24h_avg > 0 and current_atr > atr_24h_avg * 2.0:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [極端波動率保護] 目前 ATR {current_atr:.5f} > 均值兩倍 {atr_24h_avg*2.0:.5f} (禁止逆勢接刀/摸頭)")
            return False
            
    if len(s["ohlcv"]) < 20:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線不足] 當前長度 {len(s['ohlcv'])} < 20")
        return False
    if not is_entry_pin_safe(sym, side):
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 反向長影線/方向未確認")
        return False
        
    # 量能確認過濾器
    if not is_entry_volume_confirmed(sym, side):
        return False
        
    # ADX 趨勢強度限制
    highs = np.array([x[2] for x in s["ohlcv"]])
    lows = np.array([x[3] for x in s["ohlcv"]])
    closes = np.array([x[4] for x in s["ohlcv"]])
    adx_val = calculate_adx(highs, lows, closes)
    if adx_val < 12:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [ADX過濾] 趨勢強度 ADX {adx_val:.1f} < 12")
        return False

    # 實盤最小量限制
    min_volume = max(1000.0, s["vol_ma20"] * 0.1)
    if s["current_vol"] < min_volume:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [實盤最小量過濾]")
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
    if len(s["closes"]) < 20:
        return (None, 0)

    rsi = s["current_rsi"]
    rsis = s.get("rsis", [])
    if len(rsis) == 0:
        rsis = [rsi]
        
    close = s["close_price"]
    prev_close = s["prev_close"] if s["prev_close"] is not None else close
    ema20 = s.get("ema20", 0.0)
    ema50 = s.get("ema50", 0.0)

    trend_long = ema20 > 0 and close > ema20
    trend_short = ema20 > 0 and close < ema20

    # Define parameters for dynamic RSI thresholds
    LONG_RSI_NORMAL = 40.0
    SHORT_RSI_NORMAL = 60.0
    LONG_RSI_HIGH_VOL = 35.0
    SHORT_RSI_HIGH_VOL = 65.0

    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)

    if current_atr > atr_24h_avg and atr_24h_avg > 0:
        long_rsi_threshold = LONG_RSI_HIGH_VOL
        short_rsi_threshold = SHORT_RSI_HIGH_VOL
        vol_mode = "高波動模式 (High Vol)"
    else:
        # 在低波動模式下，縮緊門檻，只做最穩定的訊號
        long_rsi_threshold = 25.0
        short_rsi_threshold = 75.0
        vol_mode = "低波動模式 (Low Vol)"

    # 每個循環輸出當前指標數值，方便追蹤與除錯
    print(f"@@COIN_DEBUG@@ 🔍 {sym} | RSI: {rsi:.1f} | Price: {close:.4f} (BB: {s.get('bb_low', 0):.4f} - {s.get('bb_up', 0):.4f}) | MACD: {s.get('macd_line', 0):.4f}/{s.get('macd_signal', 0):.4f} | Trend (L/S): {trend_long}/{trend_short} | VolMode: {vol_mode} (ATR: {current_atr:.5f} / 24h Avg: {atr_24h_avg:.5f})")
    
    is_in_bb_zone_long = close <= s.get("bb_low", 0) * 1.005
    is_in_bb_zone_short = close >= s.get("bb_up", 0) * 0.995
    
    macd_line = s.get("macd_line", 0.0)
    macd_signal = s.get("macd_signal", 0.0)
    prev_macd_line = s.get("prev_macd_line", 0.0)
    prev_macd_signal = s.get("prev_macd_signal", 0.0)
    
    macd_hist = macd_line - macd_signal
    prev_macd_hist = prev_macd_line - prev_macd_signal
    
    long_macd_cross = prev_macd_line <= prev_macd_signal and macd_line > macd_signal
    short_macd_cross = prev_macd_line >= prev_macd_signal and macd_line < macd_signal
    
    long_macd_hist_aligned = macd_hist > prev_macd_hist
    short_macd_hist_aligned = macd_hist < prev_macd_hist
    
    long_macd_ok = long_macd_cross or long_macd_hist_aligned
    short_macd_ok = short_macd_cross or short_macd_hist_aligned

    # 放寬收盤價確認：只要不低於前一根最低點就算確認
    last_candle_confirmed_long = len(s["ohlcv"]) >= 2 and close > s["ohlcv"][-2][3]
    last_candle_confirmed_short = len(s["ohlcv"]) >= 2 and close < s["ohlcv"][-2][2]

    print(f"@@COIN_DEBUG@@ 🔍 {sym} 條件檢測 | RSI門檻(L/S: {long_rsi_threshold:.0f}/{short_rsi_threshold:.0f}): {rsi < long_rsi_threshold}/{rsi > short_rsi_threshold} | BB區間(L/S): {is_in_bb_zone_long}/{is_in_bb_zone_short} | MACD滿足(L/S): {long_macd_ok}/{short_macd_ok} (交叉:{long_macd_cross}/{short_macd_cross}, 柱狀體向上/下:{long_macd_hist_aligned}/{short_macd_hist_aligned}) | 收盤價確認(L/S): {last_candle_confirmed_long}/{last_candle_confirmed_short}")

    ema50 = s.get("ema50", 0.0)
    trend_confluence_long = ema50 == 0.0 or close > ema50
    trend_confluence_short = ema50 == 0.0 or close < ema50

    is_above_sma200 = s.get("sma200_15m", 0) > 0 and close > s.get("sma200_15m", 0) * 0.999
    is_below_sma200 = s.get("sma200_15m", 0) > 0 and close < s.get("sma200_15m", 0) * 1.001

    # Route A (Trend Following): 站上 SMA200 AND MACD黃金交叉 AND K線方向確認
    route_a_long = is_above_sma200 and long_macd_cross and last_candle_confirmed_long
    route_a_short = is_below_sma200 and short_macd_cross and last_candle_confirmed_short

    # Route B (Mean Reversion): RSI極度超賣 AND 價格低於布林下軌 AND K線方向確認
    route_b_long = rsi < long_rsi_threshold and is_in_bb_zone_long and last_candle_confirmed_long
    route_b_short = rsi > short_rsi_threshold and is_in_bb_zone_short and last_candle_confirmed_short

    # Route C (Counter-Trend Scalp / 反轉搶短機制): RSI超賣/超買 (35/65)，無視 EMA 趨勢濾網，允許開倉搶反彈
    route_c_long = rsi < 35.0 and last_candle_confirmed_long
    route_c_short = rsi > 65.0 and last_candle_confirmed_short

    # Route S (Divergence / 背離確認機制): 最強反轉訊號，無視 1H 大週期過濾
    bullish_div, bearish_div = check_rsi_divergence(s["closes"], s.get("rsis", []), window=60)
    route_s_long = bullish_div and last_candle_confirmed_long
    route_s_short = bearish_div and last_candle_confirmed_short
    if route_s_long:
        print(f"🌟 [背離確認] {sym} 出現看漲背離 (Bullish Divergence)!")
    if route_s_short:
        print(f"🌟 [背離確認] {sym} 出現看跌背離 (Bearish Divergence)!")

    long_base_ok = route_s_long or route_a_long or route_b_long or route_c_long
    short_base_ok = route_s_short or route_a_short or route_b_short or route_c_short

    if long_base_ok:
        route = "s" if route_s_long else "c" if route_c_long else "b" if route_b_long else "a"
        if route == "a":
            strength = 5.0 + ((close - ema20) / max(ema20, 1e-8) * 100)
        else:
            strength = max(0.0, long_rsi_threshold - rsi) + ((ema20 - close) / max(ema20, 1e-8) * 100) + 5.0
            
        if long_macd_cross:
            strength += 5.0
        if route == "a":
            strength += 10.0  # Extra score for trend confluence
        return ("buy", strength if strength >= 8.0 else 0.0, route)

    if short_base_ok:
        route = "s" if route_s_short else "c" if route_c_short else "b" if route_b_short else "a"
        if route == "a":
            strength = 5.0 + ((ema20 - close) / max(ema20, 1e-8) * 100)
        else:
            strength = max(0.0, rsi - short_rsi_threshold) + ((close - ema20) / max(ema20, 1e-8) * 100) + 5.0
            
        if short_macd_cross:
            strength += 5.0
        if route == "a":
            strength += 10.0  # Extra score for trend confluence
        return ("sell", strength if strength >= 8.0 else 0.0, route)

    return (None, 0, None)

async def check_entries():
    open_count = get_open_position_count()
    if open_count >= MAX_POSITIONS:
        return
    remaining_slots = MAX_POSITIONS - open_count

    candidates = []
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s["status"] != "ACTIVE":
            continue
        if abs(s["qty"]) > 0.000001:
            continue
        side_strength = compute_signal_strength(sym)
        if side_strength[0] is None:
            continue
        side, strength, route = side_strength
        if strength <= 0.0:
            continue
        if not is_entry_allowed(sym, side, route):
            continue
        candidates.append((sym, side, strength, route))

    if not candidates:
        return

    candidates.sort(key=lambda x: -x[2])
    print(f"📊 [訊號排行] {' | '.join(f'{sym}:{side}({strength:.2f})' for sym, side, strength, _ in candidates[:3])}")

    for i in range(min(remaining_slots, len(candidates))):
        sym, side, _, route = candidates[i]
        s = STATES[sym]
        now = time.time()
        
        # Route C/S 反轉搶短或背離直接開倉，不進行二次確認與突破等待
        if route in ["c", "s"]:
            print(f"⚡ [即時開倉] {sym} 觸發反轉搶短/背離，繞過二次確認即刻下單！")
            await execute_order(sym, side, s["close_price"])
            s["pending_side"] = None
            s["pending_confirm_high"] = 0
            s["pending_confirm_low"] = 0
            continue

        if s["pending_side"] != side:
            s["pending_side"] = side
            s["pending_time"] = now
            m_line, m_sig, _, _, _ = calculate_macd(s["closes"])
            macd_triggered = (s["prev_macd_line"] <= s["prev_macd_signal"] and m_line > m_sig) or \
                             (s["prev_macd_line"] >= s["prev_macd_signal"] and m_line < m_sig)
            if macd_triggered and len(s["ohlcv"]) >= 2:
                s["pending_confirm_high"] = s["ohlcv"][-2][2]
                s["pending_confirm_low"] = s["ohlcv"][-2][3]
            continue
        candle_range = max(1e-8, s["pending_confirm_high"] - s["pending_confirm_low"])
        required_breakout = candle_range * 0.07
        confirm_delay = 5.0 if s.get("current_atr", 0.0) > s.get("atr_ma20", 0.0) else PENDING_CONFIRM_SEC

        if now - s["pending_time"] >= confirm_delay:
            if side == 'buy' and s["pending_confirm_high"] > 0 and s["close_price"] <= (s["pending_confirm_high"] + required_breakout):
                continue
            if side == 'sell' and s["pending_confirm_low"] > 0 and s["close_price"] >= (s["pending_confirm_low"] - required_breakout):
                continue
            await execute_order(sym, side, s["close_price"])
            s["pending_side"] = None
            s["pending_confirm_high"] = 0
            s["pending_confirm_low"] = 0

# ── 主循環 ──────────────────────────────────────────────────

async def watch_symbol_trades(sym):
    while True:
        try:
            trades = await exchange.watch_trades(sym)
            if trades:
                data = trades if isinstance(trades, list) else [trades]
                for trade in data:
                    update_trade_signal(sym, trade)
        except Exception as e:
            print(f"⚠️ [成交流監聽異常] {sym}: {e}，5秒後重試")
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
    print(f"📊 最大同時持倉: {MAX_POSITIONS}")
    print(f"📡 模式: {'模擬' if PAPER_TRADING else '實盤'}")
    print(f"@@LEVERAGE@@{LEVERAGE}")
    try:
        await asyncio.wait_for(exchange.load_markets(), timeout=15)
    except Exception as e:
        print(f"⚠️ load_markets 失敗 ({e})，使用預設市場清單")
    
    global ALL_SYMBOLS
    ALL_SYMBOLS = filter_valid_symbols(ALL_SYMBOLS)
    save_symbol_pool(ALL_SYMBOLS)
    
    print(f"📋 監控幣種: {', '.join(ALL_SYMBOLS)}")
    try:
        await asyncio.wait_for(initialize_atr_history(), timeout=30)
    except (asyncio.TimeoutError, Exception) as e:
        print(f"⏳ [初始化] ATR 歷史預熱超時或失敗 ({e})，將在運行中慢慢加熱")
    await fetch_real_balance()
    await load_open_positions()
    await fetch_all_sma200()
    await fetch_all_htf_trend()

    last_balance_update = time.time()
    last_htf_update = time.time()

    while True:
        try:
            loop_start = time.time()
            if not PAPER_TRADING and loop_start - last_balance_update > 30:
                await fetch_real_balance()
                last_balance_update = loop_start
                
            if loop_start - last_htf_update > 1800:  # 每 30 分鐘更新一次大週期
                await fetch_all_htf_trend()
                last_htf_update = loop_start

            for sym in ALL_SYMBOLS:
                STATES[sym]["adjusted_this_tick"] = False
            if ALL_SYMBOLS != load_symbol_pool():
                apply_symbol_pool_change(load_symbol_pool())
            await ensure_watch_tasks()
            await update_market_wind()
            await fetch_all_klines()
            for sym in ALL_SYMBOLS:
                compute_indicators(sym)
            update_states()
            for sym in ALL_SYMBOLS:
                await check_exits(sym)
            for sym in ALL_SYMBOLS:
                await check_position_exits(sym)
            await check_entries()
            
            # 成功執行，重置連續錯誤計數器
            global CONSECUTIVE_ERRORS
            CONSECUTIVE_ERRORS = 0
            
            # 權重節流檢測
            weight_sleep = check_binance_weight()
            
            elapsed = time.time() - loop_start
            sleep_time = max(1.5, MAIN_LOOP_INTERVAL_SEC - elapsed) + weight_sleep
            await asyncio.sleep(sleep_time)
        except ccxt.DDoSProtection as e:
            print(f"🚨 [API限流 429] 檢測到 DDoSProtection 限流，冷卻 10 秒: {e}")
            await asyncio.sleep(10)
        except ccxt.RateLimitExceeded as e:
            print(f"🚨 [API限流 429] 檢測到 RateLimitExceeded 限流，冷卻 10 秒: {e}")
            await asyncio.sleep(10)
        except Exception as e:
            if "429" in str(e):
                print(f"🚨 [API限流 429] 檢測到 429 錯誤，冷卻 10 秒: {e}")
                await asyncio.sleep(10)
                continue
            import traceback
            CONSECUTIVE_ERRORS += 1
            print(f"❌ [主循環錯誤] 當前連續錯誤數: {CONSECUTIVE_ERRORS} | 錯誤: {e}")
            traceback.print_exc()
            
            # 連續錯誤防爆防封禁冷卻機制
            if CONSECUTIVE_ERRORS >= 3:
                cooldown = min(120, 15 * (CONSECUTIVE_ERRORS - 2))
                print(f"🚨 [連續API錯誤風控] 已連續錯誤 {CONSECUTIVE_ERRORS} 次，觸發風控冷卻，暫停 {cooldown} 秒...")
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
        active = sum(1 for s in STATES.values() if s["status"] == "ACTIVE")
        cooldown = sum(1 for s in STATES.values() if s["status"] == "COOLDOWN")
        banned = sum(1 for s in STATES.values() if s["status"] == "BANNED")
        open_syms = get_open_symbols()
        open_str = ', '.join(f"{sym}({'多' if STATES[sym]['qty']>0 else '空'})" for sym in open_syms) if open_syms else "無"
        print(f"📊 [狀態] ACTIVE={active} COOLDOWN={cooldown} BANNED={banned} | 持倉({len(open_syms)}): {open_str}")

async def main():
    await asyncio.gather(
        main_loop(),
        periodic_sma200_update(),
        periodic_status_log(),
        watch_all_trades(),
    )

if __name__ == "__main__":
    asyncio.run(main())
