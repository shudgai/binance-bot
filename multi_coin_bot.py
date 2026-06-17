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

LOCK_FILE = "/tmp/binance_bot_single_instance.lock"
lock_file_handle = None

def ensure_single_instance():
    global lock_file_handle
    lock_file_handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("🚨 錯誤: 偵測到系統中已有另一個機器人正在執行！")
        print("💡 為了避免重複下單與邏輯衝突，本次啟動已自動攔截並退出。")
        print("💡 提示: 請確保只透過網頁儀表板來啟動，不要在終端機重複手動啟動。")
        sys.exit(1)

ensure_single_instance()

exchange = ccxtpro.binance({
    'apiKey': os.getenv('BINANCE_API_KEY') or None,
    'secret': os.getenv('BINANCE_API_SECRET') or None,
    'enableRateLimit': True,
    'rateLimit': 1000,
    'options': {
        'defaultType': 'future',
        'watchOrderBookSnapshot': True,
    },
})
USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
PAPER_TRADING = True
TIMEFRAME = '1m'
LEVERAGE = 5
RSI_PERIOD = 9
VOLUME_RATIO_THRESHOLD = 1.2
ATR_WARMUP_BATCH_SIZE = 2
ATR_WARMUP_SYMBOL_COUNT = 12
ATR_WARMUP_LIMIT = 1000
ATR_WARMUP_PAUSE_SEC = 0.4

if USE_TESTNET:
    exchange.urls['api']['fapiPublic'] = 'https://testnet.binancefuture.com/fapi/v1'
    exchange.urls['api']['fapiPrivate'] = 'https://testnet.binancefuture.com/fapi/v1'

DEFAULT_SYMBOLS = [
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT",
    "DOTUSDT", "UNIUSDT", "NEARUSDT", "FETUSUSDT", "SUIUSDT"
]
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "bot_symbols.json")

PERSONALITY_TEMPLATES = {
    "calm": {
        "personality": "calm",
        "risk_multiplier": 0.8,
        "volume_multiplier": 0.9,
        "entry_cooldown_sec": 120,
        "max_additional_entries": 1,
        "entry_size_pct": 0.4,
        "add_entry_pct": 0.2,
        "sl_atr_multiplier": 4.5,
        "tp_atr_multiplier": 1.6,
        "hard_stop_loss_pct": 0.035,
    },
    "balanced": {
        "personality": "balanced",
        "risk_multiplier": 1.0,
        "volume_multiplier": 1.0,
        "entry_cooldown_sec": 90,
        "max_additional_entries": 2,
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "sl_atr_multiplier": 4.0,
        "tp_atr_multiplier": 1.5,
        "hard_stop_loss_pct": 0.03,
    },
    "aggressive": {
        "personality": "aggressive",
        "risk_multiplier": 1.15,
        "volume_multiplier": 1.1,
        "entry_cooldown_sec": 60,
        "max_additional_entries": 3,
        "entry_size_pct": 0.6,
        "add_entry_pct": 0.35,
        "sl_atr_multiplier": 3.5,
        "tp_atr_multiplier": 1.3,
        "hard_stop_loss_pct": 0.025,
    },
    "adaptive": {
        "personality": "adaptive",
        "risk_multiplier": 1.0,
        "volume_multiplier": 1.0,
        "entry_cooldown_sec": 90,
        "max_additional_entries": 2,
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "sl_atr_multiplier": 4.0,
        "tp_atr_multiplier": 1.4,
        "hard_stop_loss_pct": 0.03,
    },
}

SYMBOL_EXIT_OVERRIDES = {
    "HUSDT": {
        "tp_atr_multiplier": 1.8,
        "sl_atr_multiplier": 5.0,
    },
}

DEFAULT_REVERSAL_SETTINGS = {
    "trade_signal_threshold": 1.8,
    "volume_multiplier": 3.0,
    "price_jump_pct": 0.01,
    "min_reverse_pct": 0.008,
}

SYMBOL_REVERSAL_SETTINGS = {
    "XRPUSDT": {
        "trade_signal_threshold": 2.5,
        "volume_multiplier": 3.5,
        "price_jump_pct": 0.012,
        "min_reverse_pct": 0.01,
    },
}


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


def load_symbol_config():
    global SYMBOL_EXIT_OVERRIDES, SYMBOL_REVERSAL_SETTINGS
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        symbols = []
        profiles = {}
        exit_overrides = {}
        reversal_settings = {}
        if isinstance(data, dict):
            symbols = normalize_symbol_list(data.get("symbols", []))
            raw_profiles = data.get("profiles", {})
            if isinstance(raw_profiles, dict):
                for sym, profile in raw_profiles.items():
                    normalized = normalize_symbol(sym)
                    if not normalized or not isinstance(profile, dict):
                        continue
                    profile_copy = dict(profile)
                    overrides = profile_copy.get("exit_overrides")
                    if isinstance(overrides, dict):
                        exit_overrides[normalized] = overrides
                    settings = profile_copy.get("reversal_settings")
                    if isinstance(settings, dict):
                        reversal_settings[normalized] = settings
                    profiles[normalized] = profile_copy
        SYMBOL_EXIT_OVERRIDES = exit_overrides
        SYMBOL_REVERSAL_SETTINGS = reversal_settings
        return symbols or list(DEFAULT_SYMBOLS), profiles
    except FileNotFoundError:
        return list(DEFAULT_SYMBOLS), {}
    except Exception as e:
        print(f"⚠️ 讀取幣種清單失敗: {e}")
        return list(DEFAULT_SYMBOLS), {}


def load_symbol_pool():
    symbols, _ = load_symbol_config()
    return symbols


def load_symbol_profiles():
    _, profiles = load_symbol_config()
    return profiles


def save_symbol_pool(symbols):
    normalized = normalize_symbol_list(symbols)
    profiles = load_symbol_profiles()
    payload = {"symbols": normalized}
    if profiles:
        payload["profiles"] = profiles
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return normalized


def save_symbol_profiles(profiles):
    symbols = load_symbol_pool()
    normalized_profiles = {}
    for sym, profile in (profiles or {}).items():
        normalized = normalize_symbol(sym)
        if normalized and isinstance(profile, dict):
            normalized_profiles[normalized] = profile
    payload = {"symbols": symbols, "profiles": normalized_profiles}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return normalized_profiles


def infer_symbol_personality(sym):
    if sym in ("BTCUSDT", "ETHUSDT"):
        return "calm"
    if sym == "HUSDT":
        return "calm"
    aggressive_coins = {"DOGEUSDT", "PEPEUSDT", "SHIBUSDT", "XRPUSDT", "SANDUSDT", "MANAUSDT", "FIOUSDT"}
    balanced_coins = {"ADAUSDT", "SOLUSDT", "LINKUSDT", "AVAXUSDT", "UNIUSDT", "NEARUSDT", "SUIUSDT"}
    if sym in aggressive_coins:
        return "aggressive"
    if sym in balanced_coins:
        return "balanced"
    return "adaptive"


def get_personality_template(personality):
    if not personality:
        return PERSONALITY_TEMPLATES["balanced"]
    return PERSONALITY_TEMPLATES.get(personality.lower(), PERSONALITY_TEMPLATES["balanced"])


def get_symbol_exit_override(sym):
    return SYMBOL_EXIT_OVERRIDES.get(sym, {})


def should_require_strong_exit(overrides):
    return bool(
        overrides.get("require_strong_momentum")
        or overrides.get("volume_threshold") is not None
        or overrides.get("momentum_threshold") is not None
    )


def is_strong_exit_condition(sym, is_long):
    overrides = get_symbol_exit_override(sym)
    if not overrides:
        return False
    if overrides.get("require_strong_momentum"):
        return has_strong_momentum(sym, is_long)
    volume_threshold = overrides.get("volume_threshold")
    momentum_threshold = overrides.get("momentum_threshold")
    s = STATES[sym]
    if s.get("vol_ma20", 0.0) <= 0 or len(s.get("closes", [])) < 4:
        return False
    volume_ratio = s["current_vol"] / max(s["vol_ma20"], 1e-8)
    recent_return = abs((s["closes"][-1] - s["closes"][-4]) / max(abs(s["closes"][-4]), 1e-8))
    if volume_threshold is not None and volume_ratio < float(volume_threshold):
        return False
    if momentum_threshold is not None and recent_return < float(momentum_threshold):
        return False
    return True


def get_effective_exit_setting(sym, key, base_value, is_long):
    overrides = get_symbol_exit_override(sym)
    if not overrides:
        return base_value
    value = overrides.get(key)
    if not isinstance(value, (int, float)):
        return base_value
    if key == "tp_atr_multiplier":
        if should_require_strong_exit(overrides) and not is_strong_exit_condition(sym, is_long):
            return base_value
        return value
    if key in ("sl_atr_multiplier", "hard_stop_loss_pct"):
        if value > base_value:
            return base_value
    return value


def apply_symbol_profile(sym, profile):
    if sym not in STATES:
        return
    state = STATES[sym]
    if isinstance(profile, str):
        profile = {"personality": profile}
    personality = profile.get("personality") or state.get("personality") or infer_symbol_personality(sym)
    personality_source = "manual" if profile.get("personality") else state.get("personality_source", "infer")
    template = get_personality_template(personality)
    state.update(template)
    for key in [
        "risk_multiplier", "volume_multiplier", "entry_cooldown_sec",
        "max_additional_entries", "entry_size_pct", "add_entry_pct",
        "sl_atr_multiplier", "tp_atr_multiplier", "hard_stop_loss_pct",
    ]:
        if key in profile and isinstance(profile[key], (int, float)):
            state[key] = profile[key]
    state["personality"] = personality
    state["personality_source"] = personality_source
    if personality_source == "manual":
        state["last_personality_update"] = time.time()


def apply_all_symbol_profiles():
    for sym in ALL_SYMBOLS:
        profile = SYMBOL_PROFILES.get(sym, {})
        apply_symbol_profile(sym, profile)


def has_manual_personality(sym):
    profile = SYMBOL_PROFILES.get(sym, {})
    return isinstance(profile, dict) and "personality" in profile


def evaluate_dynamic_personality(sym):
    s = STATES[sym]
    if s["current_atr"] <= 0 or s["vol_ma20"] <= 0 or len(s["ohlcv"]) < 20:
        return s.get("personality", "balanced")

    close = s["close_price"]
    atr_pct = s["current_atr"] / max(close, 1e-8)
    volume_ratio = s["current_vol"] / max(s["vol_ma20"], 1e-8)
    recent_candles = s["ohlcv"][-20:]
    highs = np.array([x[2] for x in recent_candles])
    lows = np.array([x[3] for x in recent_candles])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / max(recent_low, 1e-8)
    rsi = s.get("current_rsi", 50.0)
    macd_hist = s.get("macd_hist", 0.0)

    quiet_market = volume_ratio < 1.15 and atr_pct < 0.008 and range_width_pct < 0.02
    high_volatility = volume_ratio > 1.9 or atr_pct > 0.02 or range_width_pct > 0.04
    strong_trend = abs(rsi - 50.0) > 12.0 or abs(macd_hist) > close * 0.0006

    if quiet_market:
        return "calm"
    if high_volatility or strong_trend:
        return "aggressive"
    if range_width_pct >= 0.03 or abs(rsi - 50.0) > 8.0:
        return "balanced"
    return "adaptive"


def measure_personality_traits(sym):
    s = STATES[sym]
    close = max(s.get("close_price", 0.0), 1e-8)
    atr_pct = s.get("current_atr", 0.0) / close
    volume_ratio = s.get("current_vol", 0.0) / max(s.get("vol_ma20", 1e-8), 1e-8)
    recent_candles = s.get("ohlcv", [])[-20:]
    highs = np.array([x[2] for x in recent_candles]) if recent_candles else np.array([close])
    lows = np.array([x[3] for x in recent_candles]) if recent_candles else np.array([close])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / max(recent_low, 1e-8)
    rsi = s.get("current_rsi", 50.0)
    return volume_ratio, atr_pct, rsi, range_width_pct


def update_dynamic_personality(sym):
    if has_manual_personality(sym):
        return False
    s = STATES[sym]
    new_personality = evaluate_dynamic_personality(sym)
    old_personality = s.get("personality", "balanced")
    if new_personality == old_personality and s.get("personality_source") == "dynamic":
        s["last_personality_update"] = time.time()
        return False
    if new_personality != old_personality:
        s.update(get_personality_template(new_personality))
        s["personality"] = new_personality
        s["personality_source"] = "dynamic"
        s["last_personality_update"] = time.time()
        volume_ratio, atr_pct, rsi, range_width_pct = measure_personality_traits(sym)
        print(f"🔧 [動態個性] {sym} 由 {old_personality} 變更為 {new_personality} | vol={volume_ratio:.2f} atr_pct={atr_pct:.4f} rsi={rsi:.1f} range={range_width_pct:.3f}")
        return True
    return False


def update_all_dynamic_personalities():
    now = time.time()
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if has_manual_personality(sym):
            continue
        if now - s.get("last_personality_update", 0.0) < 300:
            continue
        update_dynamic_personality(sym)


ALL_SYMBOLS, SYMBOL_PROFILES = load_symbol_config()

MAX_POSITIONS = 2
COOLDOWN_SEC = 300
MAIN_LOOP_INTERVAL_SEC = 6
PENDING_CONFIRM_SEC = 2
BAN_WINDOW = 3600
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 3
SL_ATR_MULTIPLIER = 4.0
TP_ATR_MULTIPLIER = 1.5
HARD_STOP_LOSS_PCT = 0.025

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
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "risk_multiplier": 1.0,
        "volume_multiplier": 1.0,
        "sl_atr_multiplier": 4.0,
        "tp_atr_multiplier": 1.5,
        "hard_stop_loss_pct": HARD_STOP_LOSS_PCT,
        "personality": "balanced",
        "personality_source": "infer",
        "last_personality_update": 0.0,
        "last_entry_time": 0.0,
    }

STATES = {sym: build_symbol_state(sym) for sym in ALL_SYMBOLS}
apply_all_symbol_profiles()
WATCH_TASKS = {}

# ── 指標計算函數 ──────────────────────────────────────────────

def calculate_ema(prices, period):
    if len(prices) < period:
        return np.mean(prices)
    multiplier = 2.0 / (period + 1)
    ema = np.mean(prices[:period])
    for p in prices[period:]:
        ema = (p - ema) * multiplier + ema
    return ema

def calculate_macd(prices, fast=12, slow=26, signal=9):
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
    apply_all_symbol_profiles()
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
    if balance <= 0:
        return 0
    # 交易金額直接與帳戶餘額掛鉤，使用約 95% 的可用資金
    usable = balance * 0.95
    return usable

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

async def initialize_atr_history(batch_size: int = ATR_WARMUP_BATCH_SIZE, limit: int = ATR_WARMUP_LIMIT, pause_sec: float = ATR_WARMUP_PAUSE_SEC):
    target_symbols = ALL_SYMBOLS[:ATR_WARMUP_SYMBOL_COUNT]
    print(f"⏳ [初始化] 開始分批獲取 {limit} 根 1m K線，以預熱前 {len(target_symbols)} 個主攻幣種的 ATR 歷史，下次批次間隔 {pause_sec}s...")
    total = len(target_symbols)
    if total == 0:
        print("⚠️ [初始化] 監控幣種清單為空，跳過 ATR 歷史預熱")
        return

    for batch_index in range(0, total, batch_size):
        batch = target_symbols[batch_index:batch_index + batch_size]
        print(f"⏳ [初始化] 進行第 {batch_index // batch_size + 1} 批：{len(batch)} 個幣種")
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
                        STATES[sym]["atr_history"].append(atr)
                print(f"✅ [初始化] {sym} 歷史 ATR 預熱完成，載入 {len(STATES[sym]['atr_history'])} 筆數據")
            else:
                print(f"⚠️ [初始化] {sym} 歷史 ATR 預熱失敗: {result}")

        if batch_index + batch_size < total:
            await asyncio.sleep(pause_sec)

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

async def load_open_positions():
    if not PAPER_TRADING:
        return
    try:
        with open("paper_state.json", "r") as f:
            state = json.load(f)
            
        current_time = time.time()
        for sym in ALL_SYMBOLS:
            pk = paper_key(sym)
            pos = state.get("positions", {}).get(pk, {})
            qty = float(pos.get("qty", 0.0))
            if abs(qty) > 0.000001:
                STATES[sym]["qty"] = qty
                STATES[sym]["avg_price"] = float(pos.get("avg_price", 0.0))

        # 檢查最近的平倉紀錄，加上冷卻時間，防止剛平倉完馬上又自動開倉
        trades = state.get("trades", [])
        for t in reversed(trades):
            if t.get("is_close"):
                # 將 "BTC:USDT" 還原為 "BTCUSDT" 以匹配 STATES 的鍵
                sym = t.get("symbol", "").replace(":USDT", "USDT")
                if sym in STATES:
                    trade_time_sec = t.get("time", 0) / 1000.0
                    # 如果這筆平倉是在最近 5 分鐘內發生的，且當前沒有持倉
                    if current_time - trade_time_sec < 300 and STATES[sym]["qty"] == 0:
                        if STATES[sym]["status"] != "COOLDOWN":
                            STATES[sym]["status"] = "COOLDOWN"
                            STATES[sym]["next_status_time"] = trade_time_sec + 300
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
        deltas = np.diff(closes[-(RSI_PERIOD + 1):])
        gains = deltas[deltas > 0].mean() if np.any(deltas > 0) else 1e-10
        losses = -deltas[deltas < 0].mean() if np.any(deltas < 0) else 1e-10
        rs = gains / losses
        s["current_rsi"] = 100.0 - (100.0 / (1.0 + rs))
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

    if not macd_reversal or not s.get("prev_close") or len(s["ohlcv"]) < 2:
        return False

    current_price = s["close_price"]
    atr_val = s["current_atr"] if s["current_atr"] > 0 else (current_price * 0.01)
    prev_bar_idx = -2
    prev_bar_high = s["ohlcv"][prev_bar_idx][2]
    prev_bar_low = s["ohlcv"][prev_bar_idx][3]
    breakout_confirmed = False
    if is_long:
        breakout_confirmed = current_price < prev_bar_low and prev_bar_low - current_price > max(atr_val * 0.25, 0.001)
    else:
        breakout_confirmed = current_price > prev_bar_high and current_price - prev_bar_high > max(atr_val * 0.25, 0.001)

    reversal_settings = DEFAULT_REVERSAL_SETTINGS.copy()
    reversal_settings.update(SYMBOL_REVERSAL_SETTINGS.get(sym, {}))

    volume_confirmed = s["current_vol"] > s["vol_ma20"] * reversal_settings["volume_multiplier"]
    trade_signal = s.get("trade_signal_strength", 0.0)
    trade_confirmed = trade_signal >= reversal_settings["trade_signal_threshold"]

    if macd_reversal and breakout_confirmed and volume_confirmed and trade_confirmed:
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

    # 1) 即時成交流監聽：大額異常成交 + 明顯逆向價格跳動才判定為突破反轉
    reversal_settings = DEFAULT_REVERSAL_SETTINGS.copy()
    reversal_settings.update(SYMBOL_REVERSAL_SETTINGS.get(sym, {}))
    trade_signal = s.get("trade_signal_strength", 0.0)
    reversal_threshold = reversal_settings["trade_signal_threshold"]
    prev_close = s.get("prev_close")
    if trade_signal >= reversal_threshold and prev_close:
        price_move_pct = (current_price - prev_close) / max(prev_close, 1e-8)
        if (is_long and price_move_pct < -max(reversal_settings["min_reverse_pct"], atr_pct * 1.2)) or \
           (not is_long and price_move_pct > max(reversal_settings["min_reverse_pct"], atr_pct * 1.2)):
            return "BREAKOUT_REVERSAL", f"即時大額成交異常 {s['trade_signal_reason']}"

    # 2) 簡化的大單/突發行情判斷：必須是與持倉方向相反的急速價格跳動
    reversal_settings = DEFAULT_REVERSAL_SETTINGS.copy()
    reversal_settings.update(SYMBOL_REVERSAL_SETTINGS.get(sym, {}))
    volume_surge = s["current_vol"] > s["vol_ma20"] * reversal_settings["volume_multiplier"]
    if prev_close:
        price_jump = (prev_close - current_price) / max(prev_close, 1e-8) > max(reversal_settings["price_jump_pct"], atr_pct * 1.2) if is_long else \
                     (current_price - prev_close) / max(prev_close, 1e-8) > max(reversal_settings["price_jump_pct"], atr_pct * 1.2)
    else:
        price_jump = False
    if volume_surge and price_jump:
        return "BREAKOUT_REVERSAL", "放量突發且價格急速變動"

    # 2) 盤整市場：價格被壓縮在狹窄區間內，且 ATR 也偏小
    is_ranging = range_width_pct < 0.025 and atr_pct < 0.015
    if is_ranging:
        profit_pct = (current_price - avg_price) / avg_price if is_long else (avg_price - current_price) / avg_price
        if profit_pct >= 0.003:
            return "RANGE_PROFIT_TAKE", f"盤整區間內已獲利 {profit_pct * 100:.2f}%"

    return "HOLD", "未達出場條件"


def has_strong_momentum(sym, is_long):
    s = STATES[sym]
    if s.get("vol_ma20", 0.0) <= 0 or len(s.get("closes", [])) < 4:
        return False
    volume_ratio = s["current_vol"] / max(s["vol_ma20"], 1e-8)
    recent_return = (s["closes"][-1] - s["closes"][-4]) / max(abs(s["closes"][-4]), 1e-8)
    if is_long:
        return volume_ratio > 1.2 and s["close_price"] > s.get("bb_mid", 0.0) and s["current_rsi"] > 52 and s.get("macd_hist", 0.0) > 0 and recent_return > 0.005
    return volume_ratio > 1.2 and s["close_price"] < s.get("bb_mid", 0.0) and s["current_rsi"] < 48 and s.get("macd_hist", 0.0) < 0 and recent_return < -0.005

async def close_position(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = STATES[sym]
    s["adjusted_this_tick"] = True
    if abs(s["qty"]) < 0.000001:
        return
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
    if s.get("adjusted_this_tick", False):
        return
    if abs(s["qty"]) < 0.000001 or s["avg_price"] <= 0:
        return
    hold_sec = time.time() - s["open_time"] if s["open_time"] > 0 else 9999
    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)
    # 高波動期縮短保護盲區到 20 秒，低波動維持 60 秒
    cooldown_limit = 20.0 if (current_atr > atr_24h_avg and atr_24h_avg > 0) else 60.0
    if hold_sec < cooldown_limit:
        return

    p = s["close_price"]
    avg = s["avg_price"]
    is_long = s["qty"] > 0
    profit_pct = (p - avg) / avg if is_long else (avg - p) / avg


    # cooldown_limit 過後才進此函數，所以 120 秒邊界仍有意義（低波動情況下 60~120 秒區間）
    sl_base = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long)
    sl_mult = sl_base * 1.5 if hold_sec < 120 else sl_base
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
        print(f"🔄 [反向補救] {sym} 方向錯誤且出現反轉訊號，直接反手 | current={p:.4f} | prev_close={s.get('prev_close', 0):.4f} | trade_signal={s.get('trade_signal_strength', 0.0):.2f} | vol={s.get('current_vol', 0):.0f}/{s.get('vol_ma20', 0):.0f} | MACD反向交叉")
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
    if ((is_long and m_death) or (not is_long and m_golden)) and (s["has_been_negative"] or abs(profit_pct) > 0.005):
        cs = 'sell' if is_long else 'buy'
        is_sl = profit_pct < 0.0
        print(f"📉 [反轉出場] {sym} MACD反向交叉，立即平倉 (損益: {profit_pct*100:.2f}%)")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="趨勢反轉", is_stop_loss=is_sl)
        return

    # 2. 判斷市場狀態：強勢 / 弱勢
    is_strong = (is_long and s["current_rsi"] > 50) or (not is_long and s["current_rsi"] <= 50)

    # ATR TP/SL 價格 (兩條路線共用)
    tp_base = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), is_long)
    if is_long:
        tp = avg + max(atr_val * tp_base, avg * 0.003)
        sl = avg - (atr_val * sl_mult)
    else:
        tp = avg - max(atr_val * tp_base, avg * 0.003)
        sl = avg + (atr_val * sl_mult)

    # ── 保本鎖利與利潤防護機制 (Break-even & Capital Protection Lock) ──
    # 實施分級保本與回撤防護，防止「有利潤不平倉，最後被打到停損」
    if s["highest_profit_pct"] >= 0.008 and profit_pct < s["highest_profit_pct"] * 0.5:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [回撤鎖利] {sym} 獲利最高曾達 {s['highest_profit_pct']*100:.3f}%，回撤已達50% (目前 {profit_pct*100:.3f}%)，觸發回撤平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="回撤鎖利防護")
        s["highest_profit_pct"] = 0.0
        return
    elif s["highest_profit_pct"] >= 0.006 and profit_pct < 0.0025:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [高利鎖利] {sym} 獲利最高曾達 {s['highest_profit_pct']*100:.3f}%，目前回落至 {profit_pct*100:.3f}%，觸發高利保護平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="高利鎖利防護")
        s["highest_profit_pct"] = 0.0
        return
    elif s["highest_profit_pct"] >= 0.004 and profit_pct < 0.0015:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [中利鎖利] {sym} 獲利最高曾達 {s['highest_profit_pct']*100:.3f}%，目前回落至 {profit_pct*100:.3f}%，觸發中利保護平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="中利鎖利防護")
        s["highest_profit_pct"] = 0.0
        return
    elif s["highest_profit_pct"] >= 0.0025 and profit_pct < 0.0010:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [微利鎖利] {sym} 獲利最高曾達 {s['highest_profit_pct']*100:.3f}%，目前回落至 {profit_pct*100:.3f}%，觸發微利保護平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="微利鎖利防護")
        s["highest_profit_pct"] = 0.0
        return

    # 見好就收：先以 0.3% 進行停利，若後續價格再創新高，就把停利線往上移動
    early_take_profit_pct = 0.005
    if s["highest_profit_pct"] >= early_take_profit_pct and profit_pct >= 0.001:
        should_exit, trail_tp = update_trailing_take_profit(sym, p, is_long)
        if should_exit:
            cs = 'sell' if is_long else 'buy'
            print(f"🎯 [移動停利] {sym} 已達到 0.3% 目標，按移動停利出場")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="移動停利")
            s["highest_profit_pct"] = 0.0
            return

    if not is_strong:
        # ── 盤整／弱勢路線 ────────────────────────────────
        # 僵局一階：時間到 → 有任何正利潤就全平，利潤微薄(0.2%~0.5%)平50%
        stagnation_limit = get_dynamic_stagnation_limit(s["current_atr"], s["atr_ma20"])
        if hold_sec > stagnation_limit and profit_pct > 0:
            if not s["has_partial_closed"] and 0.002 <= profit_pct < 0.005:
                half = abs(s["qty"]) * 0.5
                cs = 'sell' if is_long else 'buy'
                print(f"⏳ [僵局一階] {sym} 持倉{stagnation_limit//60}分利潤{profit_pct*100:.2f}%，平50%")
                await close_position(sym, cs, half, p, avg, reason="僵局一階")
                s["has_partial_closed"] = True
                return
            if profit_pct < 0.002:
                cs = 'sell' if is_long else 'buy'
                print(f"⏳ [僵局平倉] {sym} 持倉{stagnation_limit//60}分利潤僅{profit_pct*100:.2f}%，全平")
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
        # 弱勢快速停利：穩健型幣種可以等更久，再決定是否落袋
        weak_tp = 0.005
        if s.get("personality") == "calm":
            weak_tp = 0.007
        if s["highest_profit_pct"] >= weak_tp:
            if not has_strong_momentum(sym, is_long):
                cs = 'sell' if is_long else 'buy'
                print(f"🎯 [快速停利] {sym} 弱勢利潤達{weak_tp*100:.1f}%，動能不足則落袋")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="快速停利")
                s["highest_profit_pct"] = 0.0
                return
            print(f"⚡ [保留動能] {sym} 弱勢已達{weak_tp*100:.1f}%但動能仍強，暫不停利")
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
        if s["highest_profit_pct"] >= 0.01:
            if (is_long and p <= s["trailing_highest"] * 0.990) or (not is_long and p >= s["trailing_lowest"] * 1.010):
                cs = 'sell' if is_long else 'buy'
                print(f"🏃 [動態停利] {sym} 強勢回撤0.5%")
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
    profit_pct = (p - avg) / avg if is_long else (avg - p) / avg
    hold_sec = time.time() - s["open_time"] if s["open_time"] > 0 else 9999

    if hold_sec < 120:
        return

    # 硬停損：以百分比計算觸發價格（HARD_STOP_LOSS_PCT = 0.01 = 虧損 1%）
    hard_stop_loss_pct = get_effective_exit_setting(sym, "hard_stop_loss_pct", s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT), is_long)
    hard_sl_long = avg * (1 - hard_stop_loss_pct)
    hard_sl_short = avg * (1 + hard_stop_loss_pct)
    if (is_long and p <= hard_sl_long) or (not is_long and p >= hard_sl_short):
        cs = 'sell' if is_long else 'buy'
        print(f"⛔ [硬停損{hard_stop_loss_pct*100:.1f}%] {sym} 均價:{avg:.6f} 現價:{p:.6f}")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason=f"硬停損{hard_stop_loss_pct*100:.1f}%", is_stop_loss=True)
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
    if hold_sec > 900 and profit_pct < -0.005:
        cs = 'sell' if is_long else 'buy'
        print(f"⏱️ [時間停損] {sym} {hold_sec/60:.1f}分仍虧損")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="時間停損", is_stop_loss=True)
        return

# ── 進場邏輯 ──────────────────────────────────────────────────

async def execute_order(sym, side, price):
    s = STATES[sym]
    pk = paper_key(sym)
    margin = compute_per_coin_margin(sym)
    if margin <= 0:
        print(f"⚠️ [風控] {sym} 無可用保證金")
        return

    now = time.time()
    if side == 'buy' and s["entry_count"] > 0:
        if now - s["last_entry_time"] < s["entry_cooldown_sec"]:
            print(f"⏳ [加倉冷卻] {sym} 距離上次加倉不足 {s['entry_cooldown_sec']} 秒")
            return
        if s["entry_count"] >= s["max_additional_entries"]:
            print(f"⚠️ [加倉上限] {sym} 已達最大加倉次數")
            return
        if s["avg_price"] > 0 and s["close_price"] > 0:
            profit_pct = (s["close_price"] - s["avg_price"]) / s["avg_price"]
            if profit_pct < 0.001:
                print(f"🛑 [加倉風控] {sym} 目前尚未回到保本線以上，不加倉")
                return

    base_amt = margin / price
    allocation_pct = s["entry_size_pct"] if s["entry_count"] == 0 else s["add_entry_pct"]
    base_amt *= allocation_pct
    max_notional = 500.0  # 最大名義價值 500 USDT
    if base_amt * price > max_notional:
        base_amt = max_notional / price
    if base_amt < 0.001:
        print(f"⚠️ [風控] {sym} 數量過小 {base_amt:.6f}")
        return

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
        if close_price <= prev_close:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 收盤價 {close_price:.4f} <= 前K收盤 {prev_close:.4f}")
            return False
        if upper_wick > body * 3.0:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 上影線過長 (上影線 {upper_wick:.4f} > 實體 {body:.4f} * 3)")
            return False
        return True

    if close_price >= prev_close:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 收盤價 {close_price:.4f} >= 前K收盤 {prev_close:.4f}")
        return False
    if lower_wick > body * 3.0:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 下影線過長 (下影線 {lower_wick:.4f} > 實體 {body:.4f} * 3)")
        return False
    return True


def is_entry_volume_confirmed(sym, side):
    s = STATES[sym]
    if len(s["ohlcv"]) < 2:
        return False
    current_vol = s["current_vol"]
    vol_ma20 = s["vol_ma20"]
    if vol_ma20 <= 0:
        return False
    threshold = vol_ma20 * VOLUME_RATIO_THRESHOLD * s.get("volume_multiplier", 1.0)
    if current_vol < threshold:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能不足] 當前量 {current_vol:.2f} < 均量門檻 {threshold:.2f}")
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
    
    # 均線過濾器：受 15m SMA200 趨勢保護，防止逆勢接刀
    if s.get("sma200_15m", 0) > 0:
        ma200 = s["sma200_15m"]
        
        if side == 'buy' and cp <= ma200:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [MA200過濾] 做多但價格 {cp:.4f} <= MA200 {ma200:.4f}")
            return False
        if side == 'sell' and cp >= ma200:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [MA200過濾] 做空但價格 {cp:.4f} >= MA200 {ma200:.4f}")
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
    if adx_val < 20:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [ADX過濾] 趨勢強度 ADX {adx_val:.1f} < 20")
        return False

    # 實盤最小量限制
    min_volume = max(1000.0, s["vol_ma20"] * 0.1)
    if s["current_vol"] < min_volume:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [實盤最小量過濾]")
        return False
    return True

def compute_signal_strength(sym):
    s = STATES[sym]
    if len(s["closes"]) < 20:
        return (None, 0)

    rsi = s["current_rsi"]
    close = s["close_price"]
    prev_close = s["prev_close"] if s["prev_close"] is not None else close
    ema20 = s.get("ema20", 0.0)
    ema50 = s.get("ema50", 0.0)

    trend_long = ema20 > 0 and close > ema20
    trend_short = ema20 > 0 and close < ema20

    # Define parameters for dynamic RSI thresholds
    LONG_RSI_NORMAL = 45.0
    SHORT_RSI_NORMAL = 55.0
    LONG_RSI_HIGH_VOL = 30.0
    SHORT_RSI_HIGH_VOL = 70.0

    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)

    if current_atr > atr_24h_avg and atr_24h_avg > 0:
        long_rsi_threshold = LONG_RSI_HIGH_VOL
        short_rsi_threshold = SHORT_RSI_HIGH_VOL
        vol_mode = "高波動模式 (High Vol)"
    else:
        long_rsi_threshold = LONG_RSI_NORMAL
        short_rsi_threshold = SHORT_RSI_NORMAL
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

    last_candle_confirmed_long = len(s["ohlcv"]) >= 2 and s["ohlcv"][-1][4] > s["ohlcv"][-2][4]
    last_candle_confirmed_short = len(s["ohlcv"]) >= 2 and s["ohlcv"][-1][4] < s["ohlcv"][-2][4]

    ema50 = s.get("ema50", 0.0)
    trend_confluence_long = ema50 == 0.0 or close > ema50
    trend_confluence_short = ema50 == 0.0 or close < ema50

    is_above_sma200 = s.get("sma200_15m", 0) > 0 and close > s.get("sma200_15m", 0) * 0.999
    is_below_sma200 = s.get("sma200_15m", 0) > 0 and close < s.get("sma200_15m", 0) * 1.001

    print(f"@@COIN_DEBUG@@ 🔍 {sym} 條件檢測 | RSI動能(L>45/S<55): {rsi > 45.0}/{rsi < 55.0} | SMA200長線(L/S): {is_above_sma200}/{is_below_sma200} | MACD多頭/空頭: {macd_hist > 0}/{macd_hist < 0} | 收盤價確認(L/S): {last_candle_confirmed_long}/{last_candle_confirmed_short}")

    # Route A (Trend Following): 站上 SMA200 AND (MACD交叉或柱狀體為正) AND K線方向確認 AND 動能確認
    route_a_long = (
        is_above_sma200 and 
        (long_macd_cross or macd_hist > 0) and 
        last_candle_confirmed_long and 
        rsi > 45.0                      # 放寬：只要 RSI > 45 (脫離空頭區間) 即可
    )
    
    route_a_short = (
        is_below_sma200 and 
        (short_macd_cross or macd_hist < 0) and 
        last_candle_confirmed_short and 
        rsi < 55.0                      # 放寬：只要 RSI < 55 (脫離多頭區間) 即可
    )

    long_base_ok = route_a_long
    short_base_ok = route_a_short

    if long_base_ok:
        route = "a"
        strength = 15.0 + ((close - ema20) / max(ema20, 1e-8) * 100)
        if long_macd_cross:
            strength += 5.0
        return ("buy", strength if strength >= 8.0 else 0.0, route)

    if short_base_ok:
        route = "a"
        strength = 15.0 + ((ema20 - close) / max(ema20, 1e-8) * 100)
        if short_macd_cross:
            strength += 5.0
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
        
        # Route C 反轉搶短直接開倉，不進行二次確認與突破等待
        if route == "c":
            print(f"⚡ [即時開倉] {sym} 觸發反轉搶短，繞過二次確認即刻下單！")
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
            if isinstance(trades, list):
                for trade in trades:
                    update_trade_signal(sym, trade)
            elif trades:
                update_trade_signal(sym, trades)
        except Exception as e:
            print(f"⚠️ [成交流監聽異常] {sym}: {e}")
            await asyncio.sleep(3)


async def ensure_watch_tasks():
    global WATCH_TASKS
    desired_symbols = set(ALL_SYMBOLS)
    current_symbols = set(WATCH_TASKS.keys())

    for sym in current_symbols - desired_symbols:
        task = WATCH_TASKS.pop(sym, None)
        if task is not None:
            task.cancel()

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
        await asyncio.wait_for(initialize_atr_history(), timeout=60)
    except (asyncio.TimeoutError, Exception) as e:
        print(f"⏳ [初始化] ATR 歷史預熱超時或失敗 ({e})，將在運行中慢慢加熱")
    await fetch_real_balance()
    await load_open_positions()
    await fetch_all_sma200()

    last_balance_update = time.time()

    while True:
        try:
            loop_start = time.time()
            if not PAPER_TRADING and loop_start - last_balance_update > 30:
                await fetch_real_balance()
                last_balance_update = loop_start

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
            update_all_dynamic_personalities()
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
