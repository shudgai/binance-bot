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
from collections import deque
import aiohttp
from ai_signal import build_ai_context, fetch_ai_signals, AI_UPDATE_INTERVAL


class dotdict(dict):
    """dot.notation access to dictionary attributes"""
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

import logging
from dataclasses import dataclass

@dataclass
class Signal:
    symbol: str
    side: str
    qty: float = 0.0
    reverse_confirmed: bool = False
    reason: str = ""
    route: str = ""
    is_ai: bool = False
    strength: float = 0.0



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
from services.symbol_manager import replace_underperforming_symbol

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
    "SOLUSDT",   # 主力 L1
    "BNBUSDT",   # 流動性穩定
    "XRPUSDT",   # 高流動性
    "TAOUSDT",   # AI 板塊代表
    "NEARUSDT",  # 你已熟悉
    "DOGEUSDT",  # 高波動、成交量大
    "WLDUSDT"    # ATR 正常、訊號明顯
]
SLOW_OR_LOW_QUALITY_SYMBOLS = {
    "AERO", "ADA", "DOT", "UNI", "FET",
    "STG", "SEI",
    "NOT",     # ATR = 0，精度問題
    "BOME",    # ATR = 0，精度問題
    "PEOPLE",  # ATR 幾乎為 0
    "1000PEPEUSDT", "1000BONKUSDT", "1000FLOKIUSDT", "WIFUSDT", "ARKMUSDT"
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
    "ATR_SPIKE_MULTIPLIER": 1.5,
    "SEMI_AUTO_EXIT": True
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


BANNED_TOKENS = [
    'CROSS', 'HANA', 'COAI', 'PHA', 'BAN', 'FOGO', 
    'ESPORTS', 'PLAY', 'HOME', 'VELVET', 'AIO', 'ALLO',
    'H', 'CL', 'BZ', 'SIREN', 'BEAT', 'OPG', 'EVAA',
    'XAU', 'XAG',
    'ZEC',
    'NOT', 'BOME', 'PEOPLE',
    'SHIB', 'FLOKI', 'WIF', 'SEI', 'STRK', 'CRV',
    'ARB', 'OP'
]

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

HIGH_VOLATILITY_COINS = {"WIFUSDT", "1000PEPEUSDT", "ORDIUSDT", "BOMEUSDT", "1000BONKUSDT", "1000FLOKIUSDT", "NOTUSDT"}
ANCHOR_COINS = {"BTCUSDT", "ETHUSDT"}
TREND_FOLLOWER_COINS = {"SOLUSDT", "SUIUSDT", "AVAXUSDT", "FETUSDT", "LINKUSDT", "NEARUSDT", "INJUSDT", "ARKMUSDT", "ENAUSDT", "ONDOUSDT", "PENDLEUSDT", "SEIUSDT", "WLDUSDT"}

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
        "rsi_history": [],
        "atr_history": deque(maxlen=1440),
        "recent_adx": [],
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
        "pnl_history": deque(maxlen=5),
        "has_partial_closed": False,
        "ohlcv": [],
        "closes": [],
        "tr_list": [],
        "pending_entry": None,
        "vwap": 0.0,
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
        "has_been_negative": False,
        "trail_tp_price": 0.0,
        "entry_count": 0,
        "avg_entry_price": 0.0,
        "max_additional_entries": 3,
        "entry_cooldown_sec": 30,
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
        "atr_24h_avg": 0.0,
    }
    return dotdict(state_dict)

GLOBAL_STATE = {
    "daily_pnl": 0.0,
    "last_reset_day": "",
    "initial_daily_equity": 0.0,
    "consecutive_losses": 0,
    "trading_enabled": True,
    "recent_entries": [],
    "route_stats": {"a": {"win": 0, "loss": 0}, "b": {"win": 0, "loss": 0}, "c": {"win": 0, "loss": 0}, "s": {"win": 0, "loss": 0}},
    "low_vol_days": {},
    "last_vol_check_day": "",
    "initial_prices": {}
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
        
    profit = max(0.0, balance - 150.0)
    # 每次交易基礎用 150，並加上利潤
    per_slot = 150.0 + profit
    
    # 安全防護：單筆最大不可超過可用餘額的平均分配
    usable = balance * 0.95
    max_safe_slot = usable / max_pos
    
    final_slot = min(per_slot, max_safe_slot)
    return min(final_slot, 1000.0)

# ── 幣種狀態更新 ──────────────────────────────────────────────

def update_states():
    now = time.time()
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s.atr_history:
            s.atr_24h_avg = float(np.mean(s.atr_history))
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
            s.status_reason = f"封禁中 ({max_stops}次停損，準備替換)"
            logger.warning(f"🚫 [狀態] {sym} {max_stops}次停損 → 觸發替換機制")
            
            async def _replace_and_apply():
                new_sym = await replace_underperforming_symbol(exchange, sym)
                if new_sym:
                    apply_symbol_pool_change(load_symbol_pool())
            
            asyncio.create_task(_replace_and_apply())
    else:
        # 一般平倉（時間停損、趨勢翻轉等）：90 秒
        s.status = "COOLDOWN"
        s.next_status_time = now + 90
        s.status_reason = "平倉冷卻 (90秒)"

def reset_coin_state(sym):
    s = STATES.get(sym)
    preserved_exit_time = s.last_exit_time if s else 0.0
    preserved_exit_type = getattr(s, "last_exit_type", "normal") if s else "normal"
    
    # Preserve safety ban state
    preserved_status = getattr(s, "status", "ACTIVE") if s else "ACTIVE"
    preserved_stop_count = getattr(s, "stop_count", 0) if s else 0
    preserved_stop_times = getattr(s, "stop_times", []) if s else []
    preserved_first_stop_time = getattr(s, "first_stop_time", 0.0) if s else 0.0
    preserved_next_status_time = getattr(s, "next_status_time", 0.0) if s else 0.0
    preserved_status_reason = getattr(s, "status_reason", "") if s else ""
    
    STATES[sym] = build_symbol_state(sym)
    s = STATES[sym]
    
    s.last_exit_time = preserved_exit_time
    s.last_exit_type = preserved_exit_type
    s.status = preserved_status
    s.stop_count = preserved_stop_count
    s.stop_times = preserved_stop_times
    s.first_stop_time = preserved_first_stop_time
    s.next_status_time = preserved_next_status_time
    s.status_reason = preserved_status_reason
    s.qty = 0.0
    s.avg_price = 0.0
    s.open_time = 0.0
    s.trailing_highest = 0.0
    s.trailing_lowest = float('inf')
    s.highest_profit_pct = 0.0
    s.pnl_history.clear()
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
        # 改為更敏感的風控 (BTC 15m < -0.3%)
        if btc_change_15m < -0.003 or eth_change_15m < -0.004:
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
            symbols_to_replace = []
            now_day = datetime.datetime.now().strftime("%Y-%m-%d")
            
            for ccxt_sym, ticker in tickers.items():
                sym = ticker.get('info', {}).get('symbol', '')
                if not sym:
                    sym = ccxt_sym.replace('/', '').replace(':USDT', '')
                    
                if sym in STATES:
                    # Binance 的 baseVolume 是 24h 內的基礎幣種總成交量
                    vol_24h = float(ticker.get('baseVolume', 0.0))
                    STATES[sym].avg_vol_24h_1m = vol_24h / 1440.0
                    
                    price = float(ticker.get('lastPrice', 0.0))
                    if price > 0:
                        if sym not in GLOBAL_STATE["initial_prices"]:
                            GLOBAL_STATE["initial_prices"][sym] = price
                        
                        initial_p = GLOBAL_STATE["initial_prices"][sym]
                        price_change_pct = float(ticker.get('percentage', 0.0))
                        
                        drop_below_initial = price < initial_p * 0.4
                        
                        if price_change_pct <= -30.0 or drop_below_initial:
                            reason = "暴跌超過 30%" if price_change_pct <= -30.0 else "跌破初始監控價格的 40%"
                            logger.warning(f"🚨 [緊急替換] {sym} {reason}，觸發淘汰機制！")
                            symbols_to_replace.append((sym, reason))

            # 每日結算量能不足天數
            if now_day != GLOBAL_STATE.get("last_vol_check_day"):
                if GLOBAL_STATE.get("last_vol_check_day"):  # 確保不是第一次啟動
                    # 建立反向對應表 sym -> ccxt_sym
                    sym_to_ccxt = {}
                    for ccxt_sym, t in tickers.items():
                        s = t.get('info', {}).get('symbol', '')
                        if not s:
                            s = ccxt_sym.replace('/', '').replace(':USDT', '')
                        sym_to_ccxt[s] = ccxt_sym
                        
                    for sym in ALL_SYMBOLS:
                        ccxt_sym = sym_to_ccxt.get(sym)
                        ticker = tickers.get(ccxt_sym, {}) if ccxt_sym else {}
                        q_vol = float(ticker.get('quoteVolume', 0.0))
                        if q_vol > 0 and q_vol < 30_000_000:
                            GLOBAL_STATE["low_vol_days"][sym] = GLOBAL_STATE["low_vol_days"].get(sym, 0) + 1
                            if GLOBAL_STATE["low_vol_days"][sym] >= 5:
                                logger.warning(f"🚨 [量能不足] {sym} 連續 5 天 24h 成交額低於 30M USDT，觸發淘汰機制！")
                                symbols_to_replace.append((sym, "連續 5 天量能不足"))
                                GLOBAL_STATE["low_vol_days"][sym] = 0
                        elif q_vol >= 30_000_000:
                            GLOBAL_STATE["low_vol_days"][sym] = 0
                GLOBAL_STATE["last_vol_check_day"] = now_day
                
            for sym, reason in symbols_to_replace:
                if sym in STATES and STATES[sym].status != "BANNED":
                    STATES[sym].status = "BANNED"
                    STATES[sym].status_reason = reason
                    # 進行替換
                    new_sym = await replace_underperforming_symbol(exchange, sym)
                    if new_sym:
                        apply_symbol_pool_change(load_symbol_pool())
                        
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

async def fetch_kline_for_symbol(sym, sem):
    async with sem:
        try:
            res = await asyncio.wait_for(exchange.fetch_ohlcv(sym, TIMEFRAME, limit=100), timeout=10)
            STATES[sym].ohlcv = res
            STATES[sym].close_price = res[-1][4]
        except Exception as e:
            logger.warning(f"⚠️ [K線獲取失敗] {sym}: {e}")

async def fetch_all_klines():
    sem = asyncio.Semaphore(10)
    tasks = [fetch_kline_for_symbol(sym, sem) for sym in ALL_SYMBOLS]
    await asyncio.gather(*tasks)

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
    if len(ohlcv) < 50:  # 確保有足夠的 K 線筆數來計算 EMA50 等長周期指標
        return
        
    closes = np.array([x[4] for x in ohlcv])
    highs = np.array([x[2] for x in ohlcv])
    lows = np.array([x[3] for x in ohlcv])
    volumes = np.array([x[5] for x in ohlcv])
    s.closes = closes
    
    # 計算 VWAP (利用抓取到的 limit=100 根 K 線作為 Rolling Session)
    typical_prices = (highs + lows + closes) / 3.0
    vol_sum = np.sum(volumes)
    s.vwap = float(np.sum(typical_prices * volumes) / vol_sum) if vol_sum > 0 else float(closes[-1])

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
        s.atr_ma20 = float(np.mean(list(s.atr_history)[-20:])) if len(s.atr_history) >= 20 else s.current_atr
        s.atr_24h_avg = float(np.mean(s.atr_history)) if len(s.atr_history) > 0 else 0.0
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

    if len(highs) >= 15:
        adx_val = calculate_adx(highs, lows, closes, period=14)
        if adx_val is None or np.isnan(adx_val):
            s.adx = 0.0
            if hasattr(s, "recent_adx") and isinstance(s.recent_adx, list):
                s.recent_adx.append(0.0)
        else:
            s.adx = float(adx_val)
            if hasattr(s, "recent_adx") and isinstance(s.recent_adx, list):
                s.recent_adx.append(s.adx)

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

def update_trade_exit_advice(sym, advice=None):
    path = "paper_state.json"
    if not os.path.exists(path):
        return
    import fcntl
    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = json.load(f)
            active_sym = f"{sym.replace('USDT', '')}:USDT"
            changed = False
            for t in data.get("trades", []):
                if t.get("symbol") == active_sym and not t.get("is_close"):
                    current_adv = t.get("exit_advice", "")
                    target_adv = advice or ""
                    if current_adv != target_adv:
                        t["exit_advice"] = target_adv
                        changed = True
            if changed:
                f.seek(0)
                json.dump(data, f, indent=4)
                f.truncate()
        except Exception as e:
            logger.error(f"Error updating exit advice for {sym}: {e}")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ==========================================
# 新版 Signal-Driven 核心路由
# ==========================================


async def should_reverse(sym: str, signal: Signal) -> bool:
    s = STATES[sym]
    pos_side = 'long' if s.qty > 0 else ('short' if s.qty < 0 else None)
    if not pos_side:
        return False

    hold_sec = time.time() - s.open_time if s.open_time > 0 else 9999
    profit_pct = (s.close_price - s.avg_price) / max(s.avg_price, 1e-8) if pos_side == 'long' else (s.avg_price - s.close_price) / max(s.avg_price, 1e-8)

    if hold_sec < 30:
        return False

    if getattr(s, "is_breakeven_set", False):
        return False

    if not signal.reverse_confirmed:
        return False

    if signal.strength < 0.6:
        return False

    if s.current_vol < getattr(s, "vol_ma20", 0) * 1.2:
        return False

    if abs(profit_pct) < 0.002:
        return False

    last_exit_time = getattr(s, "last_exit_time", 0)
    time_since_exit = time.time() - last_exit_time
    
    # 針對小幣的嚴格反手冷卻 (15 分鐘冷卻，尤其是停損後)
    is_altcoin = sym not in ["BTCUSDT", "ETHUSDT"]
    cooldown_sec = 900 if is_altcoin else 60
    
    if time_since_exit < cooldown_sec:
        return False

    if (getattr(s, "ai_regime", None) or "CHOP") == "CHOP":
        return False

    ai_conf = getattr(s, "ai_confidence", 0)
    ai_action = (getattr(s, "ai_action", None) or "HOLD").upper()
    is_ai_fresh = time.time() - getattr(s, "ai_updated_at", 0) < 1800
    
    # 若 AI 有近期判定且自信高於 80 且同意反手，則放行；否則退回技術面保護（技術面不允許輕易反手，預設 False）
    if is_ai_fresh and ai_conf >= 80 and ai_action == "REVERSE":
        return True
        
    return False

async def process_signal(sym: str, signal: Signal | None):
    s = STATES[sym]
    pos_side = 'long' if s.qty > 0 else ('short' if s.qty < 0 else None)

    if not pos_side:
        if signal and signal.strength > 0:
            open_count = get_open_position_count()
            balance = get_balance()
            max_pos = get_max_positions(balance)
            if open_count < max_pos:
                logger.info(f"⚡ [開倉確認] {sym} 準備市價開倉前確認 K 線...")
                # 融合 AI 與技術面的 Bypass Pullback 邏輯
                ai_conf = getattr(s, "ai_confidence", 0)
                ai_action = (getattr(s, "ai_action", None) or "HOLD").upper()
                is_ai_fresh = time.time() - getattr(s, "ai_updated_at", 0) < 1800
                
                # 技術面直通標準
                fast_path_threshold = STRATEGY_CONF["FAST_PATH_SCORE_LOW_VOL"] if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)" else STRATEGY_CONF["FAST_PATH_SCORE_HIGH_VOL"]
                tech_bypass = signal.strength >= fast_path_threshold
                
                # AI 賦能直通標準
                ai_bypass = is_ai_fresh and ai_conf >= 70 and ai_action == signal.side.upper()
                
                bypass_pullback = tech_bypass or ai_bypass
                
                if bypass_pullback:
                    if ai_bypass:
                        logger.info(f"⚡ [AI強勢直通] {sym} AI 置信度 {ai_conf}% 護航，允許市價進場前確認 K 線...")
                    else:
                        logger.info(f"⚡ [技術面直通] {sym} 技術分數達標，AI 無明顯干預，允許市價進場前確認 K 線...")
                        
                    confirmed = await wait_for_confirmation(sym, signal.side)
                    if confirmed:
                        await execute_order(sym, signal.side, s.close_price, signal.route, signal.is_ai, signal.strength)
                else:
                    logger.info(f"⏳ [保守掛單] {sym} 技術分數不足且無 AI 高自信背書，強制掛單等回撤 EMA20...")
                    target_price = s.ema20
                    if target_price and target_price > 0:
                        s.pending_entry = {
                            "side": signal.side,
                            "target_price": target_price,
                            "route": signal.route,
                            "is_ai": signal.is_ai,
                            "strength": signal.strength,
                            "expire_at": time.time() + 900
                        }
        return

    if not signal or pos_side == signal.side:
        await manage_position(sym)
        return

    hold_sec = time.time() - s.open_time if s.open_time > 0 else 9999
    avg = max(s.avg_price, 1e-8)
    p = s.close_price
    profit_pct = (p - avg) / avg if pos_side == 'long' else (avg - p) / avg

    if await should_reverse(sym, signal):
        await close_position(
            sym,
            'sell' if pos_side == 'long' else 'buy',
            abs(s.qty),
            p,
            avg,
            reason="反向訊號(Reverse)",
            is_stop_loss=(profit_pct < 0),
            is_profit=(profit_pct >= 0)
        )

        open_count = get_open_position_count()
        balance = get_balance()
        max_pos = get_max_positions(balance)
        if open_count < max_pos:
            logger.info(f"⚡ [反手開倉確認] {sym} 準備市價開倉前確認 K 線...")
            confirmed = await wait_for_confirmation(sym, signal.side)
            if confirmed:
                await execute_order(sym, signal.side, s.close_price, signal.route, signal.is_ai, signal.strength)
        return

    await manage_position(sym)

async def manage_position(sym):
    s = STATES[sym]
    if s.adjusted_this_tick:
        return
    if abs(s.qty) < 0.000001 or s.avg_price <= 0:
        update_trade_exit_advice(sym, None)
        return

    p = s.close_price
    avg = s.avg_price
    is_long = s.qty > 0
    profit_pct = (p - avg) / max(avg, 1e-8) if is_long else (avg - p) / max(avg, 1e-8)
    hold_sec = time.time() - s.open_time if s.open_time > 0 else 9999

    if profit_pct > s.highest_profit_pct:
        s.highest_profit_pct = profit_pct
    if profit_pct < 0:
        s.has_been_negative = True

    if p > s.trailing_highest:
        s.trailing_highest = p
    if p < s.trailing_lowest:
        s.trailing_lowest = p

    # ── 死水滯留自動平倉 (Stagnation Exit) ──
    STAGNATION_HOLD_SEC = 5400  # 持倉超過 90 分鐘
    STAGNATION_RANGE_PCT = 0.003  # 價格在開倉價 ±0.3% 內沒動
    adx_val = getattr(s, "adx", 99.0)

    if hold_sec > STAGNATION_HOLD_SEC and adx_val < 18.0:
        price_drift = abs(p - avg) / max(avg, 1e-8)
        if price_drift < STAGNATION_RANGE_PCT:
            close_side = 'sell' if is_long else 'buy'
            reason_msg = f"死水滯留平倉 (持倉 {hold_sec/60:.0f}min，震幅 {price_drift*100:.2f}%，ADX={adx_val:.1f})"
            logger.info(f"🧊 [死水滯留] {sym} {reason_msg}")
            await close_position(sym, close_side, abs(s.qty), p, avg, reason=reason_msg, is_stop_loss=False, is_profit=False)
            return

    exit_triggered = False
    exit_reason = ""
    is_stop_loss = False

    NORMAL_STOP_PCT = -max((s.current_atr * 2.0) / max(avg, 1e-8), 0.008)
    HARD_STOP_PCT = -0.025
    TAKE_PROFIT_PCT = abs(NORMAL_STOP_PCT) * 2.5

    if profit_pct >= 0.003:
        s.is_breakeven_set = True
    
    ai_conf = getattr(s, "ai_confidence", 0)
    ai_regime = (getattr(s, "ai_regime", None) or "CHOP")
    ai_action = (getattr(s, "ai_action", None) or "HOLD")
    
    if ai_regime == "CHOP" or ai_conf < 50:
        TRAIL_START_PCT = abs(NORMAL_STOP_PCT) * 1.0  # 提早啟動追蹤
        TRAIL_RETRACE_RATIO = 0.9  # 只容忍 10% 回撤
    else:
        TRAIL_START_PCT = abs(NORMAL_STOP_PCT) * 1.2  # 提早啟動追蹤
        if ai_conf >= 70 and ai_action.lower() == ('buy' if is_long else 'sell'):
            TRAIL_RETRACE_RATIO = 0.75  # 容忍 25% 回撤
        else:
            TRAIL_RETRACE_RATIO = 0.85  # 容忍 15% 回撤
    DYNAMIC_STOP_MIN = 0.003
    DYNAMIC_STOP_MAX = 0.006

    if profit_pct <= HARD_STOP_PCT:
        exit_triggered = True
        exit_reason = f"硬底線停損 (虧損達 {profit_pct*100:.2f}%)"
        is_stop_loss = True

    elif getattr(s, "is_breakeven_set", False):
        # 動態保本底線：最低 0.1%，隨著最高利潤往上拉，保持 0.2% 的距離
        dynamic_floor = max(0.001, s.highest_profit_pct - 0.002)
        if profit_pct <= dynamic_floor:
            exit_triggered = True
            exit_reason = f"動態保本防護 (最高 {s.highest_profit_pct*100:.2f}% -> 回落至 {profit_pct*100:.2f}%)"
            is_stop_loss = False

    elif hold_sec < 90:
        return

    elif hold_sec > 600 and abs(profit_pct) < 0.005:
        exit_triggered = True
        exit_reason = f"時限平倉 (橫盤 {hold_sec/60:.0f} 分鐘未突破成本區)"
        is_stop_loss = False

    elif profit_pct <= NORMAL_STOP_PCT:
        exit_triggered = True
        exit_reason = f"一般固定停損 (虧損達 {profit_pct*100:.2f}%)"
        is_stop_loss = True

    elif profit_pct >= TAKE_PROFIT_PCT:
        exit_triggered = True
        exit_reason = f"主停利達標 (獲利 {profit_pct*100:.2f}%)"
        is_stop_loss = False

    elif s.highest_profit_pct >= TRAIL_START_PCT:
        retrace_floor = s.highest_profit_pct * TRAIL_RETRACE_RATIO
        if profit_pct <= retrace_floor:
            exit_triggered = True
            exit_reason = f"追蹤停利回落 (高點 {s.highest_profit_pct*100:.2f}% -> 現在 {profit_pct*100:.2f}%)"
            is_stop_loss = False

    if not exit_triggered:
        dynamic_stop_pct = min(max(s.current_atr * 4.0 / avg, DYNAMIC_STOP_MIN), DYNAMIC_STOP_MAX)
        if profit_pct <= -dynamic_stop_pct:
            exit_triggered = True
            exit_reason = f"動態硬停損 (虧損達 {profit_pct*100:.2f}%，門檻 {dynamic_stop_pct*100:.2f}%)"
            is_stop_loss = True

    if not exit_triggered and profit_pct <= -0.01 and s.current_vol > getattr(s, "vol_ma20", 0) * 2.0:
        exit_triggered = True
        exit_reason = f"爆量洗盤急殺防禦 (虧損 {profit_pct*100:.2f}%)"
        is_stop_loss = True

    if not exit_triggered:
        return

    close_side = 'sell' if is_long else 'buy'
    await close_position(
        sym,
        close_side,
        abs(s.qty),
        p,
        avg,
        reason=exit_reason,
        is_stop_loss=is_stop_loss,
        is_profit=(not is_stop_loss)
    )



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
    
    # ── 1. 處理反向開倉 (Reverse Position) ──
    # 如果目前持有部位方向與新訊號方向相反，必須先明確平倉，再進行開倉
    if s.qty != 0:
        is_long = s.qty > 0
        is_reverse = (is_long and side == 'sell') or (not is_long and side == 'buy')
        if is_reverse:
            logger.info(f"🔄 [反向開倉] {sym} 訊號翻轉，先平原有{'多' if is_long else '空'}倉")
            profit_pct = (price - s.avg_price) / max(s.avg_price, 1e-8) if is_long else (s.avg_price - price) / max(s.avg_price, 1e-8)
            is_sl = profit_pct < 0
            await close_position(sym, side, abs(s.qty), price, s.avg_price, reason="反向開倉前平倉", is_stop_loss=is_sl, is_profit=not is_sl)
            await asyncio.sleep(0.5)  # 確保狀態重置
            # close_position 會呼叫 reset_coin_state，因此下方會將此次交易視為全新開倉
            s = STATES[sym]  # 重新取得最新的狀態物件

    pk = paper_key(sym)
    margin = compute_per_coin_margin()
    if margin <= 0:
        logger.warning(f"⚠️ [風控] {sym} 無可用保證金")
        return

    # ── 大盤豁免縮倉標記 (Half-Size Override) ──
    if getattr(s, "_half_size_override", False):
        margin *= 0.5
        s._half_size_override = False
        logger.info(f"⚖️ [縮倉豁免] {sym} 大盤逆風，倉位自動縮減至 50%")

    now = time.time()
    if s.entry_count > 0:
        if now - s.last_entry_time < s.entry_cooldown_sec:
            logger.info(f"⏳ [加倉冷卻] {sym} 距離上次加倉不足 {s.entry_cooldown_sec} 秒")
            return
        if s.entry_count >= s.max_additional_entries:
            logger.warning(f"⚠️ [加倉上限] {sym} 已達最大加倉次數")
            return
        if s.avg_price > 0 and price > 0:
            profit_pct = (price - s.avg_price) / max(s.avg_price, 1e-8) if side == 'buy' \
                         else (s.avg_price - price) / max(s.avg_price, 1e-8)

            # ── 獲利保護加倉 Pyramiding：ATR 動態門檻 ──
            atr_pct = s.current_atr / max(s.avg_price, 1e-8)
            required_profit = atr_pct * 1.0 if s.entry_count == 1 else atr_pct * 2.0
            required_profit = max(required_profit, 0.004)  # 至少 0.4% 才加倉

            if profit_pct < required_profit:
                logger.info(f"🛑 [順勢加碼風控] {sym} 獲利未達 {required_profit*100:.2f}% ATR門檻 (目前: {profit_pct*100:.2f}%)，禁止加倉")
                return

            # ✅ 獲利已達 1 ATR：自動拉保本線，加倉風險歸零
            if atr_pct > 0 and profit_pct >= atr_pct and not getattr(s, 'is_breakeven_set', False):
                s.is_breakeven_set = True
                logger.info(f"🔒 [保本鎖定] {sym} 獲利達 {profit_pct*100:.2f}%，止損自動移至開倉價")

    if GLOBAL_STATE["consecutive_losses"] >= 5:
        logger.warning(f"⚠️ [連敗風控] {sym} 全局已連續虧損 {GLOBAL_STATE['consecutive_losses']} 次，暫停開倉避免上頭")
        return

    # 最近 10 分鐘內停損超過 2 次，暫停開倉
    recent_stops = sum(
        1 for k, v in STATES.items()
        if getattr(v, "last_stop_loss_time", 0) > 0 
        and (time.time() - v.last_stop_loss_time) < 600
    )
    if recent_stops >= 2:
        logger.warning(f"⚠️ [連續停損風控] 最近10分鐘內全局已停損 {recent_stops} 次，觸發短線冷卻，暫停開倉")
        return

    # 大盤 1H 趨勢向下，暫停所有做多
    if side == 'buy' and MARKET_WIND.get("btc_1h_trend") == "DOWN":
        if route == "auto_reverse":
            logger.info(f"⚠️ [大盤空頭] BTC 1H趨勢向下，但 {sym} 是觸發停損反向交易，豁免大盤限制。")
        else:
            logger.warning(f"⚠️ [大盤空頭] BTC 1H趨勢向下，暫停做多開倉 ({sym})")
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
        # 智能動態倉位 (Dynamic Position Sizing) - 調整為純技術面模式，不再進行無 AI 縮倉
        size_multiplier = 1.0
        if strength >= 12.0:
            size_multiplier = 1.5
            logger.info(f"💎 [智能倉位] {sym} 訊號極強 (分數:{strength:.1f}) -> 放大倉位至 1.5 倍")
        else:
            logger.info(f"📊 [智能倉位] {sym} 正常技術開倉 (分數:{strength:.1f}) -> 使用標準倉量 1.0 倍")
            
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
            
            # 計算 RV 並記錄，幫助後續覆盤
            rv = s.current_vol / max(s.vol_ma20, 1e-8)
            print(f"🟢 [{direction}] {sym} {base_amt:.4f} @ {price} (保證金:{margin:.2f} USDT) [RV: {rv:.2f}]")
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
            if "recent_entries" not in GLOBAL_STATE:
                GLOBAL_STATE["recent_entries"] = []
            GLOBAL_STATE["recent_entries"].append(now)
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
            if upper_wick > candle_range * 0.5:
                logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 上影線超過整根K線 50%，做多危險")
                return False
            # 針對當前 K 線收跌，且上影線大於實體的情況
            if i == -1 and len(s.ohlcv) >= 2:
                prev_close = float(s.ohlcv[-2][4])
                if close_price < prev_close and upper_wick >= body:
                    logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 收跌且上影線過長，做多危險")
                    return False
        elif side == 'sell':
            if lower_wick > candle_range * 0.5:
                logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 下影線超過整根K線 50%，做空危險")
                return False
            # 針對當前 K 線收漲，且下影線大於實體的情況
            if i == -1 and len(s.ohlcv) >= 2:
                prev_close = float(s.ohlcv[-2][4])
                if close_price > prev_close and lower_wick >= body:
                    logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 收漲且下影線過長，做空危險")
                    return False

    return True


def is_entry_volume_confirmed(s):
    if len(s.ohlcv) < 20:
        return True
    
    # 計算相對成交量 (Relative Volume, RV)
    rv = s.current_vol / max(s.vol_ma20, 1e-8)
    
    # 鬆動門檻：RV 必須大於 1.0 倍的 20均量 才算有足夠成交量
    min_rv = 1.0 
    
    if rv < min_rv:
        return False
    return True


def check_direction_ok(sym, side, bypass_pullback=False, route="a"):
    """進場前全時區方向一致性總檢查 (Direction OK Gate)"""
    s = STATES[sym]
    
    # 大盤 1H (BTC regime)
    btc_trend = MARKET_WIND.get("btc_1h_trend", "CHOP")
    
    # 個幣 4H & 1H
    htf_4h = getattr(s, "htf_4h_trend", None)
    htf_1h = getattr(s, "htf_1h_trend", None)
    
    # VWAP 站上/跌破判斷
    vwap_ok = True
    if s.vwap > 0:
        if side == 'buy' and s.close_price < s.vwap:
            vwap_ok = False
        elif side == 'sell' and s.close_price > s.vwap:
            vwap_ok = False

    # EMA20 站上/跌破判斷
    ema_ok = True
    if s.ema20 > 0:
        if side == 'buy' and s.close_price < s.ema20:
            ema_ok = False
        elif side == 'sell' and s.close_price > s.ema20:
            ema_ok = False
            
    # MACD 順勢判斷 (15m is default)
    macd_ok = True
    if s.macd_line is not None and s.macd_signal is not None:
        if side == 'buy' and s.macd_line < s.macd_signal:
            macd_ok = False
        elif side == 'sell' and s.macd_line > s.macd_signal:
            macd_ok = False

    # 針對非左側抄底路由 (Route A) 進行把關 - 已放寬：僅記錄警告，不硬性攔截
    if route not in ['b', 'c', 's']:
        if side == 'buy':
            # 1H 同向, 4H 同向, BTC 不逆風 (非 DOWN), 價格站上 EMA20 或 VWAP
            if htf_1h != "long" or htf_4h != "long" or btc_trend == "DOWN" or (not ema_ok and not vwap_ok):
                logger.debug(f"@@COIN_DEBUG@@ ⚠️ {sym} 觸發 [Direction OK Gate] 右側做多條件未對齊 (1H:{htf_1h}, 4H:{htf_4h}, BTC:{btc_trend}, EMA/VWAP OK:{ema_ok}/{vwap_ok})，放行")
        else:
            # 1H 同向, 4H 同向, BTC 不逆風 (非 UP), 價格跌破 EMA20 或 VWAP
            if htf_1h != "short" or htf_4h != "short" or btc_trend == "UP" or (not ema_ok and not vwap_ok):
                logger.debug(f"@@COIN_DEBUG@@ ⚠️ {sym} 觸發 [Direction OK Gate] 右側做空條件未對齊 (1H:{htf_1h}, 4H:{htf_4h}, BTC:{btc_trend}, EMA/VWAP OK:{ema_ok}/{vwap_ok})，放行")
                
    elif bypass_pullback:
        # 左側抄底路由，但觸發了妖幣直通車 (雖然少見，但若發生則要求動能)
        if not vwap_ok or not macd_ok:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [Direction OK Gate] 左側強勢直通，但 VWAP({vwap_ok})/MACD({macd_ok}) 逆勢，拒絕開倉")
            return False
            
    return True


def risk_guard(sym, side, route="a"):
    if not GLOBAL_STATE["trading_enabled"]:
        return False

    market_regime = MARKET_WIND.get("market_regime", "NORMAL_CHOP")
    # 關閉大盤極端行情封鎖，允許機器人搶暴跌反彈或暴漲回調
    # if side == 'buy' and market_regime == "PANIC_BEAR":
    #     logger.info(f"🛑 {sym} 大盤處於極度恐慌(PANIC_BEAR)，禁止做多")
    #     return False
    # if side == 'sell' and market_regime == "RAGING_BULL":
    #     logger.info(f"🛑 {sym} 大盤處於狂暴牛市(RAGING_BULL)，禁止做空")
    #     return False

    is_trend = route == "a"
    if side == 'buy' and not MARKET_WIND.get("allow_long", True) and is_trend:
        # 原本直接封殺，現在改為：相對強度夠高就縮半倉放行
        s_rg = STATES[sym]
        rv = s_rg.current_vol / max(s_rg.avg_vol_24h_1m, 1e-8) if s_rg.avg_vol_24h_1m > 0 else 0
        if rv >= 4.0:
            logger.info(f"⚡ [大盤微跌豁免] {sym} RV={rv:.1f}x 超強，縮半倉允許開多")
            s_rg._half_size_override = True
        else:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤瀑布風控] 大盤異常跌勢，禁止順勢開多 (RV={rv:.1f}x 不足豁免)")
            return False
    if side == 'sell' and not MARKET_WIND.get("allow_short", True) and is_trend:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤上漲風控] 大盤異常漲勢，禁止順勢開空")
        return False

    # 剝頭皮模式：移除大盤 1H 與個幣 4H 的絕對禁止鎖
    # 短線交易只抓 15m 的波動，不看太遠的大週期
    if side == 'buy' and MARKET_WIND.get("btc_1h_trend") == "bear":
        logger.debug(f"⚠️ {sym} BTC 1H趨勢向下，但極致短線允許搶反彈")

    s = STATES[sym]
    
    if side == 'buy' and getattr(s, "htf_4h_trend", None) == "short":
        logger.debug(f"⚠️ {sym} 4H趨勢向下，但極致短線允許做多")
    if side == 'sell' and getattr(s, "htf_4h_trend", None) == "long":
        logger.debug(f"⚠️ {sym} 4H趨勢向上，但極致短線允許做空")
        
    cp = s.close_price

    if side == 'buy' and s.current_rsi > 75:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [RSI追高過濾] RSI={s.current_rsi:.1f} > 75，拒絕追高")
        return False
    if side == 'sell' and s.current_rsi < 25:
        logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [RSI超賣過濾] RSI={s.current_rsi:.1f} < 25，拒絕追空")
        return False
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
    
    min_atr_pct = 0.0005 if is_high_vol else 0.0003
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
        if avg_move < 0.0002:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [無動能過濾] 過去10K線平均振幅僅 {avg_move*100:.3f}% < 0.02%，判定無動能")
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

    # VWAP 乖離率防追高 (Anti-FOMO)
    if getattr(s, "vwap", 0) > 0:
        if side == 'buy' and cp > s.vwap * 1.015:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [VWAP防追高過濾] 價格 {cp:.4f} > VWAP {s.vwap:.4f} * 1.015，放棄追高多單")
            return False
        if side == 'sell' and cp < s.vwap * 0.985:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [VWAP防追空過濾] 價格 {cp:.4f} < VWAP {s.vwap:.4f} * 0.985，放棄追空空單")
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
    
    # ══════════════════════════════════════════
    # 多單中庸條件（只看 1H 趨勢與均線支撐）
    # ══════════════════════════════════════════
    if side == 'buy' and route == 'a':
        # 條件 1：1H 趨勢必須同向 (已放寬：改為警告日誌，不硬性封鎖)
        if getattr(s, "htf_trend", None) != "long":
            logger.info(f"⚠️ [多單過濾] {sym} 1H 趨勢非多頭 ({s.htf_trend})，放行但記錄")

        # 條件 2：價格必須站上 EMA20 或 VWAP
        price_above_ema20 = s.ema20 > 0 and cp >= s.ema20
        price_above_vwap = s.vwap > 0 and cp >= s.vwap
        if not price_above_ema20 and not price_above_vwap:
            logger.info(f"🛑 [多單過濾] {sym} 價格低於 EMA20 且低於 VWAP，禁止做多")
            return False

    # 空單中庸條件（只看 1H 趨勢與均線壓力）
    if side == 'sell' and route == 'a':
        # 條件 1：1H 趨勢必須同向 (已放寬：改為警告日誌，不硬性封鎖)
        if getattr(s, "htf_trend", None) != "short":
            logger.info(f"⚠️ [空單過濾] {sym} 1H 趨勢非空頭 ({s.htf_trend})，放行但記錄")

        # 條件 2：價格必須跌破 EMA20 且低於 VWAP
        price_below_ema20 = s.ema20 > 0 and cp <= s.ema20
        price_below_vwap = s.vwap > 0 and cp <= s.vwap
        if not price_below_ema20 and not price_below_vwap:
            logger.info(f"🛑 [空單過濾] {sym} 價格高於 EMA20 且高於 VWAP，禁止做空")
            return False
    # ══════════════════════════════════════════

    # 均線過濾器：僅限制 Route A (順勢) - 已放寬：改為警告日誌，不硬性封鎖
    if is_trend and s.ema50 > 0:
        ma_trend = s.ema50
        if side == 'buy' and cp <= ma_trend:
            logger.info(f"@@COIN_DEBUG@@ ⚠️ {sym} 價格 {cp:.4f} <= MA50 {ma_trend:.4f}，均線限制已放寬，放行開倉")
        if side == 'sell' and cp >= ma_trend:
            logger.info(f"@@COIN_DEBUG@@ ⚠️ {sym} 價格 {cp:.4f} >= MA50 {ma_trend:.4f}，均線限制已放寬，放行開倉")

    # 大週期 (HTF) 1H 趨勢過濾器：(已放寬：改為警告日誌，不硬性封鎖)
    htf_trend = s.htf_trend
    if htf_trend and is_trend:
        if side == 'buy' and htf_trend == 'short':
            logger.info(f"@@COIN_DEBUG@@ ⚠️ {sym} 1H 大週期趨勢為空頭，但趨勢限制已放寬，放行開多")
        if side == 'sell' and htf_trend == 'long':
            logger.info(f"@@COIN_DEBUG@@ ⚠️ {sym} 1H 大週期趨勢為多頭，但趨勢限制已放寬，放行開空")
            
    # ── 逆勢路由 HTF 方向過濾 ──────────────────────────────────
    if route in ['b', 'c', 's']:
        htf = s.htf_trend
        if side == 'buy' and htf == 'short' and s.current_rsi > 32.0:
            logger.info(
                f"@@COIN_DEBUG@@ ⚠️ {sym} [逆勢HTF過濾] "
                f"1H空頭+RSI={s.current_rsi:.1f}>32，但限制已放寬，放行逆勢做多"
            )
        if side == 'sell' and htf == 'long' and s.current_rsi < 68.0:
            logger.info(
                f"@@COIN_DEBUG@@ ⚠️ {sym} [逆勢HTF過濾] "
                f"1H多頭+RSI={s.current_rsi:.1f}<68，但限制已放寬，放行逆勢做空"
            )
    # 極端波動率過濾：當市場處於瘋狂洗盤、暴漲暴跌時，禁止任何逆勢搶短
    if not is_trend:
        atr_24h_avg = getattr(s, "atr_24h_avg", 0.0)
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
        
    # 量能確認過濾器 (已放寬：暫時關閉成交量最低門檻)
    # if route != "s" and not is_entry_volume_confirmed(s):
    #     return False
        
    # ADX 趨勢強度限制：僅限制順勢策略
    if is_trend:
        adx_val = getattr(s, "adx", None)
        if adx_val is not None and adx_val < STRATEGY_CONF["ADX_MIN_THRESHOLD"]:
            logger.debug(f"@@COIN_DEBUG@@ 🛑 {sym} ADX {adx_val:.1f} < {STRATEGY_CONF['ADX_MIN_THRESHOLD']}")
            return False
        elif adx_val is None:
            logger.warning(f"@@COIN_DEBUG@@ ⚠️ {sym} ADX 數值為 None，數據異常，拒絕進場並觸發告警。")
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
    btc_trend = MARKET_WIND.get("btc_1h_trend", "").lower()
    ohlcv = getattr(s, "ohlcv", None) or []
    if len(s.closes) > 0:
        closes = np.array(s.closes, dtype=float)
    elif ohlcv:
        closes = np.array([x[4] for x in ohlcv], dtype=float)
    else:
        closes = np.array([], dtype=float)
    if len(closes) < 20 and not ohlcv and s.close_price == 0.0 and (s.prev_close is None or s.prev_close == 0.0):
        return (None, 0.0, None, False)
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

    atr_24h_avg = getattr(s, "atr_24h_avg", 0.0)
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

    # 日誌減肥：只有在高波動模式，或者是 RSI 處於邊緣地帶才印出
    is_log_worthy = (vol_mode == "高波動模式 (High Vol)") or (rsi > 60 or rsi < 40)
    
    if is_log_worthy:
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

    if is_log_worthy:
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

    # Route B (Left Side / 逆勢反轉 / 買在極端低點)
    route_b_long = rsi < 30.0 and is_in_bb_zone_long
    route_b_short = rsi > 70.0 and is_in_bb_zone_short

    momentum_long = close > prev_close * 1.001 and (s.current_vol >= max(1000.0, s.vol_ma20 * 1.2) or s.trade_signal_strength > 0.2)
    momentum_short = close < prev_close * 0.999 and (s.current_vol >= max(1000.0, s.vol_ma20 * 1.2) or s.trade_signal_strength > 0.2)

    # Route C (Left Side / 搶極端反彈): 只要暴跌且出現K線反轉確認
    route_c_long = rsi <= 20.0 and last_candle_confirmed_long
    route_c_short = rsi >= 80.0 and last_candle_confirmed_short

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


    # --- 右側順勢突破 ---
    right_side_long = route_a_long or (momentum_long and last_candle_confirmed_long and (long_macd_cross or long_macd_hist_aligned) and trend_long)
    right_side_short = route_a_short or (momentum_short and last_candle_confirmed_short and (short_macd_cross or short_macd_hist_aligned) and trend_short)

    # === 微觀護城河 (Micro-Trend & RSI Filter) ===
    # 針對右側追高/追空：妖幣暴衝時 RSI 常突破 70，因此放寬到 80/20 才禁止追車
    is_rsi_safe_long = rsi < 80.0
    is_rsi_safe_short = rsi > 20.0
    
    if right_side_long and not is_rsi_safe_long:
        right_side_long = False
        logger.debug(f"⚠️ [微觀護城河] {sym} RSI過熱({rsi:.1f})，封殺右側追多訊號")
            
    if right_side_short and not is_rsi_safe_short:
        right_side_short = False
        logger.debug(f"⚠️ [微觀護城河] {sym} RSI過冷({rsi:.1f})，封殺右側追空訊號")

    # --- 左側買在低點 (不受 EMA50 限制) ---
    left_side_long = route_s_long or route_b_long or route_c_long
    left_side_short = route_s_short or route_b_short or route_c_short

    long_base_ok = right_side_long or left_side_long
    short_base_ok = right_side_short or left_side_short

    long_score = 0.0
    short_score = 0.0
    long_route = None
    short_route = None
    
    long_details = {}
    short_details = {}

    if long_base_ok:
        long_route = "s" if route_s_long else "c" if route_c_long else "b" if route_b_long else "a"
        if long_route == "a":
            base_score = 4.0 + ((close - ema20) / max(ema20, 1e-8) * 100) + 10.0
            long_score = base_score
            long_details["基礎"] = f"{base_score:.2f}"
        else:
            rsi_bonus = max(0.0, long_rsi_threshold - rsi)
            if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)":
                rsi_bonus *= 0.5
            # [修正] RSI 紅利不可無上限，最高 3.0 分，且逆勢時不給紅利
            if btc_trend == "bear" or s.trend_1h == "short" or s.trend_4h == "short":
                rsi_bonus = 0.0 # 逆大趨勢去摸底，不給紅利
            else:
                rsi_bonus = min(3.0, rsi_bonus)
                
            long_score = rsi_bonus + 4.0
            long_details["基礎"] = "4.0"
            if rsi_bonus > 0:
                long_details["RSI紅利"] = f"{rsi_bonus:.2f}"
        if momentum_long: 
            long_score += 3.0
            long_details["動能"] = "3.0"
        if long_macd_cross: 
            long_score += 5.0
            long_details["MACD"] = "5.0"

    if short_base_ok:
        short_route = "s" if route_s_short else "c" if route_c_short else "b" if route_b_short else "a"
        if short_route == "a":
            base_score = 4.0 + ((ema20 - close) / max(ema20, 1e-8) * 100) + 10.0
            short_score = base_score
            short_details["基礎"] = f"{base_score:.2f}"
        else:
            rsi_bonus = max(0.0, rsi - short_rsi_threshold)
            if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)":
                rsi_bonus *= 0.5
            # [修正] RSI 紅利不可無上限，最高 3.0 分，且逆勢時不給紅利
            if btc_trend == "bull" or s.trend_1h == "long" or s.trend_4h == "long":
                rsi_bonus = 0.0 # 逆大趨勢去摸頭，不給紅利
            else:
                rsi_bonus = min(3.0, rsi_bonus)
                
            short_score = rsi_bonus + 4.0
            short_details["基礎"] = "4.0"
            if rsi_bonus > 0:
                short_details["RSI紅利"] = f"{rsi_bonus:.2f}"
        if momentum_short: 
            short_score += 3.0
            short_details["動能"] = "3.0"
        if short_macd_cross: 
            short_score += 5.0
            short_details["MACD"] = "5.0"

    # 選分數高的方向，不再固定優先多單
    if long_score == 0 and short_score == 0:
        return (None, 0.0, None, False)

    # 綁定 ATR 動能
    current_atr = getattr(s, "current_atr", 0.0)
    atr_ma20 = getattr(s, "atr_ma20", current_atr)
    if atr_ma20 > 0:
        atr_modifier = 0.0
        if current_atr > atr_ma20:
            atr_modifier = 3.0
        elif current_atr < atr_ma20 * 0.8:
            atr_modifier = -5.0
            
        if long_score > 0:
            long_score = max(0.0, long_score + atr_modifier)
            long_details["ATR加權"] = f"{atr_modifier:+.1f}"
        if short_score > 0:
            short_score = max(0.0, short_score + atr_modifier)
            short_details["ATR加權"] = f"{atr_modifier:+.1f}"

    if long_score == 0 and short_score == 0:
        # 即使 ATR 將分數扣光，只要有初始訊號方向，我們仍保留 side 給「爆量直通車」與「大盤保護」最後一次強行過關的機會
        if long_base_ok and not short_base_ok:
            side, strength, route = "buy", 0.0, long_route
            details_str = " + ".join([f"{k}({v})" for k,v in long_details.items()]) if long_details else "無"
        elif short_base_ok and not long_base_ok:
            side, strength, route = "sell", 0.0, short_route
            details_str = " + ".join([f"{k}({v})" for k,v in short_details.items()]) if short_details else "無"
        else:
            return (None, 0.0, None, False)
    else:
        if short_score > long_score:
            side, strength, route = "sell", short_score, short_route
            details_str = " + ".join([f"{k}({v})" for k,v in short_details.items()])
        else:
            side, strength, route = "buy", long_score, long_route
            details_str = " + ".join([f"{k}({v})" for k,v in long_details.items()])

    # --- 以下為統一的進階風控 (套用於勝出方向) ---

    # 左側交易併發控制：如果不允許太多左側交易同時發生
    if route in ['b', 'c', 's'] and left_side_positions >= 2:  # 寬鬆：允許最多 2 個左側倉位
        logger.info(f"🛑 [風控] {sym} 觸發左側({route})，但目前已有 {left_side_positions} 個左側倉位，放棄開倉。")
        return (None, 0.0, None, False)

    # --- FNG 全局門檻偏移 ---
    fng = MARKET_WIND.get("fng_value", 50)
    if fng > 75:
        offset = 1.0 if side == 'sell' else -1.0
        strength += offset  # 貪婪：放寬做空，提高做多門檻
        details_str += f" + FNG貪婪({offset:+.1f})"
    elif fng < 25:
        offset = 1.0 if side == 'buy' else -1.0
        strength += offset   # 恐慌：放寬做多，提高做空門檻
        details_str += f" + FNG恐慌({offset:+.1f})"
        
    if strength > s.max_strength:
        s.max_strength = strength

    # === 快車道動能算分邏輯 ===
    if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)":
        strength -= 1.0
        details_str += " + 低波動(-1.0)"
        logger.debug(f"⚠️ {sym} 處於低波動模式，扣分 -1.0，防止指標虛胖。")
        
    # 2. 只有在真正爆量（current_atr > ma20 * 1.2）時，才給予動能加分
    if getattr(s, "atr_ma20", 0) > 0 and getattr(s, "current_atr", 0) > s.atr_ma20 * 1.2:
        strength += 3.0
        details_str += " + 爆量(+3.0)"

    fast_path_threshold = STRATEGY_CONF["FAST_PATH_SCORE_LOW_VOL"] if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)" else STRATEGY_CONF["FAST_PATH_SCORE_HIGH_VOL"]
    expected_trend = "long" if side == "buy" else "short"
    
    # === 新增：大盤連動保護 (BTC Master Wind) ===
    btc_change = MARKET_WIND.get("btc_change_15m", 0.0)
    if btc_change > 0.005 and side == "buy":
        logger.debug(f"📊 [分數明細] {sym} 總分: {max(strength, fast_path_threshold):.2f} | {details_str} + 大盤保護放行")
        logger.info(f"🚀 [大盤保護] BTC 15分鐘急漲 ({btc_change*100:.2f}%)，放行 {sym} 做多訊號")
        return (side, max(strength, fast_path_threshold), route, False)
    elif btc_change < -0.005 and side == "sell":
        logger.debug(f"📊 [分數明細] {sym} 總分: {max(strength, fast_path_threshold):.2f} | {details_str} + 大盤保護放行")
        logger.info(f"🚀 [大盤保護] BTC 15分鐘急跌 ({btc_change*100:.2f}%)，放行 {sym} 做空訊號")
        return (side, max(strength, fast_path_threshold), route, False)

    # === 新增：爆量直通車 (Volume Explosion Bypass) ===
    if s.avg_vol_24h_1m > 0 and s.current_vol > s.avg_vol_24h_1m * 3.0:
        if len(s.closes) >= 2:
            c_change = abs(s.closes[-1] - s.closes[-2]) / max(s.closes[-2], 1e-8)
            if c_change > 0.002:  # 實體大於 0.2%
                logger.debug(f"📊 [分數明細] {sym} 總分: {max(strength, fast_path_threshold):.2f} | {details_str} + 爆量直通")
                logger.info(f"💥 [爆量直通] {sym} 成交量瞬間放大至 {s.current_vol / s.avg_vol_24h_1m:.1f} 倍！無視規則直接進場！")
                return (side, max(strength, fast_path_threshold), route, False)
    # === 剝頭皮解鎖 (Scalping HTF & VWAP) ===
    # 在極致短線模式下，將鐵血雙鎖改為「扣分機制」，只要動能夠強依然允許進場
    
    # 1. 1H 大週期逆勢扣分
    if getattr(s, "htf_trend", None) and getattr(s, "htf_trend", None) != expected_trend:
        # 特例：如果是 AI 明確判斷的「反轉」模式，免扣分
        is_ai_reversal = getattr(s, "ai_setup", "") == "Reversal" and route in ["b", "c", "s"]
        if not is_ai_reversal:
            strength -= 3.0
            details_str += " + 大週期逆勢(-3.0)"
            logger.debug(f"⚠️ [大週期逆勢] {sym} 1H 趨勢 ({s.htf_trend}) 與開單方向 ({side}) 不符，短線扣分 (-3.0)")

    # 2. VWAP 機構成本線扣分
    if s.vwap and s.vwap > 0:
        if side == "buy" and s.close_price < s.vwap:
            strength -= 1.0
            details_str += " + VWAP逆勢(-1.0)"
            logger.debug(f"⚠️ [VWAP 逆勢] {sym} 價格低於 VWAP，短線做多扣分 (-1.0)")
        elif side == "sell" and s.close_price > s.vwap:
            strength -= 1.0
            details_str += " + VWAP逆勢(-1.0)"
            logger.debug(f"⚠️ [VWAP 逆勢] {sym} 價格高於 VWAP，短線做空扣分 (-1.0)")

    # 3. EMA50 微觀護城河壓力/支撐線扣分 (由硬封殺降級為扣分)
    if side == "buy" and not trend_confluence_long:
        strength -= 1.5
        details_str += " + 均線逆勢(-1.5)"
        logger.debug(f"⚠️ [均線逆勢] {sym} 價格低於 EMA50，短線做多扣分 (-1.5)")
    elif side == "sell" and not trend_confluence_short:
        strength -= 1.5
        details_str += " + 均線逆勢(-1.5)"
        logger.debug(f"⚠️ [均線逆勢] {sym} 價格高於 EMA50，短線做空扣分 (-1.5)")
            
    # 解除快車道的大週期限制
    fast_path_ok = route == "a" and strength >= fast_path_threshold
    if STRATEGY_CONF["FAST_PATH_REQUIRE_4H"] and getattr(s, "htf_4h_trend", None) != expected_trend:
        fast_path_ok = False
        
    if fast_path_ok:
        # 新增：AI 訊號老化懲罰 (Old AI Penalty)
        ai_updated_at = getattr(s, "ai_updated_at", 0.0)
        if ai_updated_at > 0.0:
            ai_age = time.time() - ai_updated_at
            ai_age_msg = f"已老化 ({ai_age/60:.1f}m)"
        else:
            ai_age = float('inf')
            ai_age_msg = "尚未取得最新審查"

        if ai_age > 600:  # AI 訊號超過 10 分鐘或未取得
            ema20 = s.ema20
            if ema20 and ema20 > 0:
                if (side == "buy" and s.close_price <= ema20) or (side == "sell" and s.close_price >= ema20):
                    logger.info(f"⏳ [快車道降級] {sym} 雖然達標，但 AI 訊號{ai_age_msg}，且未站穩 EMA20，降級為慢速路徑！")
                    fast_path_ok = False

    if fast_path_ok:
        logger.debug(f"📊 [分數明細] {sym} 總分: {strength:.2f} | {details_str}")
        logger.info(f"⚡ [Fast Path] {sym} 觸發做{side}直通 (Score: {strength:.2f} >= {fast_path_threshold})，無須等待 AI！")
        return (side, strength, route, False)  # is_ai_assisted=False
    
    # 計算送交 AI 的基本門檻
    ai_trigger_threshold = STRATEGY_CONF.get("AI_TRIGGER_SCORE_LOW_VOL", 5.0) if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)" else STRATEGY_CONF.get("AI_TRIGGER_SCORE_HIGH_VOL", 6.0)

    # === 新增：右側突破二次確認模式 (No-AI Trend Follower) ===
    # 條件：分數達標 + 大週期完全順風 + 短線突破且站穩 + RSI 健康
    if strength >= ai_trigger_threshold:
        htf_ok = getattr(s, "htf_trend", None) == expected_trend and getattr(s, "htf_4h_trend", None) == expected_trend
        if htf_ok and len(s.closes) >= 2:
            c1, c2 = s.closes[-2], s.closes[-1]
            ema20 = s.ema20
            if ema20 and ema20 > 0:
                if side == "buy" and c1 > ema20 and c2 > ema20 and s.current_rsi > 50:
                    logger.debug(f"📊 [分數明細] {sym} 總分: {strength:.2f} | {details_str}")
                    logger.info(f"📈 [二次確認突破] {sym} 1H/4H順風且連續兩根K線站穩 EMA20，RSI>50，不須 AI 同意直接開多！")
                    return (side, strength, route, False)
                elif side == "sell" and c1 < ema20 and c2 < ema20 and s.current_rsi < 50:
                    logger.debug(f"📊 [分數明細] {sym} 總分: {strength:.2f} | {details_str}")
                    logger.info(f"📉 [二次確認突破] {sym} 1H/4H順風且連續兩根K線跌破 EMA20，RSI<50，不須 AI 同意直接開空！")
                    return (side, strength, route, False)

    if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)":
        TECHNICAL_ENTRY_THRESHOLD = 12.0
    else:
        TECHNICAL_ENTRY_THRESHOLD = 7.5
    
    # 若分數未達鬆動後的門檻，直接過濾掉假突破
    if strength < TECHNICAL_ENTRY_THRESHOLD:
        return (None, 0.0, None, False)

    # ── 多時區共振快速通道 (MTF Confluence Fast Path) ──
    htf_1h = getattr(s, "htf_trend", None)
    htf_4h = getattr(s, "htf_4h_trend", None)
    expected = "long" if side == "buy" else "short"

    is_mtf_aligned = (htf_1h == expected and htf_4h == expected)
    is_vol_spike = s.avg_vol_24h_1m > 0 and s.current_vol > s.avg_vol_24h_1m * 2.0

    if is_mtf_aligned and is_vol_spike:
        MTF_THRESHOLD = 5.0   # 共振時降低門檻（原本 7.5）
        if strength >= MTF_THRESHOLD:
            logger.info(
                f"🌊 [MTF共振通道] {sym} 1H/4H/1m 三層順風 + 爆量 "
                f"(Vol: {s.current_vol/s.avg_vol_24h_1m:.1f}x)，"
                f"門檻降至 {MTF_THRESHOLD}，Score={strength:.2f} 直接放行！"
            )
            return (side, strength, route, False)

    # ==========================================
    # 🚀 極致短線模式：純技術面高標進場
    # 只要指標分數達標 (>= 7.5)，代表確認為強勢訊號，直接開槍進場！
    # ==========================================
    logger.debug(f"📊 [分數明細] {sym} 總分: {strength:.2f} | {details_str}")
    logger.info(f"⚡ [純技術面觸發] {sym} 訊號達標 (Score: {strength:.2f} >= {TECHNICAL_ENTRY_THRESHOLD})，直接開倉！")
    return (side, strength, route, False)

async def process_symbols():
    load_strategy_config()
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
        await check_pending_entries()
        
        # 產生訊號
        signals = await generate_signals()
        
        # 統一處理訊號與部位管理
        for sym in ALL_SYMBOLS:
            await process_signal(sym, signals.get(sym))

async def check_pending_entries():
    if not GLOBAL_STATE["trading_enabled"]:
        return
        
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        pe = s.pending_entry
        if not pe:
            continue
            
        if time.time() > pe["expire_at"]:
            logger.info(f"⏳ [掛單過期] {sym} 等待回撤超過 15 分鐘未觸發，取消掛單。")
            s.pending_entry = None
            continue
            
        cp = s.close_price
        side = pe["side"]
        target = pe["target_price"]
        
        # 判斷是否觸發 (Buy -> 價格跌到目標價以下; Sell -> 價格漲到目標價以上)
        triggered = False
        if side == "buy" and cp <= target:
            triggered = True
        elif side == "sell" and cp >= target:
            triggered = True
            
        if triggered:
            if not check_direction_ok(sym, side, bypass_pullback=False):
                logger.info(f"🛑 [掛單觸發否決] {sym} 價格雖達目標，但多時區方向已轉變，取消掛單。")
                s.pending_entry = None
                continue
                
            logger.info(f"🎯 [掛單觸發] {sym} 價格已回到目標 {target:.4f} (目前 {cp:.4f})，準備市價進場！")
            open_count = get_open_position_count()
            balance = get_balance()
            max_pos = get_max_positions(balance)
            if open_count < max_pos:
                await execute_order(sym, side, cp, pe["route"], pe["is_ai"], pe["strength"])
            else:
                logger.warning(f"⚠️ [掛單觸發失敗] {sym} 倉位已滿，放棄掛單。")
            s.pending_entry = None


async def wait_for_confirmation(sym, side):
    """等待下一根 K 線收盤確認方向"""
    s = STATES[sym]
    await asyncio.sleep(10)  # 等 10 秒讓當前 K 線繼續發展
    
    # 重新抓最新 K 線
    try:
        res = await asyncio.wait_for(exchange.fetch_ohlcv(sym, '1m', limit=3), timeout=5)
        if res and len(res) >= 2:
            latest_close = res[-1][4]
            prev_close = res[-2][4]
            
            # 確認方向還在
            if side == 'buy' and latest_close <= prev_close:
                logger.info(f"🛑 [方向確認失敗] {sym} 等待期間未創高，取消做多")
                return False
            if side == 'sell' and latest_close >= prev_close:
                logger.info(f"🛑 [方向確認失敗] {sym} 等待期間未創低，取消做空")
                return False
    except:
        pass
    return True


async def generate_signals() -> dict:
    signals = {}
    if not GLOBAL_STATE["trading_enabled"]:
        return signals
        
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
            if profit < -0.01:
                losing_count += 1
    # --------------------
    
    now = time.time()
    GLOBAL_STATE["recent_entries"] = [t for t in GLOBAL_STATE.get("recent_entries", []) if now - t < 900]
    concurrent_limit_reached = len(GLOBAL_STATE["recent_entries"]) >= 2

    candidates = []
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s.status != "ACTIVE" or getattr(s, "adjusted_this_tick", False):
            continue
            
        # 停損冷卻：連虧兩次鎖 1 小時
        if (getattr(s, "stop_loss_count", 0) or 0) >= 2:
            cooldown = 3600
        else:
            cooldown = 45 # default
            
        if time.time() < getattr(s, "last_exit_time", 0) + cooldown:
            continue
            
        side_strength = compute_signal_strength(sym)
        if side_strength[0] is None:
            continue
            
        side, strength, route, is_ai = side_strength
        if strength <= 0.0:
            continue
            
        # 0. 盤整過濾 (CHOP Filter)
        ai_regime = getattr(s, "ai_regime", "CHOP")
        if ai_regime == "CHOP":
            profit_potential = s.current_atr / max(s.close_price, 1e-8)
            if profit_potential < 0.0015:
                logger.debug(f"🛑 [進場過濾] {sym} 處於盤整且波幅過小 (ATR: {profit_potential*100:.2f}% < 0.15%)，拒絕進場")
                continue
            
        # 1. 強制 HTF 同向 (逆勢單全面封殺)
        is_counter_trend = False
        htf = getattr(s, "htf_trend", None)
        htf4h = getattr(s, "htf_4h_trend", None)
        if (side == "buy" and (htf == "short" or htf4h == "short")):
            is_counter_trend = True
        elif (side == "sell" and (htf == "long" or htf4h == "long")):
            is_counter_trend = True
            
        if is_counter_trend:
            logger.debug(f"⚠️ [進場過濾] {sym} 大週期逆勢 (1H:{htf}, 4H:{htf4h})，扣分但放行")
            strength -= 3.0
            if strength <= 0:
                continue
            
        # 區分大幣與小幣 (Altcoins) 的防護等級
        is_altcoin = sym not in ["BTCUSDT", "ETHUSDT"]
        
        if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)":
            vol_multi = 1.1 if is_altcoin else 1.0  # 放寬門檻：低波動模式不需太高量突破 (原 1.5/1.2)
        else:
            vol_multi = 1.2 if is_altcoin else 1.0  # 高波動/正常模式維持不變
        hugging_pct = 0.3 if is_altcoin else 0.2
        ai_conf_threshold = 70 if is_altcoin else 60

        # 2. 強制量能突破 (低波動模式放寬至 1.1x，高波動維持 1.2x)
        #    例外：訊號強度 >= 18 分時豁免量能過濾（非常強的訊號）
        vol_req = getattr(s, "vol_ma20", 0) * vol_multi
        prev_vol = float(s.ohlcv[-2][5]) if getattr(s, "ohlcv", []) and len(s.ohlcv) >= 2 else getattr(s, "current_vol", 0.0)
        if prev_vol <= vol_req:
            if strength >= 18.0:
                logger.debug(f"⚡ [量能豁免] {sym} 訊號強度 {strength:.2f} >= 18，豁免量能門檻 ({vol_multi}x MA20)")
            else:
                logger.debug(f"🛑 [進場過濾] {sym} 拒絕進場：無量假突破 (PrevVol < {vol_multi}x MA20, 分數 {strength:.2f} < 18)")
                continue
            
        # 3. 拒絕均線沾毛 (Anti-Hugging)
        if s.ema20 and s.ema20 > 0:
            ema_dist = abs(s.close_price - s.ema20) / s.ema20 * 100
            if ema_dist < hugging_pct:
                logger.debug(f"🛑 [進場過濾] {sym} 拒絕進場：價格過於貼近 EMA20 (乖離率 {ema_dist:.2f}% < {hugging_pct}%)")
                continue
                
        # 4. VWAP 空間擋板
        if getattr(s, "vwap", 0) > 0:
            vwap_dist = abs(s.close_price - s.vwap) / s.vwap * 100
            if vwap_dist < hugging_pct:
                logger.debug(f"🛑 [進場過濾] {sym} 拒絕進場：價格過於貼近 VWAP (乖離率 {vwap_dist:.2f}% < {hugging_pct}%)")
                continue
                
        # 4.5. 突破後回踩與 FOMO 追高防護 (拒絕買在針尖上)
        rsi = getattr(s, "current_rsi", 50)
        
        # [防禦 1] K棒型態過濾 (拒絕長影線)
        if getattr(s, "ohlcv", []) and len(s.ohlcv) >= 1:
            last_candle = s.ohlcv[-1]
            c_open = float(last_candle[1])
            c_high = float(last_candle[2])
            c_low = float(last_candle[3])
            c_close = float(last_candle[4])
            c_len = c_high - c_low
            if c_len > 0:
                if side == "buy":
                    upper_wick = c_high - max(c_open, c_close)
                    if (upper_wick / c_len) > 0.4:
                        logger.debug(f"🛑 [進場過濾] {sym} 拒絕進場：上影線過長 ({upper_wick/c_len*100:.0f}%)，大戶賣壓沉重")
                        continue
                else:
                    lower_wick = min(c_open, c_close) - c_low
                    if (lower_wick / c_len) > 0.4:
                        logger.debug(f"🛑 [進場過濾] {sym} 拒絕進場：下影線過長 ({lower_wick/c_len*100:.0f}%)，底部買盤強勁")
                        continue
                        
        # [防禦 2 & 3] 布林通道天花板過濾 & RSI 收緊 (小幣專用)
        if is_altcoin:
            if side == "buy":
                if rsi > 70:
                    logger.debug(f"🛑 [進場過濾] {sym} 拒絕進場：RSI 過高 ({rsi:.1f} > 70)，防範 FOMO 追高")
                    continue
                if getattr(s, "bb_up", 0) > 0 and s.close_price >= s.bb_up * 1.010:
                    logger.debug(f"🛑 [進場過濾] {sym} 拒絕進場：價格突破布林上軌超過 1%，防範過度追高")
                    continue
            elif side == "sell":
                if rsi < 30:
                    logger.debug(f"🛑 [進場過濾] {sym} 拒絕進場：RSI 過低 ({rsi:.1f} < 30)，防範 FOMO 殺跌")
                    continue
                if getattr(s, "bb_low", 0) > 0 and s.close_price <= s.bb_low * 0.990:
                    logger.debug(f"🛑 [進場過濾] {sym} 拒絕進場：價格跌破布林下軌超過 1%，防範過度殺跌")
                    continue

        # 5. AI 絕對否決權
        ai_conf = getattr(s, "ai_confidence", 0)
        ai_action = (getattr(s, "ai_action", None) or "HOLD").upper()
        is_ai_fresh = time.time() - getattr(s, "ai_updated_at", 0) < 1800
        
        if is_ai_fresh:
            if ai_conf < ai_conf_threshold:
                logger.debug(f"🛑 [進場過濾] {sym} 拒絕進場：AI 置信度過低 ({ai_conf} < {ai_conf_threshold})")
                continue
            if (side == "buy" and ai_action == "SELL") or (side == "sell" and ai_action == "BUY"):
                # Conflict: downgrade
                strength -= 3.0
                logger.debug(f"⚠️ [AI降權] {sym} 訊號與 AI 衝突 (AI: {ai_action} {ai_conf}%)，分數降至 {strength:.1f}")
        
        if strength <= 0.0:
            continue

        if not risk_guard(sym, side, route):
            continue
            
        candidates.append((sym, side, strength, route, is_ai))

    candidates.sort(key=lambda x: -x[2])
    
    for sym, side, strength, route, is_ai in candidates:
        sig = Signal(
            symbol=sym,
            side=side,
            strength=strength,
            route=route,
            is_ai=is_ai,
            reverse_confirmed=True,
            reason=f"Strength {strength:.2f}"
        )
        
        s = STATES[sym]
        if abs(s.qty) < 0.000001 and concurrent_limit_reached:
            continue
            
        signals[sym] = sig

    return signals

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
                for sym in ALL_SYMBOLS:
                    if sym not in STATES: continue
                    if sym in SLOW_OR_LOW_QUALITY_SYMBOLS: continue
                    s = STATES[sym]
                    
                    reason = ""
                    # 只有高價值標的才掃描
                    if abs(s.qty) > 0.000001:
                        reason = "POSITION_MANAGEMENT"
                    else:
                        ai_trigger_threshold = STRATEGY_CONF["AI_TRIGGER_SCORE_LOW_VOL"] if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)" else STRATEGY_CONF["AI_TRIGGER_SCORE_HIGH_VOL"]
                        is_high_vol = getattr(s, "vol_mode", "") == "高波動模式 (High Vol)"
                        has_conflict = (s.max_strength >= 5.0) and ((s.htf_trend == "long" and s.htf_4h_trend == "short") or (s.htf_trend == "short" and s.htf_4h_trend == "long"))
                        
                        if s.max_strength >= ai_trigger_threshold - 1.0:
                            reason = f"EDGE_SCORE({s.max_strength:.1f})"
                        elif is_high_vol and s.max_strength >= 5.0:
                            reason = "HIGH_VOL_CANDIDATE"
                        elif has_conflict:
                            reason = "HTF_CONFLICT"
                        
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
