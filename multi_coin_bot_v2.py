import asyncio
import ccxt
import ccxt.pro as ccxtpro
import numpy as np
import json
import os
from dotenv import load_dotenv
load_dotenv()

import time
import datetime
import copy
import aiohttp
from ai_signal import build_ai_context, fetch_ai_signals, AI_UPDATE_INTERVAL


class dotdict(dict):
    """dot.notation access to dictionary attributes"""
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

import logging

# Setup structured logging
logger = logging.getLogger("multi_coin_bot")
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
ch.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(ch)


from services.utils import paper_key
from update_paper_state import update_paper_state

exchange = ccxtpro.binance({
    'apiKey': os.getenv('BINANCE_API_KEY') or None,
    'secret': os.getenv('BINANCE_API_SECRET') or None,
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
FORCE_AI_SCAN = True  # 測試階段開啟，強制抓取高成交量幣種給 AI
EXECUTING_SYMBOLS = set()

if USE_TESTNET:
    exchange.urls['api']['fapiPublic'] = 'https://testnet.binancefuture.com/fapi/v1'
    exchange.urls['api']['fapiPrivate'] = 'https://testnet.binancefuture.com/fapi/v1'

DEFAULT_SYMBOLS = [
    # 錨定幣種 (Anchor)
    "BTCUSDT", "ETHUSDT",
    # 趨勢跟隨者 (Trend Follower)
    "SOLUSDT", "SUIUSDT", "AVAXUSDT", "FETUSDT", "LINKUSDT", "NEARUSDT", "INJUSDT",
    # 高波動幣種 (High Volatility)
    "WIFUSDT", "1000PEPEUSDT", "ORDIUSDT"
]
SLOW_OR_LOW_QUALITY_SYMBOLS = {
    "AERO", "ADA", "DOT", "UNI", "FET",
    "STG",   # 流動性差，容易被插針造成 -6%+ 損失
    "SEI",   # 低流動性，訊號容易被 3 分鐘守護線洗掉
}
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "bot_symbols.json")

# ═══════════════════════════════════════════
# 開倉條件設定 (動態熱更新)
# ═══════════════════════════════════════════
STRATEGY_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "strategy_config.json")

DEFAULT_STRATEGY_CONF = {
    "AI_FRESHNESS_MULTIPLIER": 4,
    "AI_CONF_CHOP": 0.55,
    "AI_CONF_TREND": 0.35,
    "FAST_PATH_REQUIRE_4H": False,
    "FAST_PATH_SCORE_LOW_VOL": 14.0,
    "FAST_PATH_SCORE_HIGH_VOL": 17.0,
    "ENTRY_SCORE_MIN": 6.0,
    "AI_TRIGGER_SCORE_LOW_VOL": 5.0,
    "AI_TRIGGER_SCORE_HIGH_VOL": 6.0,
    "ENTRY_COOLDOWN_GLOBAL_SEC": 120,
    "ENTRY_COOLDOWN_SAME_SIDE_SEC": 600,
    "ADX_MIN_THRESHOLD": 5.0,
    "VOLUME_CONFIRM_RATIO": 0.6,
    "ATR_SPIKE_MULTIPLIER": 1.5
}

STRATEGY_CONF = DEFAULT_STRATEGY_CONF.copy()

def load_strategy_config():
    global STRATEGY_CONF
    try:
        if not os.path.exists(STRATEGY_CONFIG_FILE):
            with open(STRATEGY_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_STRATEGY_CONF, f, indent=4)
        with open(STRATEGY_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Update with fallback to default
            for k in DEFAULT_STRATEGY_CONF:
                if k in data:
                    STRATEGY_CONF[k] = data[k]
    except Exception as e:
        logger.error(f"載入策略設定檔失敗: {e}，使用記憶體內設定")



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

# 動態計算最大持倉數
def get_max_positions(balance: float) -> int:
    if balance < 500:
        return 2
    else:
        return 3

COOLDOWN_SEC = 180
MAIN_LOOP_INTERVAL_SEC = 3
PENDING_CONFIRM_SEC = 1.0
BAN_WINDOW = 3600
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 3
SL_ATR_MULTIPLIER = 1.5
TP_ATR_MULTIPLIER = 1.8
HARD_STOP_LOSS_PCT = 0.005  # 硬停損閾值 -0.5%

HIGH_VOLATILITY_COINS = {"WIFUSDT", "1000PEPEUSDT", "ORDIUSDT"}
ANCHOR_COINS = {"BTCUSDT", "ETHUSDT"}
TREND_FOLLOWER_COINS = {"SOLUSDT", "SUIUSDT", "AVAXUSDT", "FETUSDT", "LINKUSDT", "NEARUSDT", "INJUSDT"}

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
        "avg_vol_24h_1m": 0.0,
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
        "stop_times": [],
        "adjusted_this_tick": False,
        "htf_trend": None,
        "htf_ema20": 0.0,
        "htf_4h_trend": None,
        "htf_4h_ema20": 0.0,
        "rsis": [],
        "sma200_15m": 0.0,
        "max_strength": 0.0,
        "entry_route": "a",
        "last_stop_loss_side": "",
        "last_stop_loss_price": 0.0,
        "last_stop_loss_time": 0.0,
        "last_exit_time": 0.0,
        "last_exit_type": "normal",
        "ai_bias": None,        # "long" / "short" / "neutral" / None
        "ai_confidence": 0.0,
        "ai_reason": "",
        "ai_updated_at": 0.0,
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
    ema = np.zeros_like(prices, dtype=float)
    ema[0] = prices[0]
    multiplier = 2 / (period + 1)
    for i in range(1, len(prices)):
        ema[i] = (prices[i] - ema[i-1]) * multiplier + ema[i-1]
    return ema[-1]

def calculate_ema_array(prices, period=14):
    if len(prices) == 0: return np.array([])
    ema = np.zeros_like(prices, dtype=float)
    ema[0] = prices[0]
    multiplier = 2 / (period + 1)
    for i in range(1, len(prices)):
        ema[i] = (prices[i] - ema[i-1]) * multiplier + ema[i-1]
    return ema

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
    prices_arr = np.array(prices, dtype=float)
    ema_fast = calculate_ema_array(prices_arr, fast)
    ema_slow = calculate_ema_array(prices_arr, slow)
    
    macd_line_arr = ema_fast - ema_slow
    macd_signal_arr = calculate_ema_array(macd_line_arr, signal)
    
    macd_line = macd_line_arr[-1]
    prev_macd_line = macd_line_arr[-2] if len(macd_line_arr) >= 2 else macd_line
    macd_signal = macd_signal_arr[-1]
    prev_macd_signal = macd_signal_arr[-2] if len(macd_signal_arr) >= 2 else macd_signal
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
        desired = list(DEFAULT_SYMBOLS)

    new_symbols = []
    used = set()
    # 確保不會被之前的長度限制住，一律至少可以容納 DEFAULT_SYMBOLS 的長度
    target_count = min(20, max(len(desired), len(DEFAULT_SYMBOLS)))

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
    max_pos = get_max_positions(balance)
    open_count = get_open_position_count()
    remaining_slots = max_pos - open_count
    if remaining_slots <= 0:
        return 0
    usable = balance * 0.95
    per_slot = usable / max_pos
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

def mark_exit(sym, is_stop_loss=False, is_profit=False):
    s = STATES[sym]
    now = time.time()

    if is_profit:
        # 停利：冷卻縮短到 60 秒，不累積停損計數
        s.status = "COOLDOWN"
        s.next_status_time = now + 60
        s.status_reason = "停利冷卻 (1分鐘)"
        logger.info(f"✅ [狀態] {sym} 停利 → COOLDOWN 1分鐘")
        return

    if is_stop_loss:
        # 停損：維持原本 180 秒 + 累積計數
        s.status = "COOLDOWN"
        s.next_status_time = now + COOLDOWN_SEC
        s.status_reason = "停損冷卻 (3分鐘)"
        if not isinstance(getattr(s, 'stop_times', None), list):
            s.stop_times = []
        s.stop_times.append(now)
        s.stop_times = [t for t in s.stop_times if now - t <= BAN_WINDOW]
        s.stop_count = len(s.stop_times)
        max_stops = 2 if sym in HIGH_VOLATILITY_COINS else MAX_STOPS_IN_WINDOW
        if s.stop_count >= max_stops:
            s.status = "BANNED"
            s.next_status_time = now + BAN_DURATION
            s.status_reason = f"封禁中 (24h，{max_stops}次停損)"
            logger.warning(f"🚫 [狀態] {sym} {max_stops}次停損 → BANNED 24h")
    else:
        # 一般平倉（時間停損、趨勢翻轉等）：90 秒
        s.status = "COOLDOWN"
        s.next_status_time = now + 90
        s.status_reason = "平倉冷卻 (90秒)"

def reset_coin_state(sym):
    s = STATES.get(sym)
    preserved_exit_time = s.last_exit_time if s else 0.0
    preserved_exit_type = getattr(s, "last_exit_type", "normal") if s else "normal"
    
    STATES[sym] = build_symbol_state(sym)
    s = STATES[sym]
    s.last_exit_time = preserved_exit_time
    s.last_exit_type = preserved_exit_type
    s.qty = 0.0
    s.avg_price = 0.0
    s.open_time = 0.0
    s.trailing_highest = 0.0
    s.trailing_lowest = float('inf')
    s.highest_profit_pct = 0.0
    s.pnl_history = []
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
    s.avg_vol_24h_1m = 0.0
    s.is_breakeven_set = False
    s.max_strength = 0.0

# ── 大盤與風向監控 (BTC & ETH Filter) ─────────────────────────

MARKET_WIND = {
    "btc_trend": "NEUTRAL",  # "BULL" or "BEAR"
    "allow_long": True,
    "allow_short": True,
    "btc_change_15m": 0.0,
    "eth_change_15m": 0.0,
    "fng_value": 50,         # Fear and Greed Index value (0-100)
    "market_regime": "NORMAL_CHOP" # "RAGING_BULL", "PANIC_BEAR", "NORMAL_CHOP"
}

async def update_market_wind():
    global MARKET_WIND
    try:
        # 抓取 BTC 和 ETH
        btc_ohlcv = await asyncio.wait_for(exchange.fetch_ohlcv("BTCUSDT", TIMEFRAME, limit=100), timeout=10)
        eth_ohlcv = await asyncio.wait_for(exchange.fetch_ohlcv("ETHUSDT", TIMEFRAME, limit=100), timeout=10)
        
        MARKET_WIND["allow_long"] = True
        MARKET_WIND["allow_short"] = True
        
        if len(btc_ohlcv) >= 20:
            btc_closes = np.array([x[4] for x in btc_ohlcv])
            btc_ema20 = calculate_ema(btc_closes, 20)
            btc_price = btc_closes[-1]
            btc_change_15m = (btc_price - btc_closes[-15]) / btc_closes[-15]
            
            MARKET_WIND["btc_trend"] = "BULL" if btc_price > btc_ema20 else "BEAR"
            MARKET_WIND["btc_change_15m"] = btc_change_15m
            
            if len(btc_ohlcv) >= 60:
                btc_1h_closes = np.array([x[4] for x in btc_ohlcv[-60:]])
                btc_1h_trend = "bull" if btc_closes[-1] > np.mean(btc_1h_closes) else "bear"
                MARKET_WIND["btc_1h_trend"] = btc_1h_trend
        else:
            btc_change_15m = 0.0
            
        if len(eth_ohlcv) >= 20:
            eth_closes = np.array([x[4] for x in eth_ohlcv])
            eth_price = eth_closes[-1]
            eth_change_15m = (eth_price - eth_closes[-15]) / eth_closes[-15]
            MARKET_WIND["eth_change_15m"] = eth_change_15m
        else:
            eth_change_15m = 0.0
            
        # 1. 瀑布防護 (15m內跌超過0.6%暫停多單，漲超過1.2%暫停空單)
        if btc_change_15m < -0.006 or eth_change_15m < -0.008:
            MARKET_WIND["allow_long"] = False
            logger.warning(f"⚠️ [大盤瀑布風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣多單開倉！")
        elif btc_change_15m > 0.012 or eth_change_15m > 0.015:
            MARKET_WIND["allow_short"] = False
            logger.warning(f"⚠️ [大盤暴漲風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣空單開倉！")
            
        # 2. 決定 Market Regime (大盤狀態)
        if 'btc_closes' in locals() and len(btc_closes) >= 50:
            btc_ema50 = calculate_ema(btc_closes, 50)
            fng = MARKET_WIND.get("fng_value", 50)
            if btc_price > btc_ema50 and fng >= 70:
                MARKET_WIND["market_regime"] = "RAGING_BULL"
            elif btc_price < btc_ema50 and fng <= 30:
                MARKET_WIND["market_regime"] = "PANIC_BEAR"
            else:
                MARKET_WIND["market_regime"] = "NORMAL_CHOP"
            
    except Exception as e:
        logger.warning(f"⚠️ [更新大盤風向失敗]: {e}")

async def update_fear_and_greed_index():
    global MARKET_WIND
    while True:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get("https://api.alternative.me/fng/?limit=1") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        val = int(data['data'][0]['value'])
                        MARKET_WIND["fng_value"] = val
                        logger.info(f"🧠 [情緒指標] 當前恐懼與貪婪指數: {val} | 大盤狀態: {MARKET_WIND['market_regime']}")
        except Exception as e:
            logger.warning(f"⚠️ [FNG API 失敗]: {e}")
        await asyncio.sleep(3600)  # 每小時更新一次

async def update_24h_volume():
    """定期抓取 24小時 Ticker 以計算平均每分鐘成交量 (能量過濾基準)"""
    while True:
        try:
            tickers = await asyncio.wait_for(exchange.fetch_tickers(ALL_SYMBOLS), timeout=15)
            for sym, ticker in tickers.items():
                if sym in STATES:
                    # Binance 的 baseVolume 是 24h 內的基礎幣種總成交量
                    vol_24h = float(ticker.get('baseVolume', 0.0))
                    STATES[sym].avg_vol_24h_1m = vol_24h / 1440.0
            logger.info("📊 [背景更新] 24小時平均成交量更新完成")
        except Exception as e:
            logger.warning(f"⚠️ [Ticker API 失敗]: {e}")
        await asyncio.sleep(300)  # 每 5 分鐘更新一次


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
    for sym in ALL_SYMBOLS:
        try:
            res = await asyncio.wait_for(exchange.fetch_ohlcv(sym, TIMEFRAME, limit=100), timeout=10)
            STATES[sym].ohlcv = res
            STATES[sym].close_price = res[-1][4]
        except Exception as e:
            logger.warning(f"⚠️ [K線獲取失敗] {sym}: {e}")
        await asyncio.sleep(0.1)

async def fetch_sma200_15m(sym):
    try:
        ohlcv = await asyncio.wait_for(exchange.fetch_ohlcv(sym, '15m', limit=200), timeout=10)
        closes = np.array([x[4] for x in ohlcv])
        return float(np.mean(closes))
    except Exception as e:
        logger.warning(f"⚠️ [SMA200獲取失敗] {sym}: {e}")
        return 0.0

async def fetch_all_sma200():
    for sym in ALL_SYMBOLS:
        res = await fetch_sma200_15m(sym)
        STATES[sym].sma200_15m = res
        await asyncio.sleep(0.1)

async def fetch_htf_trend(sym):
    try:
        ohlcv = await asyncio.wait_for(exchange.fetch_ohlcv(sym, '1h', limit=50), timeout=10)
        closes = np.array([float(x[4]) for x in ohlcv])
        ema20 = calculate_ema(closes, 20)
        current_close = closes[-1]
        trend = "long" if current_close > ema20 else "short"
        return trend, ema20
    except Exception as e:
        logger.warning(f"⚠️ [HTF獲取失敗] {sym}: {e}")
        return None, 0.0

async def fetch_all_htf_trend():
    for sym in ALL_SYMBOLS:
        trend, ema20 = await fetch_htf_trend(sym)
        if trend:
            STATES[sym].htf_trend = trend
            STATES[sym].htf_ema20 = ema20
        await asyncio.sleep(0.1)

async def fetch_htf_4h_trend(sym):
    try:
        ohlcv = await asyncio.wait_for(exchange.fetch_ohlcv(sym, '4h', limit=50), timeout=10)
        closes = np.array([float(x[4]) for x in ohlcv])
        ema20 = calculate_ema(closes, 20)
        current_close = closes[-1]
        trend = "long" if current_close > ema20 else "short"
        return trend, ema20
    except Exception as e:
        logger.warning(f"⚠️ [4H HTF獲取失敗] {sym}: {e}")
        return None, 0.0

async def fetch_all_htf_4h_trend():
    for sym in ALL_SYMBOLS:
        trend, ema20 = await fetch_htf_4h_trend(sym)
        if trend:
            STATES[sym].htf_4h_trend = trend
            STATES[sym].htf_4h_ema20 = ema20
        await asyncio.sleep(0.1)

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




async def close_position(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False, is_profit=False):
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
        mark_exit(sym, is_stop_loss=is_stop_loss, is_profit=is_profit)
        
        # 保存同向冷卻狀態
        old_side = 'buy' if s.qty > 0 else 'sell'
        ls_side = old_side if is_stop_loss else s.last_stop_loss_side
        ls_time = time.time() if is_stop_loss else s.last_stop_loss_time
        ls_price = price if is_stop_loss else getattr(s, 'last_stop_loss_price', 0.0)
        
        reset_coin_state(sym)
        STATES[sym].last_stop_loss_side = ls_side
        STATES[sym].last_stop_loss_time = ls_time
        STATES[sym].last_stop_loss_price = ls_price
        STATES[sym].last_exit_time = time.time()
        STATES[sym].last_exit_type = "stop_loss" if is_stop_loss else ("profit" if is_profit else "normal")
        
        # 更新全局風控與績效狀態
        current_day = time.strftime("%Y-%m-%d", time.gmtime())
        if GLOBAL_STATE["last_reset_day"] != current_day:
            GLOBAL_STATE["daily_pnl"] = 0.0
            GLOBAL_STATE["last_reset_day"] = current_day
            GLOBAL_STATE["trading_enabled"] = True

        GLOBAL_STATE["daily_pnl"] += pnl
        
        # 計算實際 % 獲利 (用 pnl / 總成本)
        cost = avg_price * abs(qty)
        pct = (pnl / cost) if cost > 0 else 0

        route = s.entry_route
        if route in GLOBAL_STATE["route_stats"]:
            if pnl > 0:
                GLOBAL_STATE["route_stats"][route]["win"] += 1
                if pct > 0.005:
                    GLOBAL_STATE["consecutive_losses"] = 0
                else:
                    logger.info(f"⚠️ {sym} 獲利 {pct*100:.2f}% 太小 (<0.5%)，不足以抵銷連敗紀錄 (目前 {GLOBAL_STATE['consecutive_losses']} 連敗)")
            elif pnl < 0:
                GLOBAL_STATE["route_stats"][route]["loss"] += 1
                GLOBAL_STATE["consecutive_losses"] += 1

        # 動態每日停損：帳戶餘額的 5%，下限 $10，上限 $100
        current_balance = get_balance()
        MAX_DAILY_LOSS_USDT = max(10.0, min(100.0, current_balance * 0.05))
        if GLOBAL_STATE["daily_pnl"] <= -MAX_DAILY_LOSS_USDT:
            if GLOBAL_STATE["trading_enabled"]:
                logger.error(f"🚨 [全局風控] 當日累積虧損已達 {GLOBAL_STATE['daily_pnl']:.2f} USDT，超過每日最大虧損限制 ({MAX_DAILY_LOSS_USDT:.1f} USDT = 餘額5%)！關閉今日所有新開倉。")
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

    # ── 快速方向確認機制 (開倉後 90 秒內緊縮停損，預防方向判斷錯誤) ──
    # 原理：若方向正確，90 秒內通常不會跌超過 0.5%；若已虧損 0.5% 代表方向大概率錯了
    # 此機制不影響入場條件，只加快「方向錯誤」時的出場速度，減少損失
    QUICK_SL_WINDOW_SEC = 90
    QUICK_SL_PCT = 0.003  # -0.3% (硬停損已降至-0.5%，快速確認需在更早觸發)
    if hold_sec < QUICK_SL_WINDOW_SEC and profit_pct <= -QUICK_SL_PCT:
        cs = 'sell' if is_long else 'buy'
        logger.warning(
            f"⚡ [快速方向確認] {sym} 開倉後 {hold_sec:.0f}s，"
            f"虧損已達 {profit_pct*100:.2f}% (< -{QUICK_SL_PCT*100:.1f}%)，"
            f"方向可能錯誤，快速離場！"
        )
        await close_position(sym, cs, abs(s.qty), p, avg, reason="快速方向確認停損", is_stop_loss=True)
        return

    # 0. 強制保本機制 (Breakeven Protection)
    if profit_pct >= 0.008:
        s.is_breakeven_set = True

    if s.is_breakeven_set and profit_pct <= 0.002:
        cs = 'sell' if is_long else 'buy'
        print(f"🔒 [強制保本] {sym} 觸發保本機制，防止利潤回吐！")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="保本防護出場")
        return

    # 4. 固定底線停損 (Hard Stop Loss -0.8%)
    # 0. AI 強制平倉指令 (模式 C)
    ai_action = getattr(s, "ai_action", "HOLD")
    ai_updated_at = getattr(s, "ai_updated_at", 0.0)
    ai_reason = getattr(s, "ai_reason", "")
    ai_fresh = (time.time() - ai_updated_at) < AI_UPDATE_INTERVAL * 2
    
    if ai_fresh and ai_action == "CLOSE":
        cs = 'sell' if is_long else 'buy'
        logger.warning(f"🤖 [AI 總裁下令平倉] {sym} 獲利 {profit_pct*100:.2f}% | 理由: {ai_reason}")
        s.highest_profit_pct = 0.0
        # 重設 ai_action 避免重複觸發
        s.ai_action = "HOLD"
        await close_position(sym, cs, abs(s.qty), p, avg, reason=f"AI下令: {ai_reason[:15]}...", is_stop_loss=(profit_pct<0))
        return

    if profit_pct <= -0.02:
        cs = 'sell' if is_long else 'buy'
        logger.warning(f"💀 [固定底線停損] {sym} 虧損達 {profit_pct*100:.2f}% (<= -2.0%)，無條件斬倉！")
        s.highest_profit_pct = 0.0
        await close_position(sym, cs, abs(s.qty), p, avg, reason="固定底線停損-2.0%", is_stop_loss=True)
        return

    # 5. 動態 ATR 硬停損 (取代固定的 2%)
    # 邏輯: 停損點 = 進場價 +/- (2 * ATR)
    dynamic_stop_pct = min(max(s.current_atr * 2.0 / avg, 0.005), 0.05) # 限制在 0.5% ~ 5% 之間
    usdt_pnl = profit_pct * avg * abs(s.qty)
    if profit_pct <= -dynamic_stop_pct:
        cs = 'sell' if is_long else 'buy'
        print(f"🛑 [動態停損] {sym} 虧損達 {profit_pct*100:.2f}% (大於 {dynamic_stop_pct*100:.2f}%)，執行停損")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="動態硬停損", is_stop_loss=True)
        return
    # ── 統一利潤保護機制 (取代原本三組重疊規則：低利潤鎖利 / 利潤轉負出場 / 80%回吐鎖利) ──
    # 規則：
    #   1) 開倉未滿 60 秒不啟動，避免開倉初期正常雜訊把單子洗出去
    #   2) 必須「曾經」到過 0.5% 獲利才啟用保護 (原本 0.2%/0.3% 太容易被正常震盪觸發)
    #   3) 不論現在是正還是負，只要回吐到只剩最高獲利的 40% (即回吐超過60%)，就出場
    #      → 用同一條規則處理「轉負」與「小利回吐」，避免互相搶跑
    MIN_HOLD_FOR_PROFIT_LOCK_SEC = 60   # 開倉滿60秒後才啟動利潤保護
    PROFIT_LOCK_TRIGGER = 0.005          # 曾經到過 0.5% 才啟用
    PROFIT_LOCK_RATIO   = 0.8            # 回落到只剩 80% (回吐超過20%) 就出場

    if (hold_sec >= MIN_HOLD_FOR_PROFIT_LOCK_SEC
            and s.highest_profit_pct >= PROFIT_LOCK_TRIGGER
            and profit_pct < s.highest_profit_pct * PROFIT_LOCK_RATIO):
        cs = 'sell' if is_long else 'buy'
        kept_pct = (profit_pct / s.highest_profit_pct * 100) if s.highest_profit_pct > 0 else 0
        logger.info(
            f"🔐 [利潤保護出場] {sym} 最高 {s.highest_profit_pct*100:.2f}%"
            f" → 現在 {profit_pct*100:.2f}% (剩 {kept_pct:.0f}%)，回吐超過60%，出場"
        )
        await close_position(
            sym, cs, abs(s.qty), p, avg,
            reason="利潤保護出場",
            is_profit=(profit_pct >= 0)
        )
        s.highest_profit_pct = 0.0
        return
    if should_recover_from_reversal(sym, is_long):
        recovery_side = 'sell' if is_long else 'buy'
        logger.info(f"🔄 [反向補救] {sym} 方向錯誤且出現反轉訊號，直接反手")
        await close_position(sym, recovery_side, abs(s.qty), p, avg, reason="反轉補救", is_stop_loss=True)
        s.highest_profit_pct = 0.0
        return

    base_tp_atr = 0.008
    base_tp_trend = 0.012
    regime = MARKET_WIND.get("market_regime", "NORMAL_CHOP")
    if regime == "RAGING_BULL" and is_long:
        base_tp_atr = 0.030
        base_tp_trend = 0.035
    elif regime == "PANIC_BEAR" and not is_long:
        base_tp_atr = 0.030
        base_tp_trend = 0.035

    # 微觀 AI 環境標籤：震盪區間極速停利
    ai_regime = getattr(s, "ai_regime", "CHOP")
    if ai_regime == "CHOP":
        if profit_pct >= 0.005:
            cs = 'sell' if is_long else 'buy'
            print(f"🏓 [震盪網格停利] {sym} 獲利達 {profit_pct*100:.2f}% (>= 0.5%)，震盪區間極速入袋！")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="震盪網格極速停利", is_profit=True)
            return

    # ── 新增：分批止盈 (Partial Take Profit) ──
    # 當達到 base_tp_atr 時，不全平，先平 50%，後續放寬移動停利
    if s.highest_profit_pct >= base_tp_atr and not s.has_partial_closed:
        cs = 'sell' if is_long else 'buy'
        logger.info(f"🎯 [分批止盈] {sym} 獲利達 {s.highest_profit_pct*100:.2f}% (>= {base_tp_atr*100:.2f}%)，先平倉 50% 落袋為安，剩餘倉位放寬移動停利")
        await close_position(sym, cs, abs(s.qty) * 0.5, p, avg, reason="分批止盈(50%)", is_profit=True)
        s.has_partial_closed = True
        return

    # 3. 大波段移動停利 (ATR Trailing Stop)
    if s.highest_profit_pct >= base_tp_atr:
        trail_stop_dist = s.current_atr * (3.0 if s.has_partial_closed else 1.5)
        if is_long and p <= s.trailing_highest - trail_stop_dist:
            cs = 'sell'
            print(f"🏃 [ATR移動停利] {sym} 最高獲利達 {s.highest_profit_pct*100:.2f}%，回落 {trail_stop_dist/s.current_atr:.1f} ATR，觸發出場！")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="ATR移動停利", is_profit=True)
            return
        elif not is_long and p >= s.trailing_lowest + trail_stop_dist:
            cs = 'buy'
            print(f"🏃 [ATR移動停利] {sym} 最高獲利達 {s.highest_profit_pct*100:.2f}%，回落 {trail_stop_dist/s.current_atr:.1f} ATR，觸發出場！")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="ATR移動停利", is_profit=True)
            return


    # 4. 趨勢感知鎖利 (Trend-Aware Trailing)
    # 若趨勢仍順向則繼續持有，趨勢翻轉才出場
    if s.highest_profit_pct >= base_tp_trend and profit_pct < s.highest_profit_pct * 0.8:
        # 趨勢評分：3 個條件，≥ 2 個符合視為趨勢仍在
        cur_open = float(s.ohlcv[-1][1]) if len(s.ohlcv) >= 1 else p
        if is_long:
            trend_checks = [
                s.ema20 > 0 and p > s.ema20,          # 價格在 EMA20 之上
                s.macd_hist > 0,                        # MACD 柱狀體為正
                p > cur_open,                           # 當前 K 線為綠 (漲)
            ]
        else:
            trend_checks = [
                s.ema20 > 0 and p < s.ema20,           # 價格在 EMA20 之下
                s.macd_hist < 0,                        # MACD 柱狀體為負
                p < cur_open,                           # 當前 K 線為紅 (跌)
            ]
        trend_score = sum(trend_checks)
        trend_still_ok = trend_score >= 2

        if trend_still_ok:
            logger.debug(f"@@COIN_DEBUG@@ ✋ {sym} [趨勢持倉] 獲利回落但趨勢仍在 (評分 {trend_score}/3)，繼續持有")
        else:
            cs = 'sell' if is_long else 'buy'
            kept_pct = (profit_pct / s.highest_profit_pct * 100) if s.highest_profit_pct > 0 else 0
            print(f"🛡️ [趨勢翻轉鎖利] {sym} 最高獲利 {s.highest_profit_pct*100:.2f}%，目前 {profit_pct*100:.2f}% ({kept_pct:.0f}%)，趨勢翻轉(評分 {trend_score}/3)，出場")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="趨勢翻轉鎖利", is_profit=True)
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
        if is_decaying and profit_pct * 100 >= 0.8:
            cs = 'sell' if is_long else 'buy'
            print(f"📉 [動能衰減] {sym} 利潤連5次下滑且大於 0.8% ({profit_pct*100:.2f}%)，即時出場落袋為安")
            await close_position(sym, cs, abs(s.qty), p, avg, reason="動能衰減落袋為安", is_profit=True)
            s.highest_profit_pct = 0.0
            return

    # 已移除：動能停滯微利出場 (10分鐘 0.1%~1.2%) 與 15分鐘時間停損
    # 讓單子有足夠的時間發展，不再被時間強迫結算。

    # 7. ATR 完全獲利出場 (Full ATR TP)
    # 提高倍數 3.0 → 4.0 → 退回 2.5x 避免難以觸發
    tp_dist = s.current_atr * 2.5
    if (is_long and p >= avg + tp_dist) or (not is_long and p <= avg - tp_dist):
        cs = 'sell' if is_long else 'buy'
        logger.info(f"🎯 [ATR停利K線] {sym}")
        await close_position(sym, cs, abs(s.qty), p, avg, reason="ATR停利K線", is_profit=True)
        return

# ── 進場邏輯 ──────────────────────────────────────────────────




async def execute_order(sym, side, price, route="a", is_ai=False, strength=0.0):
    # ── 防止同一幣種並發重複開倉 ──
    if sym in EXECUTING_SYMBOLS:
        logger.warning(f"⚠️ [並發鎖] {sym} 已有開倉進行中，跳過重複執行")
        return
    EXECUTING_SYMBOLS.add(sym)
    try:
        await _execute_order_inner(sym, side, price, route, is_ai, strength)
    finally:
        # 無論成功失敗都要釋放鎖
        EXECUTING_SYMBOLS.discard(sym)

async def _execute_order_inner(sym, side, price, route="a", is_ai=False, strength=0.0):
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
        # 取得 ccxt 格式幣種代號 (ex: BTC/USDT)
        try:
            ccxt_sym = exchange.market(sym)['symbol']
        except Exception:
            ccxt_sym = sym
        ticker_data = await exchange.fetch_bids_asks([ccxt_sym])
        ticker = ticker_data.get(ccxt_sym)
        if ticker and ticker.get('info'):
            info = ticker['info']
            best_bid = info.get('bidPrice')
            best_ask = info.get('askPrice')
            bid_qty = info.get('bidQty')
            ask_qty = info.get('askQty')

            if best_bid and best_ask:
                best_bid = float(best_bid)
                best_ask = float(best_ask)
                spread = (best_ask - best_bid) / best_ask
                if spread > 0.001:  # 0.1%
                    logger.warning(f"🛑 [流動性風控] {sym} 買賣價差 {spread*100:.2f}% > 0.1%，拒絕開倉！")
                    return

            if bid_qty and ask_qty:
                bid_qty = float(bid_qty)
                ask_qty = float(ask_qty)
                base_amt_est = (margin * LEVERAGE) / price
                target_qty = ask_qty if side == 'buy' else bid_qty
                if target_qty > 0 and base_amt_est > target_qty * 5.0:
                    logger.warning(f"🛑 [深度風控] {sym} 預估下單量 {base_amt_est:.4f} 超過盤口首層深度 500% ({target_qty:.4f})，拒絕開倉！")
                    return
    except Exception as e:
        logger.debug(f"⚠️ [流動性檢查失敗] {sym}: {e}")

    # ===== AI 信心分數倉位控制 (Confidence-based Sizing) =====
    ai_confidence = getattr(s, "ai_confidence", 1.0)
    if is_ai and ai_confidence > 0:
        # 最低保留 30% 倉位，避免數量過小無法開倉
        confidence_multiplier = max(ai_confidence, 0.3)
        logger.info(f"🧠 [信心權重] {sym} AI 信心分數 {ai_confidence*100:.0f}%，倉位縮放倍率: {confidence_multiplier:.2f}x")
        margin = margin * confidence_multiplier
    # ==========================================================

    base_amt = (margin * LEVERAGE) / price
    if base_amt < 0.001:
        logger.warning(f"⚠️ [風控] {sym} 數量過小 {base_amt:.6f}")
        return

    if GLOBAL_STATE["consecutive_losses"] >= 3:
        logger.info(f"🛡️ [連敗降倉] 全局連輸 {GLOBAL_STATE['consecutive_losses']} 次，基礎倉位減半")
        base_amt *= 0.5

    if s.entry_count == 0:
        # 智能動態倉位 (Dynamic Position Sizing)
        size_multiplier = 1.0
        ai_conf = getattr(s, "ai_confidence", 0.0)
        
        if is_ai and ai_conf >= 0.8 and strength >= 8.0:
            size_multiplier = 1.5
            logger.info(f"💎 [智能倉位] {sym} 訊號極強 (分數:{strength:.1f}, AI信心:{ai_conf:.2f}) -> 放大倉位至 1.5 倍")
        elif not is_ai:
            size_multiplier = 0.5
            logger.info(f"🛡️ [AI Fallback 縮倉] {sym} 無 AI 輔助 (分數:{strength:.1f})，縮減倉位至 0.5 倍")
            
        base_amt *= size_multiplier
    elif s.entry_count == 1:
        base_amt *= 0.5   # 第一次加倉 50%
    elif s.entry_count == 2:
        base_amt *= 0.25  # 第二次加倉 25%
    else:
        return

    if PAPER_TRADING:
        try:
            update_paper_state(pk, side, price, base_amt, is_ai=is_ai)
            old_qty = abs(s.qty)
            if side == 'buy':
                s.qty += base_amt
            else:
                s.qty -= base_amt
            if s.avg_price <= 0:
                s.avg_price = price
            else:
                s.avg_price = ((s.avg_price * old_qty) + (price * base_amt)) / (old_qty + base_amt)
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

    # 檢查當前 K 線和上一根 K 線，防範連續插針
    for i in [-1, -2]:
        candle = s.ohlcv[i]
        open_price = float(candle[1])
        high = float(candle[2])
        low = float(candle[3])
        close_price = float(candle[4])
        body = abs(close_price - open_price)
        upper_wick = high - max(open_price, close_price)
        lower_wick = min(open_price, close_price) - low
        candle_range = high - low
        
        # 1. 絕對插針過濾 (單根 K 線波動過大，超過 3 倍 ATR)
        if s.current_atr > 0 and candle_range > s.current_atr * 3.0:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] K線波動 {candle_range:.5f} > 3x ATR {s.current_atr*3:.5f}，極端行情避險")
            return False

        if body <= 0:
            continue

        if side == 'buy':
            # 若 K 線是明顯的反轉 pin bar (上影線過長)，判定為不安全的多頭進場
            if upper_wick > body * 2.0:
                logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 上影線 {upper_wick:.5f} > 實體 {body:.5f} 兩倍，做多危險")
                return False
            # 針對當前 K 線收跌，且上影線大於實體的情況
            if i == -1 and len(s.ohlcv) >= 2:
                prev_close = float(s.ohlcv[-2][4])
                if close_price < prev_close and upper_wick >= body:
                    logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 收跌且上影線過長，做多危險")
                    return False

        elif side == 'sell':
            # 若 K 線是明顯的下引線 pin bar (下影線過長)，判定為不安全的空頭進場
            if lower_wick > body * 2.0:
                logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 下影線 {lower_wick:.5f} > 實體 {body:.5f} 兩倍，做空危險")
                return False
            # 針對當前 K 線收漲，且下影線大於實體的情況
            if i == -1 and len(s.ohlcv) >= 2:
                prev_close = float(s.ohlcv[-2][4])
                if close_price > prev_close and lower_wick >= body:
                    logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 收漲且下影線過長，做空危險")
                    return False

    return True


def is_entry_volume_confirmed(s):
    if len(s.ohlcv) < 6:
        return True
    current_vol = s.current_vol
    recent_5_vols = [x[5] for x in s.ohlcv[-6:-1]]
    avg_vol_5 = sum(recent_5_vols) / 5
    if current_vol <= avg_vol_5 * STRATEGY_CONF["VOLUME_CONFIRM_RATIO"]:
        return False
    return True


def risk_guard(sym, side, route="a"):
    if not GLOBAL_STATE["trading_enabled"]:
        return False

    market_regime = MARKET_WIND.get("market_regime", "NORMAL_CHOP")
    if side == 'buy' and market_regime == "PANIC_BEAR":
        logger.info(f"🛑 {sym} 大盤處於極度恐慌(PANIC_BEAR)，禁止做多")
        return False
    if side == 'sell' and market_regime == "RAGING_BULL":
        logger.info(f"🛑 {sym} 大盤處於狂暴牛市(RAGING_BULL)，禁止做空")
        return False

    is_trend = route == "a"
    if side == 'buy' and not MARKET_WIND.get("allow_long", True) and is_trend:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤瀑布風控] 大盤異常跌勢，禁止順勢開多")
        return False
    if side == 'sell' and not MARKET_WIND.get("allow_short", True) and is_trend:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤上漲風控] 大盤異常漲勢，禁止順勢開空")
        return False

    if side == 'buy' and MARKET_WIND.get("btc_1h_trend") == "bear":
        logger.info(f"🛑 {sym} BTC 1H趨勢向下，禁止做多")
        return False

    s = STATES[sym]
    
    if side == 'buy' and s.htf_4h_trend == "short":
        logger.info(f"🛑 {sym} 4H趨勢向下，禁止做多")
        return False
    if side == 'sell' and s.htf_4h_trend == "long":
        logger.info(f"🛑 {sym} 4H趨勢向上，禁止做空")
        return False
        
    cp = s.close_price

    last_exit = getattr(s, "last_exit_time", 0.0)
    last_exit_type = getattr(s, "last_exit_type", "normal")
    cooldown_map = {"stop_loss": 1800, "profit": 60, "normal": 120}
    required_cool = cooldown_map.get(last_exit_type, 90)

    if time.time() - last_exit < required_cool:
        # 停損冷卻：只鎖同方向，反向允許
        if last_exit_type == "stop_loss":
            stopped_side = s.last_stop_loss_side
            if stopped_side == side:  # 只擋同向
                logger.info(f"🛑 {sym} 停損冷卻中 (剩 {required_cool - (time.time()-last_exit):.0f}秒)，禁止同向重開")
                return False
            # 反向允許通過，繼續後面的檢查
        else:
            return False

    # 決定不同幣種的門檻參數
    is_high_vol = sym in HIGH_VOLATILITY_COINS
    is_anchor = sym in ANCHOR_COINS
    
    min_atr_pct = 0.0010 if is_high_vol else 0.0005
    ema_limit_pct = 0.05 if is_anchor else (0.02 if is_high_vol else 0.03)

    # 波動率過濾 (Low Volatility)
    atr_pct = s.current_atr / cp if cp > 0 else 0
    if atr_pct < min_atr_pct:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [低波動率過濾] ATR {atr_pct*100:.3f}% < {min_atr_pct*100:.2f}%，盤整死水不交易")
        return False

    # 能量過濾 (Relative Volume, RV) - 避免流動性死水
    if s.avg_vol_24h_1m > 0 and s.current_vol < s.avg_vol_24h_1m * 0.6:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [相對成交量過濾] 當前成交量 {s.current_vol:.2f} < 24h平均的60% ({s.avg_vol_24h_1m * 0.6:.2f})，能量不足")
        return False

    # 價格變動速度 (Price Velocity) - 無動能過濾
    if len(s.ohlcv) >= 10:
        recent_10 = s.ohlcv[-10:]
        avg_move = sum([abs((x[4] - x[1]) / x[1]) for x in recent_10 if x[1] > 0]) / 10
        if avg_move < 0.0010:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [無動能過濾] 過去10K線平均振幅僅 {avg_move*100:.3f}% < 0.1%，判定無動能")
            return False

    # RSI 預過濾 (避免極度超買/超賣時進場追高/追空)
    if side == 'buy' and s.current_rsi > 80:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [RSI超買過濾] RSI={s.current_rsi:.1f} > 80，拒絕追高")
        return False
    if side == 'sell' and s.current_rsi < 20:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [RSI超賣過濾] RSI={s.current_rsi:.1f} < 20，拒絕追空")
        return False

    # 偏離度過濾 (Overextension - Distance from EMA20)
    if s.ema20 > 0:
        if side == 'buy' and cp > s.ema20 * (1 + ema_limit_pct):
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [過度擴張過濾] 價格偏離 EMA20 超過 {ema_limit_pct*100:.1f}% (追高風險)，拒絕進多")
            return False
        if side == 'sell' and cp < s.ema20 * (1 - ema_limit_pct):
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [過度擴張過濾] 價格偏離 EMA20 低於 -{ema_limit_pct*100:.1f}% (追空風險)，拒絕進空")
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

    # ── 當前 K 線插針過濾 (Current Candle Spike Filter) ──
    # 門檻 2.2 ATR：只封鎖真正的插針行情，不封鎖正常強勢K線
    if len(s.ohlcv) >= 1:
        cur = s.ohlcv[-1]
        cur_open  = float(cur[1])
        cur_close = float(cur[4])
        atr_ref = s.current_atr if s.current_atr > 0 else cp * 0.005
        candle_move = cur_close - cur_open  # 正=漲，負=跌
        # 做多時：當前K已急拉超過 2.2 ATR → 插針頂部追多，拒絕
        if side == 'buy' and candle_move > atr_ref * 2.2:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針追多過濾] 當前K已漲 {candle_move:.5f} > 2.2 ATR {atr_ref*2.2:.5f}")
            return False
        # 做空時：當前K已急殺超過 2.2 ATR → 插針底部追空，拒絕
        if side == 'sell' and candle_move < -atr_ref * 2.2:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針追空過濾] 當前K已跌 {candle_move:.5f} < -2.2 ATR {-atr_ref*2.2:.5f}")
            return False
    
    # 均線過濾器：僅限制 Route A (順勢)
    if is_trend and s.ema50 > 0:
        ma_trend = s.ema50
        if side == 'buy' and cp <= ma_trend:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [MA50趨勢保護] 順勢做多，但價格 {cp:.4f} <= MA50 {ma_trend:.4f}")
            return False
        if side == 'sell' and cp >= ma_trend:
            logger.info(f"@@COIN_DEBUG@@ 觸發 [MA50趨勢保護] 順勢做空，但價格 {cp:.4f} >= MA50 {ma_trend:.4f}")
            return False

    # 大週期 (HTF) 1H 趨勢過濾器：(恢復寬鬆，僅過濾順勢)
    htf_trend = s.htf_trend
    if htf_trend and is_trend:
        if side == 'buy' and htf_trend == 'short':
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [1H 大週期過濾] 1H 趨勢為空頭，禁止順勢開多")
            return False
        if side == 'sell' and htf_trend == 'long':
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [1H 大週期過濾] 1H 趨勢為多頭，禁止順勢開空")
            return False
            
    # ── 逆勢路由 HTF 方向過濾 ──────────────────────────────────
    if route in ['b', 'c', 's']:
        htf = s.htf_trend
        if side == 'buy' and htf == 'short' and s.current_rsi > 32.0:
            logger.info(
                f"@@COIN_DEBUG@@ 🛑 {sym} [逆勢HTF過濾] "
                f"1H空頭+RSI={s.current_rsi:.1f}>32，禁止逆勢做多"
            )
            return False
        if side == 'sell' and htf == 'long' and s.current_rsi < 68.0:
            logger.info(
                f"@@COIN_DEBUG@@ 🛑 {sym} [逆勢HTF過濾] "
                f"1H多頭+RSI={s.current_rsi:.1f}<68，禁止逆勢做空"
            )
            return False
    # 極端波動率過濾：當市場處於瘋狂洗盤、暴漲暴跌時，禁止任何逆勢搶短
    if not is_trend:
        atr_history = s.atr_history
        atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
        current_atr = s.current_atr
        
        if atr_24h_avg > 0 and current_atr > atr_24h_avg * 2.0:
            logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [極端波動率保護] 目前 ATR {current_atr:.5f} > 均值兩倍 {atr_24h_avg*2.0:.5f} (禁止逆勢接刀/摸頭)")
            return False
            
    if len(s.ohlcv) < 20:
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線不足] 當前長度 {len(s.ohlcv)} < 20")
        return False
    if not is_entry_pin_safe(sym, side):
        logger.info(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 反向長影線/方向未確認")
        return False
        
    # 量能確認過濾器 (Route S 背離經常伴隨縮量，因此不強制要求量能)
    if route != "s" and not is_entry_volume_confirmed(s):
        return False
        
    # ADX 趨勢強度限制：僅限制順勢策略
    if is_trend:
        adx_val = getattr(s, "adx", None)
        if adx_val is not None and adx_val < STRATEGY_CONF["ADX_MIN_THRESHOLD"]:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} ADX {adx_val:.1f} < {STRATEGY_CONF['ADX_MIN_THRESHOLD']}")
            return False
        elif adx_val is None:
            logger.debug(f"@@COIN_DEBUG@@ ⚠️ {sym} ADX 數值為 None，無法進行過濾，保守放行。")

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

    # 定義動態 RSI 門檻 (稍嚴格版)
    LONG_RSI_NORMAL = 42.0   # 45 → 42
    SHORT_RSI_NORMAL = 58.0  # 55 → 58
    LONG_RSI_HIGH_VOL = 38.0  # 40 → 38
    SHORT_RSI_HIGH_VOL = 62.0 # 60 → 62

    atr_history = s.atr_history
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.current_atr

    if current_atr > atr_24h_avg * STRATEGY_CONF["ATR_SPIKE_MULTIPLIER"] and atr_24h_avg > 0:
        long_rsi_threshold = LONG_RSI_HIGH_VOL
        short_rsi_threshold = SHORT_RSI_HIGH_VOL
        vol_mode = "高波動模式 (High Vol)"
    else:
        # 在低波動模式下，使用寬鬆門檻
        long_rsi_threshold = LONG_RSI_NORMAL
        short_rsi_threshold = SHORT_RSI_NORMAL
        vol_mode = "低波動模式 (Low Vol)"
    s.vol_mode = vol_mode

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

    # 收盤價方向確認：嚴格要求突破上一根的收盤價，且連續兩根確認
    last_candle_confirmed_long = (
        len(s.ohlcv) >= 3 and 
        s.ohlcv[-1][4] > s.ohlcv[-2][4] and
        s.ohlcv[-2][4] > s.ohlcv[-3][4]
    )
    last_candle_confirmed_short = (
        len(s.ohlcv) >= 3 and 
        s.ohlcv[-1][4] < s.ohlcv[-2][4] and
        s.ohlcv[-2][4] < s.ohlcv[-3][4]
    )

    logger.debug(f"@@COIN_DEBUG@@ 🔍 {sym} 條件檢測 | RSI門檻(L/S: {long_rsi_threshold:.0f}/{short_rsi_threshold:.0f}): {rsi < long_rsi_threshold}/{rsi > short_rsi_threshold} | BB區間(L/S): {is_in_bb_zone_long}/{is_in_bb_zone_short} | MACD滿足(L/S): {long_macd_ok}/{short_macd_ok} (交叉:{long_macd_cross}/{short_macd_cross}, 柱狀體向上/下:{long_macd_hist_aligned}/{short_macd_hist_aligned}) | 收盤價確認(L/S): {last_candle_confirmed_long}/{last_candle_confirmed_short}")

    ema50 = s.ema50
    trend_confluence_long = ema50 == 0.0 or close > ema50
    trend_confluence_short = ema50 == 0.0 or close < ema50

    is_above_sma200 = s.sma200_15m > 0 and close > s.sma200_15m * 0.999
    is_below_sma200 = s.sma200_15m > 0 and close < s.sma200_15m * 1.001

    # RSI 趨勢與方向確認 (開多必須 RSI > 50 且最新 RSI 在上升，開空必須 RSI < 50 且最新 RSI 在下降)
    rsi_rising = len(rsis) >= 2 and rsis[-1] > rsis[-2]
    rsi_falling = len(rsis) >= 2 and rsis[-1] < rsis[-2]

    # Route A (Right Side / 順勢交易)
    route_a_long = (is_above_sma200 or trend_long) and (long_macd_cross or long_macd_hist_aligned) and last_candle_confirmed_long
    route_a_short = (is_below_sma200 or trend_short) and (short_macd_cross or short_macd_hist_aligned) and last_candle_confirmed_short

    # Route B (Left Side / 逆勢反轉)
    route_b_long = rsi < 30.0 and is_in_bb_zone_long and s.htf_trend == "long" and s.htf_4h_trend == "long"
    route_b_short = rsi > 70.0 and is_in_bb_zone_short and s.htf_trend == "short" and s.htf_4h_trend == "short"

    momentum_long = close > prev_close * 1.001 and (s.current_vol >= max(1000.0, s.vol_ma20 * 0.8) or s.trade_signal_strength > 0.2)
    momentum_short = close < prev_close * 0.999 and (s.current_vol >= max(1000.0, s.vol_ma20 * 0.8) or s.trade_signal_strength > 0.2)

    # Route C (Left Side / 搶極端反彈): 只有在有順勢或足夠動能支持時才允許進場
    route_c_long = rsi <= 20.0 and last_candle_confirmed_long and s.htf_trend == "long" and s.htf_4h_trend == "long"
    route_c_short = rsi >= 80.0 and last_candle_confirmed_short and s.htf_trend == "short" and s.htf_4h_trend == "short"

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

    long_score = 0.0
    short_score = 0.0
    long_route = None
    short_route = None

    if long_base_ok:
        long_route = "s" if route_s_long else "c" if route_c_long else "b" if route_b_long else "a"
        if long_route == "a":
            long_score = 4.0 + ((close - ema20) / max(ema20, 1e-8) * 100) + 10.0
        else:
            long_score = max(0.0, long_rsi_threshold - rsi) + 4.0
        if momentum_long: long_score += 3.0
        if long_macd_cross: long_score += 5.0

    if short_base_ok:
        short_route = "s" if route_s_short else "c" if route_c_short else "b" if route_b_short else "a"
        if short_route == "a":
            short_score = 4.0 + ((ema20 - close) / max(ema20, 1e-8) * 100) + 10.0
        else:
            short_score = max(0.0, rsi - short_rsi_threshold) + 4.0
        if momentum_short: short_score += 3.0
        if short_macd_cross: short_score += 5.0

    # 選分數高的方向，不再固定優先多單
    if long_score == 0 and short_score == 0:
        return (None, 0.0, None, False)

    if short_score > long_score:
        side, strength, route = "sell", short_score, short_route
    else:
        side, strength, route = "buy", long_score, long_route

    # --- 以下為統一的進階風控 (套用於勝出方向) ---

    # 左側交易併發控制：如果不允許太多左側交易同時發生
    if route in ['b', 'c', 's'] and left_side_positions >= 2:  # 寬鬆：允許最多 2 個左側倉位
        logger.info(f"🛑 [風控] {sym} 觸發左側({route})，但目前已有 {left_side_positions} 個左側倉位，放棄開倉。")
        return (None, 0.0, None, False)

    # --- FNG 全局門檻偏移 ---
    fng = MARKET_WIND.get("fng_value", 50)
    if fng > 75:
        strength += 1.0 if side == 'sell' else -1.0  # 貪婪：放寬做空，提高做多門檻
    elif fng < 25:
        strength += 1.0 if side == 'buy' else -1.0   # 恐慌：放寬做多，提高做空門檻
        
    if strength > s.max_strength:
        s.max_strength = strength

    fast_path_threshold = STRATEGY_CONF["FAST_PATH_SCORE_LOW_VOL"] if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)" else STRATEGY_CONF["FAST_PATH_SCORE_HIGH_VOL"]
    expected_trend = "long" if side == "buy" else "short"
    
    fast_path_ok = route == "a" and getattr(s, "htf_trend", None) == expected_trend and strength >= fast_path_threshold
    if STRATEGY_CONF["FAST_PATH_REQUIRE_4H"] and getattr(s, "htf_4h_trend", None) != expected_trend:
        fast_path_ok = False
        
    if fast_path_ok:
        logger.info(f"⚡ [Fast Path] {sym} 觸發做{side}直通 (Score: {strength:.2f} >= {fast_path_threshold})，無須等待 AI！")
        return (side, strength, route, True)  # is_ai_assisted=True 允許下全倉
    
    # 若分數根本未達送交 AI 的門檻，直接放棄
    ai_trigger_threshold = STRATEGY_CONF.get("AI_TRIGGER_SCORE_LOW_VOL", 5.0) if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)" else STRATEGY_CONF.get("AI_TRIGGER_SCORE_HIGH_VOL", 6.0)
    if strength < ai_trigger_threshold:
        return (None, 0.0, None, False)

    # 整合 AI 環境標籤做分流
    ai_action = getattr(s, "ai_action", "HOLD")
    ai_regime = getattr(s, "ai_regime", "CHOP")
    ai_decision = getattr(s, "ai_decision", "REJECT")
    ai_setup = getattr(s, "ai_setup_type", "None")
    ai_conf = getattr(s, "ai_confidence", 0.0)
    ai_updated_at = getattr(s, "ai_updated_at", 0.0)
    ai_fresh = (time.time() - ai_updated_at) < AI_UPDATE_INTERVAL * STRATEGY_CONF["AI_FRESHNESS_MULTIPLIER"]
    
    is_ai_assisted = False
    if ai_fresh:
        expected_ai_action = "BUY" if side == "buy" else "SELL"
        if ai_decision != "APPROVE" or ai_action != expected_ai_action:
            return (None, 0.0, None, False)  # 必須拿到明確的 APPROVE 與對應方向指令
        
        # 動態信心門檻
        min_conf = STRATEGY_CONF["AI_CONF_CHOP"] if ai_regime == "CHOP" else STRATEGY_CONF["AI_CONF_TREND"]
        if ai_conf < min_conf:
            logger.info(f"🔇 [信心過濾] {sym} AI 信心 {ai_conf:.2f} < {min_conf:.2f} ({ai_regime})，做{side}訊號被否決")
            return (None, 0.0, None, False)
        
        is_ai_assisted = True
        bonus = ai_conf * 3.0
        if ai_setup == "Breakout" and route == "a":
            bonus += 2.0
        elif ai_setup == "Reversal" and route in ["b", "c", "s"]:
            bonus += 2.0
        
        if ai_regime == "CHOP":
            if route == "a":
                return (None, 0.0, None, False)  # 震盪區不追高殺低
            strength += bonus
        elif ai_regime == "TREND_LONG":
            if side == "sell" or route in ["b", "c"]:
                return (None, 0.0, None, False)  # 多頭趨勢不抄底也不做空
            strength += bonus
        elif ai_regime == "TREND_SHORT":
            if side == "buy" or route in ["b", "c"]:
                return (None, 0.0, None, False)  # 空頭趨勢不摸頭也不做多
            strength += bonus
            
        if not is_ai_assisted:
            return (None, 0.0, None, False)
    else:
        # AI 過期（斷線降級模式）：允許超高分訊號繞過 AI 直接開倉
        ai_stale_secs = time.time() - ai_updated_at
        degraded_threshold = STRATEGY_CONF.get("AI_DEGRADED_SCORE_MIN", 25.0)
        if strength >= degraded_threshold:
            logger.warning(f"🆘 [AI降級模式] {sym} AI 已 {ai_stale_secs/60:.1f} 分鐘未回應，高分訊號 ({strength:.2f}>={degraded_threshold}) 允許進場")
            is_ai_assisted = False
        else:
            logger.info(f"⏳ [等待 AI] {sym} 慢速路徑訊號 (Score: {strength:.2f}) 需等待 AI 審查，暫不開倉。")
            return (None, 0.0, None, False)
    
    return (side, strength if strength >= STRATEGY_CONF["ENTRY_SCORE_MIN"] else 0.0, route, is_ai_assisted)

async def process_symbols():
    logger.info(f"== DEBUG == process_symbols running, trading_enabled={GLOBAL_STATE['trading_enabled']}")
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
                if profit_pct <= -0.025:
                    cs = 'sell' if is_long else 'buy'
                    reason_msg = "2.5%跌幅停損"
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
    balance = get_balance()
    max_pos = get_max_positions(balance)
    
    # --- 多幣虧損風控 ---
    open_syms = [sym for sym in ALL_SYMBOLS if abs(STATES[sym].qty) > 0.000001]
    losing_count = 0
    for sym in open_syms:
        s = STATES[sym]
        if s.avg_price > 0 and hasattr(s, "close_price"):
            profit = (s.close_price - s.avg_price) / s.avg_price if s.qty > 0 else (s.avg_price - s.close_price) / s.avg_price
            if profit < -0.003:
                losing_count += 1
                
    if losing_count >= 2:
        logger.warning(f"⚠️ [多幣虧損風控] 已有 {losing_count} 個倉位虧損超過 -0.3%，暫停新開倉")
        return
    # --------------------
    
    if open_count >= max_pos:
        return
        
    remaining_slots = max_pos - open_count

    candidates = []
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s.status != "ACTIVE":
            continue
        if getattr(s, "adjusted_this_tick", False):
            continue
        if abs(s.qty) > 0.000001:
            continue
        side_strength = compute_signal_strength(sym)
        if side_strength[0] is None:
            continue
        side, strength, route, is_ai = side_strength
        if strength <= 0.0:
            logger.info(f"🔇 [封鎖] {sym} 分數不足 ({strength:.2f})")
            continue
            
        logger.debug(f"@@COIN_DEBUG@@ 🔎 [訊號過濾前] {sym} | 方向:{side} | 分數:{strength:.2f} | 策略路由:{route}")
            
        if not risk_guard(sym, side, route):
            logger.info(f"🛑 [risk_guard 封鎖] {sym} side={side} route={route} score={strength:.2f}")
            continue
        candidates.append((sym, side, strength, route, is_ai))

    if not candidates:
        return

    candidates.sort(key=lambda x: -x[2])
    logger.info(f"📊 [訊號排行] {' | '.join(f'{sym}:{side}({strength:.2f})' for sym, side, strength, _, _ in candidates[:3])}")

    for i in range(min(remaining_slots, len(candidates))):
        sym, side, strength, route, is_ai = candidates[i]
        s = STATES[sym]
        now = time.time()
        
        # 移除突破確認機制，只要有訊號 (且通過 risk_guard) 就立即開倉
        await execute_order(sym, side, s.close_price, route, is_ai, strength)

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
    balance = get_balance()
    max_pos = get_max_positions(balance)
    logger.info(f"💰 初始餘額: {balance:.2f} USDT")
    logger.info(f"📊 動態最大持倉上限: {max_pos} 個 (隨本金增長自動調整)")
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
    
    logger.info("== DEBUG == initialize_atr_history done")
    await fetch_real_balance()
    logger.info("== DEBUG == fetch_real_balance done")
    await load_open_positions()
    logger.info("== DEBUG == load_open_positions done")
    await fetch_all_sma200()
    logger.info("== DEBUG == fetch_all_sma200 done")
    await fetch_all_htf_trend()
    logger.info("== DEBUG == fetch_all_htf_trend done")
    await fetch_all_htf_4h_trend()
    logger.info("== DEBUG == fetch_all_htf_4h_trend done")

    last_balance_update = time.time()
    last_htf_update = time.time()
    last_htf_4h_update = time.time()

    while True:
        # 定期熱更新策略設定
        load_strategy_config()
        
        try:
            logger.info("== DEBUG == LOOP START")
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
                
            if loop_start - last_htf_update > 1800:  # 每 30 分鐘更新一次 1H 大週期
                await fetch_all_htf_trend()
                last_htf_update = loop_start
            if loop_start - last_htf_4h_update > 7200:  # 每 2 小時更新一次 4H 大週期
                await fetch_all_htf_4h_trend()
                last_htf_4h_update = loop_start

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
    await asyncio.sleep(900)  # Delay first run to avoid conflict with main_loop initialization
    while True:
        await asyncio.sleep(900)
        try:
            await fetch_all_sma200()
        except Exception as e:
            logger.warning(f"⚠️ periodic_sma200_update 發生錯誤: {e}")
        print("🔄 [SMA200] 已更新所有幣種15m SMA200")

async def periodic_status_log():
    while True:
        await asyncio.sleep(60)
        active = sum(1 for s in STATES.values() if s.status == "ACTIVE")
        cooldown = sum(1 for s in STATES.values() if s.status == "COOLDOWN")
        banned = sum(1 for s in STATES.values() if s.status == "BANNED")
        open_syms = get_open_symbols()
        open_str = ', '.join(f"{sym}({'多' if STATES[sym].qty>0 else '空'})" for sym in open_syms) if open_syms else "無"
        btc_1h = MARKET_WIND.get('btc_1h_trend', '未知')
        logger.info(f"📊 [狀態] ACTIVE={active} COOLDOWN={cooldown} BANNED={banned} | BTC 1H趨勢={btc_1h.upper()} | 持倉({len(open_syms)}): {open_str}")

async def export_states_to_json():
    while True:
        try:
            await asyncio.sleep(10)
            export_data = {
                "global": {
                    "ai_next_update": GLOBAL_STATE.get("ai_next_update", 0)
                },
                "symbols": {}
            }
            # We don't necessarily need STATES_LOCK for a simple read copy, but it's safer.
            # However, acquiring lock every 3 sec might block, so we just do a dirty read.
            for sym in list(STATES.keys()):
                s = STATES[sym]
                export_data["symbols"][sym] = {
                    "status": s.status,
                    "qty": s.qty,
                    "avg_price": s.avg_price,
                    "current_price": s.close_price,
                    "rsi": s.current_rsi,
                    "atr": s.current_atr,
                    "trend": s.htf_trend,
                    "ai_action": getattr(s, 'ai_action', "HOLD"),
                    "ai_regime": getattr(s, 'ai_regime', "CHOP"),
                    "ai_confidence": getattr(s, 'ai_confidence', 0.0),
                    "ai_reason": getattr(s, 'ai_reason', ""),
                    "ai_updated_at": getattr(s, 'ai_updated_at', 0.0),
                    "ai_payload": getattr(s, 'ai_payload', None)
                }
            with open("bot_states.json", "w") as f:
                json.dump(export_data, f)
        except Exception as e:
            pass

# AI 連線失敗計數器
AI_FAILURE_COUNT = 0
AI_FAILURE_THRESHOLD = 3  # 連續失敗幾次就嘗試重啟

async def restart_ollama():
    """嘗試自動重啟 Ollama，使 AI 服務恢復"""
    import subprocess
    logger.warning("🔄 [AI自動修復] 嘗試重啟 Ollama 伺服器...")
    try:
        subprocess.Popen(
            ["bash", "-c", "OLLAMA_HOST=0.0.0.0:8888 nohup ollama serve > /home/shudgai999/project/binance-bot/ollama.log 2>&1 &"],
            close_fds=True
        )
        await asyncio.sleep(5)  # 等待 Ollama 啟動
        logger.info("✅ [AI自動修復] Ollama 重啟指令已發送，等待 5 秒後繼續")
    except Exception as e:
        logger.error(f"❌ [AI自動修復] 重啟 Ollama 失敗: {e}")

async def periodic_ai_analysis():
    global AI_FAILURE_COUNT
    while True:
        try:
            snapshots = []
            async with STATES_LOCK:
                force_scan_syms = []
                if FORCE_AI_SCAN:
                    sorted_syms = sorted([sym for sym in ALL_SYMBOLS if sym in STATES], key=lambda x: STATES[x].current_vol, reverse=True)
                    force_scan_syms = sorted_syms[:3]

                for sym in ALL_SYMBOLS:
                    if sym not in STATES: continue
                    if sym in SLOW_OR_LOW_QUALITY_SYMBOLS: continue
                    s = STATES[sym]
                    
                    reason = ""
                    if sym in force_scan_syms:
                        reason = "FORCE_SCAN"
                    elif abs(s.qty) > 0.000001:
                        reason = "POSITION_MANAGEMENT"
                    else:
                        ai_trigger_threshold = STRATEGY_CONF["AI_TRIGGER_SCORE_LOW_VOL"] if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)" else STRATEGY_CONF["AI_TRIGGER_SCORE_HIGH_VOL"]
                        if s.max_strength >= ai_trigger_threshold:
                            reason = f"SIGNAL_TRIGGERED(>{ai_trigger_threshold})"
                        
                    if reason:
                        snapshots.append((sym, copy.copy(s), dict(MARKET_WIND), reason))
                        
            contexts = []
            for sym, s, wind, reason in snapshots:
                logger.debug(f"🔍 {sym} | Sent to AI (Reason: {reason})")
                ctx = await build_ai_context(sym, s, wind)
                # 把 HTF 趨勢注入 context，讓 AI 決策時知道大週期方向
                if ctx and isinstance(ctx, dict):
                    ctx["htf_1h_trend"] = getattr(s, "htf_trend", None)
                    ctx["htf_4h_trend"] = getattr(s, "htf_4h_trend", None)
                    ctx["current_rsi"] = round(s.current_rsi, 2)
                    ctx["ai_trigger_reason"] = reason
                contexts.append(ctx)
                # 即時儲存要發送的 payload 供前端顯示
                if sym in STATES:
                    STATES[sym].ai_payload = ctx
            
            if contexts:
                start_ai_time = time.time()
                results = await fetch_ai_signals(contexts)
                ai_latency = time.time() - start_ai_time
                if results:
                    # ✅ 成功：重置失敗計數
                    AI_FAILURE_COUNT = 0
                    now = time.time()
                    async with STATES_LOCK:
                        for sym, r in results.items():
                            if sym in STATES:
                                STATES[sym].ai_action = r.get("ai_action", "HOLD")
                                STATES[sym].ai_decision = r.get("ai_decision", "REJECT")
                                STATES[sym].ai_regime = r.get("ai_regime", "CHOP")
                                STATES[sym].ai_confidence = float(r.get("ai_confidence", 0.0))
                                STATES[sym].ai_reason = r.get("ai_reason", "")
                                STATES[sym].ai_updated_at = now
                                logger.info(f"🤖 [AI決策] {sym}: {r.get('ai_decision','?')} / {r.get('ai_action','?')} (信心:{float(r.get('ai_confidence',0))*100:.0f}%) | 理由: {r.get('ai_reason','無')}")
                    logger.info(f"🤖 [AI訊號更新] 耗時: {ai_latency:.2f}s | 分析 {len(results)} 個幣種")
                else:
                    # ❌ 有送出但沒有結果 (解析失敗或空回傳)
                    AI_FAILURE_COUNT += 1
                    logger.warning(f"⚠️ [AI] 回傳空結果，失敗累計: {AI_FAILURE_COUNT}/{AI_FAILURE_THRESHOLD}")
                    if AI_FAILURE_COUNT >= AI_FAILURE_THRESHOLD:
                        AI_FAILURE_COUNT = 0
                        await restart_ollama()
        except Exception as e:
            AI_FAILURE_COUNT += 1
            logger.warning(f"⚠️ periodic_ai_analysis 錯誤 (失敗累計 {AI_FAILURE_COUNT}/{AI_FAILURE_THRESHOLD}): {e}")
            if AI_FAILURE_COUNT >= AI_FAILURE_THRESHOLD:
                AI_FAILURE_COUNT = 0
                await restart_ollama()
        
        # 更新下次預計抓取的時間 (供前端倒數)
        GLOBAL_STATE["ai_next_update"] = time.time() + AI_UPDATE_INTERVAL
        await asyncio.sleep(AI_UPDATE_INTERVAL)

async def main():
    global STATES_LOCK
    STATES_LOCK = asyncio.Lock()
    await asyncio.gather(
        main_loop(),
        periodic_sma200_update(),
        periodic_status_log(),
        watch_all_trades(),
        export_states_to_json(),
        periodic_ai_analysis(),
        update_fear_and_greed_index(),
        update_24h_volume(),
    )

import fcntl
import sys

def enforce_single_instance():
    lock_file = "/tmp/multi_coin_bot_v2.lock"
    fp = open(lock_file, 'w')
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("🚨 [系統防護] 偵測到 multi_coin_bot_v2 已經在執行中！為防止重複開倉與狀態衝突，本次啟動已自動終止。")
        sys.exit(1)
    # 不關閉 fp，讓鎖跟隨這個 process 一起存活，程式結束後 OS 自動釋放
    return fp

if __name__ == "__main__":
    _lock_fp = enforce_single_instance()
    asyncio.run(main())
