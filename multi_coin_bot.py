import asyncio
from ai_manager import ai_engine
import ccxt
import ccxt.pro as ccxtpro
import numpy as np
import json
import os
import signal
import time
import sys
import uuid
import fcntl
import math
import requests
import traceback
from dotenv import load_dotenv
from services.utils import paper_key
from update_paper_state import update_paper_state
import csv

load_dotenv()

# --- 通知設定 ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_alert(message):
    """發送緊急告警到 Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"⚠️ [通知失敗] 未設定 TELEGRAM_TOKEN 或 TELEGRAM_CHAT_ID，僅輸出到 Log: {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🚨 [機器人警報]\n{message}"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"⚠️ [通知失敗] 無法發送 Telegram 訊息: {e}")

LOCK_FILE = "/tmp/binance_bot_single_instance.lock"
lock_file_handle = None


def _process_exists(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_process(pid):
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.2)
        if _process_exists(pid):
            os.kill(pid, signal.SIGKILL)
        return True
    except Exception:
        return False


def ensure_single_instance():
    global lock_file_handle
    lock_file_handle = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file_handle.seek(0)
        lock_file_handle.truncate()
        lock_file_handle.write(str(os.getpid()))
        lock_file_handle.flush()
        return
    except IOError:
        lock_file_handle.seek(0)
        pid_text = lock_file_handle.read().strip()
        stale_pid = None
        try:
            stale_pid = int(pid_text)
        except Exception:
            stale_pid = None

        if stale_pid and stale_pid != os.getpid() and _process_exists(stale_pid):
            print(f"⚠️ 偵測到系統中已有另一個機器人正在執行 (PID={stale_pid})，將自動終止舊進程並繼續啟動...")
            _terminate_process(stale_pid)
            try:
                lock_file_handle.close()
            except Exception:
                pass
            try:
                os.remove(LOCK_FILE)
            except FileNotFoundError:
                pass
            except Exception:
                pass
            lock_file_handle = open(LOCK_FILE, "a+")
            try:
                fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_file_handle.seek(0)
                lock_file_handle.truncate()
                lock_file_handle.write(str(os.getpid()))
                lock_file_handle.flush()
                return
            except IOError:
                pass
        elif stale_pid and stale_pid != os.getpid() and not _process_exists(stale_pid):
            print(f"⚠️ 偵測到鎖定進程 PID={stale_pid} 已不存在，清理過期鎖檔並繼續啟動...")
            try:
                lock_file_handle.close()
            except Exception:
                pass
            try:
                os.remove(LOCK_FILE)
            except FileNotFoundError:
                pass
            except Exception:
                pass
            lock_file_handle = open(LOCK_FILE, "a+")
            try:
                fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_file_handle.seek(0)
                lock_file_handle.truncate()
                lock_file_handle.write(str(os.getpid()))
                lock_file_handle.flush()
                return
            except IOError:
                pass

        print("🚨 錯誤: 偵測到系統中已有另一個機器人正在執行！")
        print("💡 為了避免重複下單與邏輯衝突，本次啟動已自動攔截並退出。")
        print("💡 提示: 若是意外關閉舊程式，請先刪除過期的鎖定檔 /tmp/binance_bot_single_instance.lock，再重新啟動。")
        sys.exit(1)

if __name__ == "__main__":
    ensure_single_instance()

exchange_futures = ccxtpro.binance({
    'apiKey': os.getenv('BINANCE_API_KEY') or None,
    'secret': os.getenv('BINANCE_API_SECRET') or None,
    'enableRateLimit': True,
    'rateLimit': 1000,
    'options': {
        'defaultType': 'future',
        'watchOrderBookSnapshot': True,
    },
})

exchange_spot = ccxtpro.binance({
    'apiKey': os.getenv('BINANCE_API_KEY') or None,
    'secret': os.getenv('BINANCE_API_SECRET') or None,
    'enableRateLimit': True,
    'rateLimit': 1000,
    'options': {
        'defaultType': 'spot',
        'watchOrderBookSnapshot': True,
    },
})

USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
PAPER_TRADING = True
TIMEFRAME = '1m'
TRADE_HISTORY_FILE = "trade_history.json"
MAX_GLOBAL_CONCURRENT_TRADES = 3
DEFAULT_LEVERAGE = 5

COIN_PROFILE_CONFIG = {
    # --- 第一類：核心趨勢層 (Core Trend) - 穩健趨勢，較高槓桿 ---
    "SOLUSDT": {"sl_atr_multiplier": 2.2, "tp_atr_multiplier": 4.5, "volume_threshold_factor": 1.2, "breakeven_trigger": 0.5, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8},
    "LINKUSDT": {"sl_atr_multiplier": 1.5, "tp_atr_multiplier": 3.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.4, "min_flip_time": 180, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8},
    "TRXUSDT": {"sl_atr_multiplier": 1.9, "tp_atr_multiplier": 3.8, "volume_threshold_factor": 1.2, "breakeven_trigger": 0.5, "min_flip_time": 240, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8},

    # --- 第二類：高彈性動能層 (High-Beta Momentum) - 快速爆發，中等槓桿 ---
    "RENDERUSDT": {"sl_atr_multiplier": 1.5, "tp_atr_multiplier": 3.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4},
    "SUIUSDT": {"sl_atr_multiplier": 1.4, "tp_atr_multiplier": 2.7, "volume_threshold_factor": 1.8, "breakeven_trigger": 0.7, "min_flip_time": 90, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4},
    "INJUSDT": {"sl_atr_multiplier": 1.7, "tp_atr_multiplier": 3.3, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4},
    "NEARUSDT": {"sl_atr_multiplier": 1.7, "tp_atr_multiplier": 3.4, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 180, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4},
    "VELVETUSDT": {"sl_atr_multiplier": 1.5, "tp_atr_multiplier": 3.0, "volume_threshold_factor": 1.6, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4},
    "LABUSDT": {"sl_atr_multiplier": 1.5, "tp_atr_multiplier": 3.0, "volume_threshold_factor": 1.6, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4},

    # --- 第三類：投機與特定風險層 (Speculative_Risk) - 極端防禦，低槓桿 ---
    "AVAXUSDT": {"sl_atr_multiplier": 1.9, "tp_atr_multiplier": 3.8, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 240, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2},
    "DOGEUSDT": {"sl_atr_multiplier": 2.6, "tp_atr_multiplier": 5.2, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": False, "profile_type": "Speculative_Risk", "leverage": 2},
    "PEPEUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": False, "profile_type": "Speculative_Risk", "leverage": 2}
}

ALL_SYMBOLS = list(COIN_PROFILE_CONFIG.keys())

LEVERAGE_TIERS = {
    "custom_leverage": {
        "coins": {},  # 預留：若未來想針對某些幣種調低槓桿可填入，例如 {"DOGEUSDT"}
        "leverage": 3
    }
}

def get_symbol_leverage(sym):
    conf = COIN_PROFILE_CONFIG.get(sym, {})
    if "leverage" in conf:
        return int(conf["leverage"])
    return DEFAULT_LEVERAGE
RSI_PERIOD = 9
VOLUME_RATIO_THRESHOLD = 0.7
ATR_WARMUP_BATCH_SIZE = 2
ATR_WARMUP_SYMBOL_COUNT = 12
ATR_WARMUP_LIMIT = 1000
ATR_WARMUP_PAUSE_SEC = 0.4
TIME_STOP_MINUTES = 30

if USE_TESTNET:
    exchange_futures.urls['api']['fapiPublic'] = 'https://testnet.binancefuture.com/fapi/v1'
    exchange_futures.urls['api']['fapiPrivate'] = 'https://testnet.binancefuture.com/fapi/v1'
    exchange_spot.urls['api']['public'] = 'https://testnet.binance.vision/api/v3'
    exchange_spot.urls['api']['private'] = 'https://testnet.binance.vision/api/v3'

DEFAULT_SYMBOLS = [
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT",
    "DOTUSDT", "UNIUSDT", "NEARUSDT", "FETUSUSDT", "SUIUSDT"
]
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "bot_symbols.json")

PERSONALITY_TEMPLATES = {
    "calm": {
        "personality": "calm",
        "risk_multiplier": 0.7,
        "volume_multiplier": 0.8,
        "entry_cooldown_sec": 180,
        "max_additional_entries": 1,
        "entry_size_pct": 0.3,
        "add_entry_pct": 0.15,
        "sl_atr_multiplier": 1.5,
        "tp_atr_multiplier": 3.0,
        "hard_stop_loss_pct": 0.01,
    },
    "balanced": {
        "personality": "balanced",
        "risk_multiplier": 1.0,
        "volume_multiplier": 1.0,
        "entry_cooldown_sec": 90,
        "max_additional_entries": 2,
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "sl_atr_multiplier": 1.2,
        "tp_atr_multiplier": 2.4,
        "hard_stop_loss_pct": 0.01,
    },
    "aggressive": {
        "personality": "aggressive",
        "risk_multiplier": 1.2,
        "volume_multiplier": 1.2,
        "entry_cooldown_sec": 60,
        "max_additional_entries": 3,
        "entry_size_pct": 0.7,
        "add_entry_pct": 0.4,
        "sl_atr_multiplier": 1.0,
        "tp_atr_multiplier": 2.0,
        "hard_stop_loss_pct": 0.01,
    },
    "adaptive": {
        "personality": "adaptive",
        "risk_multiplier": 1.0,
        "volume_multiplier": 1.0,
        "entry_cooldown_sec": 90,
        "max_additional_entries": 2,
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "sl_atr_multiplier": 1.2,
        "tp_atr_multiplier": 2.4,
        "hard_stop_loss_pct": 0.01,
    },
}

SYMBOL_EXIT_OVERRIDES = {
    "XRPUSDT": {
        "tp_atr_multiplier": 3.0,
        "sl_atr_multiplier": 1.5,
    },
    "LINKUSDT": {
        "tp_atr_multiplier": 3.0,
        "sl_atr_multiplier": 1.5,
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

_PRECISION_CACHE = {}


def convert_to_ccxt_symbol(symbol: str) -> str:
    symbol = str(symbol).upper().strip()
    if symbol == "PEPEUSDT":
        return "1000PEPE/USDT"
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


    # 讀取優先權清單
    try:
        with open(os.path.join(os.path.dirname(__file__), "strategy_config.json"), "r") as f:
            config = json.load(f)
            priority_list = config.get("priority_symbols", [])
    except Exception:
        priority_list = []

    # 組合清單：優先幣種在前，其餘在後（去重）
    combined = []
    for s in priority_list:
        norm = normalize_symbol(s)
        if norm and norm not in combined:
            combined.append(norm)
    
    for s in symbols:
        norm = normalize_symbol(s)
        if norm and norm not in combined:
            combined.append(norm)
            
    return combined


def load_symbol_profiles():
    _, profiles = load_symbol_config()
    return profiles


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
    aggressive_coins = {"DOGEUSDT", "PEPEUSDT", "SHIBUSDT"}
    balanced_coins = {"ADAUSDT", "SOLUSDT", "LINKUSDT", "AVAXUSDT", "NEARUSDT", "SUIUSDT", "INJUSDT", "RENDERUSDT"}
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
    # 1. 優先從 SYMBOL_PROFILES 地圖中讀取個性化設定
    profile = SYMBOL_PROFILES.get(sym)
    if profile and key in profile:
        return profile[key]
    
    # 2. 如果地圖沒寫，再從策略配置檔讀取
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
        "volume_threshold_factor", "min_flip_time", "breakeven_trigger",
        "profile_type", "leverage", "mtf_filter"
    ]:
        if key in profile:
            state[key] = profile[key]
    state["personality"] = personality
    state["personality_source"] = personality_source
    if personality_source == "manual":
        state["last_personality_update"] = time.time()


def apply_all_symbol_profiles():
    for sym in ALL_SYMBOLS:
        json_profile = SYMBOL_PROFILES.get(sym, {})
        py_profile = COIN_PROFILE_CONFIG.get(sym, {})
        merged_profile = {**json_profile, **py_profile}
        apply_symbol_profile(sym, merged_profile)


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


_, SYMBOL_PROFILES = load_symbol_config()

MAX_POSITIONS = 3
COOLDOWN_SEC = 1800
MAIN_LOOP_INTERVAL_SEC = 6
PENDING_CONFIRM_SEC = 2
BAN_WINDOW = 3600
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 3
SL_ATR_MULTIPLIER = 2.5
TP_ATR_MULTIPLIER = 3.0
HARD_STOP_LOSS_PCT = 0.015

def build_symbol_state(sym):
    conf = COIN_PROFILE_CONFIG.get(sym, {})
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
        "vol_ma10": 0.0,
        "vol_ma20": 0.0,
        "current_vol": 0.0,
        "trailing_highest": 0.0,
        "trailing_lowest": float('inf'),
        "highest_profit_pct": 0.0,
        "has_partial_closed": False,
        "pending_stop_loss": False,
        "stop_loss_price": 0.0,
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
        "entry_cooldown_sec": conf.get("entry_cooldown_sec", 90),
        "min_flip_time": conf.get("min_flip_time", 300),
        "profile_type": conf.get("profile_type", "Core_Trend"),
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "risk_multiplier": 1.0,
        "volume_threshold_factor": conf.get("volume_threshold_factor", 0.8),
        "volume_multiplier": conf.get("volume_multiplier", 1.0),
        "sl_atr_multiplier": conf.get("sl_atr_multiplier", 1.5),
        "tp_atr_multiplier": conf.get("tp_atr_multiplier", 2.5),
        "hard_stop_loss_pct": HARD_STOP_LOSS_PCT,
        "personality": "balanced",
        "personality_source": "infer",
        "last_personality_update": 0.0,
        "last_entry_time": 0.0,
    }

STATES = {sym: build_symbol_state(sym) for sym in ALL_SYMBOLS}
apply_all_symbol_profiles()
WATCH_TASKS = {}
request_semaphore = asyncio.Semaphore(5)

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
        headers = getattr(exchange_futures, 'last_response_headers', {})
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
    return [sym for sym in ALL_SYMBOLS if sym in STATES and abs(STATES[sym]["qty"]) > 0.000001]


def is_symbol_locked(sym):
    s = STATES.get(sym)
    if not s:
        return False
    return abs(s["qty"]) > 0.000001 or s["entry_count"] > 0 or s["open_time"] > 0 or s["status"] in ("COOLDOWN", "BANNED") or s.get("pending_side") is not None


def filter_valid_symbols(exchange, symbols):
    if not exchange_futures.markets:
        return list(symbols)
    valid = []
    for sym in symbols:
        found = False
        for m in exchange_futures.markets.values():
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
    desired = filter_valid_symbols(exchange_futures, normalize_symbol_list(requested_symbols))
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
        balance_info = await exchange_futures.fetch_balance()
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

def compute_per_coin_margin(sym=None):
    balance = get_balance()
    if balance <= 0 or not sym:
        return 0

    # 用戶要求：每次持倉3個幣種，資金均分 (各拿 33%)
    return balance * 0.33 * 0.95

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

def mark_exit(sym, is_stop_loss=False, reason=""):
    s = STATES[sym]
    now = time.time()
    s["status"] = "COOLDOWN"
    
    # 動態靜默期：一般平倉 5 分鐘，停損 30 分鐘
    actual_cooldown = 1800 if is_stop_loss else 300
    s["next_status_time"] = now + actual_cooldown
    
    cd_min = actual_cooldown // 60
    s["status_reason"] = f"冷卻中 ({cd_min}分鐘) - {reason}"
    print(f"⏳ [狀態] {sym} 平倉 ({reason}) → COOLDOWN {cd_min}分鐘")
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
    s["first_entry_price"] = 0.0
    s["max_additional_entries"] = 2
    s["entry_cooldown_sec"] = 180
    s["entry_size_pct"] = 0.5
    s["add_entry_pct"] = 0.25
    s["risk_multiplier"] = 1.0
    s["volume_multiplier"] = 1.0
    s["sl_atr_multiplier"] = 1.5
    s["tp_atr_multiplier"] = 2.5
    s["hard_stop_loss_pct"] = 0.02
    s["personality"] = "balanced"
    s["personality_source"] = "infer"
    s["last_personality_update"] = 0.0
    s["last_entry_time"] = 0.0
    s["last_flip_time"] = 0.0

# ── 大盤與風向監控 (BTC & ETH Filter) ─────────────────────────

MARKET_WIND = {
    "btc_trend": "NEUTRAL",  # "BULL" or "BEAR"
    "allow_long": True,
    "allow_short": True,
    "btc_change_15m": 0.0,
    "eth_change_15m": 0.0
}

async def update_market_wind(exchange):
    global MARKET_WIND
    try:
        # 抓取 BTC 和 ETH
        btc_ohlcv = await exchange.fetch_ohlcv("BTC/USDT", TIMEFRAME, limit=100)
        eth_ohlcv = await exchange.fetch_ohlcv("ETH/USDT", TIMEFRAME, limit=100)
        btc_ohlcv_4h = await exchange.fetch_ohlcv("BTC/USDT", '4h', limit=50)
        
        MARKET_WIND["allow_long"] = True
        MARKET_WIND["allow_short"] = True
        
        if len(btc_ohlcv_4h) >= 20:
            btc_closes_4h = [x[4] for x in btc_ohlcv_4h]
            # Simple EMA20 for 4H
            alpha = 2 / 21
            ema = btc_closes_4h[0]
            for val in btc_closes_4h[1:]: ema = alpha * val + (1 - alpha) * ema
            btc_price_4h = btc_closes_4h[-1]
            MARKET_WIND["btc_trend_4h"] = "BULL" if btc_price_4h > ema else "BEAR"
        else:
            MARKET_WIND["btc_trend_4h"] = "NEUTRAL"

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
            
        # 1. 瀑布防護 (極端風暴：2% 震幅)
        if btc_change_15m < -0.02 or eth_change_15m < -0.02:
            MARKET_WIND["allow_long"] = False
            print(f"⚠️ [大盤瀑布風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣多單開倉！")
        elif btc_change_15m > 0.02 or eth_change_15m > 0.02:
            MARKET_WIND["allow_short"] = False
            print(f"⚠️ [大盤暴漲風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣空單開倉！")
            
    except Exception as e:
        print(f"⚠️ [更新大盤風向失敗]: {e}")

# ── 資料獲取 ──────────────────────────────────────────────────

async def initialize_atr_history(exchange, batch_size: int = ATR_WARMUP_BATCH_SIZE, limit: int = ATR_WARMUP_LIMIT, pause_sec: float = ATR_WARMUP_PAUSE_SEC):
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

async def fetch_all_klines(exchange):
    async def fetch_with_sem(sym):
        async with request_semaphore:
            return await exchange.fetch_ohlcv(sym, TIMEFRAME, limit=100)
            
    tasks = {sym: fetch_with_sem(sym) for sym in ALL_SYMBOLS}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym]["ohlcv"] = results[i]
            STATES[sym]["close_price"] = results[i][-1][4]
        else:
            print(f"⚠️ [K線獲取失敗] {sym}: {results[i]}")

async def fetch_sma200_15m(exchange, sym):
    try:
        async with request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '15m', limit=200)
        closes = np.array([x[4] for x in ohlcv])
        return float(np.mean(closes))
    except Exception as e:
        print(f"⚠️ [SMA200獲取失敗] {sym}: {e}")
        return 0.0

async def fetch_all_sma200(exchange):
    tasks = [fetch_sma200_15m(exchange, sym) for sym in ALL_SYMBOLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym]["sma200_15m"] = results[i]

async def fetch_ema50_1h(exchange, sym):
    try:
        async with request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '1h', limit=100)
        if not ohlcv or len(ohlcv) == 0:
            return 0.0
        closes = np.array([x[4] for x in ohlcv])
        ema50 = calculate_ema(closes, 50)
        return float(ema50)
    except Exception as e:
        print(f"⚠️ [1H EMA50獲取失敗] {sym}: {e}")
        return 0.0

async def fetch_all_ema50_1h(exchange):
    tasks = [fetch_ema50_1h(exchange, sym) for sym in ALL_SYMBOLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym]["ema50_1h"] = results[i]

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
    s["vol_ma10"] = float(np.mean(volumes[-10:])) if len(volumes) >= 10 else float(np.mean(volumes))
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

def update_trailing_stop(sym, current_price, is_long):
    """
    實作非對稱移動停損 (Asymmetric Trailing Stop)
    當價格創新高/新低時，上移停損點，且加入保本緩衝區防止被雜訊洗出場。
    """
    s = STATES[sym]
    atr_val = s.get("current_atr", 0.0)
    if atr_val <= 0:
        return False, s["trailing_stop_price"]

    trailing_multiplier = s.get("trailing_stop_multiplier", 2.0)
    liq_price = s["avg_price"] * (1 - s["sl_atr_multiplier"] * (s.get("current_atr", 0.0) / max(s["close_price"], 1e-8)))
    
    if is_long:
        if current_price > s.get("trailing_highest", 0.0):
            s["trailing_highest"] = current_price
            # 修改：使用最高點而非當前價來計算停損，確保停損點只會上移
            trail_sl = s["trailing_highest"] - (atr_val * trailing_multiplier)
            
            # --- 保本邏輯 ---
            trigger_mult = s.get("breakeven_trigger")
            if trigger_mult is None:
                trigger_mult = s.get("sl_atr_multiplier", 1.5)
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = s["avg_price"] + sl_dist_atr
            if current_price >= breakeven_trigger:
                breakeven_sl = s["avg_price"]
                trail_sl = max(trail_sl, breakeven_sl)
            
            safe_min_sl = liq_price * 1.2
            new_sl = max(s["trailing_stop_price"], trail_sl) # 確保只往有利方向移動
            if new_sl < safe_min_sl:
                new_sl = safe_min_sl
            
            if new_sl > s["trailing_stop_price"]:
                s["trailing_stop_price"] = new_sl
    else:
        if current_price < s.get("trailing_lowest", float('inf')):
            s["trailing_lowest"] = current_price
            # 修改：使用最低點而非當前價來計算停損，確保停損點只會下移
            trail_sl = s["trailing_lowest"] + (atr_val * trailing_multiplier)
            
            # --- 保本邏輯 ---
            trigger_mult = s.get("breakeven_trigger")
            if trigger_mult is None:
                trigger_mult = s.get("sl_atr_multiplier", 1.5)
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = s["avg_price"] - sl_dist_atr
            if current_price <= breakeven_trigger:
                breakeven_sl = s["avg_price"]
                trail_sl = min(trail_sl, breakeven_sl)
            
            safe_max_sl = liq_price * 0.8
            new_sl = min(s["trailing_stop_price"], trail_sl)
            if new_sl > safe_max_sl:
                new_sl = safe_max_sl
            
            if s["trailing_stop_price"] == 0.0 or new_sl < s["trailing_stop_price"]:
                s["trailing_stop_price"] = new_sl
                    
    return False, s["trailing_stop_price"]
    
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
        if profit_pct >= 0.005:
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

    try:
        await _close_position_inner(sym, close_side, qty, price, avg_price, reason, is_stop_loss)
    finally:
        s["adjusted_this_tick"] = False


import os
import json
import time

def record_trade_result(symbol, entry_reason, exit_reason, profit_pct, current_atr, max_profit_reached=0.0,
                        expected_entry=0.0, expected_exit=0.0, actual_entry=0.0, actual_exit=0.0, fees=0.0, qty=0.0):
    """
    將每筆交易的結果記錄到 trade_history.json 中，並生成 AI 友好的經驗摘要。
    """
    history_file = TRADE_HISTORY_FILE
    
    # --- 原有摩擦力計算邏輯 ---
    entry_slippage = abs(actual_entry - expected_entry) if expected_entry > 0 else 0.0
    exit_slippage = abs(actual_exit - expected_exit) if expected_exit > 0 else 0.0
    total_slippage = entry_slippage + exit_slippage
    slippage_cost = total_slippage * qty if qty > 0 else 0.0
    total_friction = slippage_cost + fees
    total_value = actual_entry * qty if (actual_entry > 0 and qty > 0) else 1.0
    friction_rate = (total_friction / total_value) * 100 if total_value > 0 else 0.0

    # --- 新增：AI 經驗摘要生成邏輯 ---
    # 根據獲利與原因，自動生成一句簡潔的摘要給 AI 看
    pnl_tag = "[大賺]" if profit_pct > 0.01 else "[微利]" if profit_pct > 0.002 else "[打平]" if profit_pct > -0.002 else "[小虧]" if profit_pct > -0.01 else "[大虧]"
    
    # 判斷是否為「異常」或「重點」交易
    is_anomaly = False
    if "Layer_1" in exit_reason or "Breakout" in exit_reason:
        is_anomaly = True
    if friction_rate > 0.4:
        is_anomaly = True

    # 組建摘要字串
    summary = f"{pnl_tag} {symbol} 透過 {exit_reason} 出場。獲利 {profit_pct*100:.2f}%，摩擦力 {friction_rate:.2f}%。"
    if is_anomaly:
        summary += " (⚠️ 異常交易，需重點關注)"

    # 準備要記錄的數據
    trade_data = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "entry_reason": entry_reason or "UNKNOWN",
        "exit_reason": exit_reason,
        "profit_pct": round(profit_pct, 4),
        "max_profit_reached": round(max_profit_reached, 4),
        "atr_at_exit": round(current_atr, 6),
        "market_mode": "High_Vol" if current_atr > 0.005 else "Low_Vol",
        "expected_entry": round(expected_entry, 6),
        "expected_exit": round(expected_exit, 6),
        "actual_entry": round(actual_entry, 6),
        "actual_exit": round(actual_exit, 6),
        "fees": round(fees, 4),
        "qty": round(qty, 4),
        "slippage": round(total_slippage, 6),
        "friction_rate": round(friction_rate, 4),
        "theoretical_profit": round((expected_exit - expected_entry)/expected_entry if expected_entry > 0 else 0.0, 4),
        "ai_summary": summary  # <--- 這是給 AI 看的核心欄位
    }

    # 讀取並寫回檔案
    if os.path.exists(history_file):
        with open(history_file, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
                if not isinstance(history, list): history = []
            except: history = []
    else:
        history = []

    history.append(trade_data)

    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=4, ensure_ascii=False)
        print(f"📝 [AI Memory] 已記錄 {symbol} 並產生摘要: {summary}")
    except Exception as e:
        print(f"⚠️ [AI Memory] 紀錄失敗: {e}")


async def _close_position_inner(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = STATES[sym]
    s["adjusted_this_tick"] = True
    if abs(s["qty"]) < 0.000001:
        return
    pk = paper_key(sym)
    qty = min(abs(qty), abs(s["qty"]))
    if qty < 0.000001:
        return

    # 動態產生損益標籤 (Reason_Tag)
    real_avg = s["avg_price"] if s["avg_price"] > 0 else avg_price
    profit_pct = (price - real_avg) / real_avg if s["qty"] > 0 else (real_avg - price) / real_avg
    
    atr_val = s.get("entry_atr", s.get("current_atr", price * 0.01))
    sl_mult = s.get("sl_atr_multiplier", 1.5)
    initial_risk_pct = (sl_mult * atr_val) / real_avg if real_avg > 0 else 0.01
    
    if profit_pct > 0 and initial_risk_pct > 0 and (profit_pct / initial_risk_pct) >= 2.0:
        pnl_tag = "[Big_Win]"
    elif profit_pct > 0.01:
        pnl_tag = "[大賺]"
    elif profit_pct > 0.002:
        pnl_tag = "[微利]"
    elif profit_pct > -0.002:
        pnl_tag = "[打平]"
    elif profit_pct > -0.01:
        pnl_tag = "[小虧]"
    else:
        pnl_tag = "[大虧]"
        
    full_reason = f"{pnl_tag} {reason}".strip()

    sanitized_qty = await sanitize_order_qty(sym, qty)
    if sanitized_qty <= 0.0:
        print(f"⚠️ [平倉風控] {sym} 無法取得有效數量 ({qty:.6f})")
        return
    # 直接使用處理過交易所精度的數量，避免因為 min() 帶回浮點數微小誤差
    qty = sanitized_qty

    if PAPER_TRADING:
        real_avg = s["avg_price"] if s["avg_price"] > 0 else avg_price
        if s["qty"] > 0:
            pnl = (price - real_avg) * qty
        else:
            pnl = (real_avg - price) * qty
        update_paper_state(pk, close_side, price, qty, is_close=True, pnl=pnl)
    else:
        try:
            await exchange_futures.create_order(sym, type='market', side=close_side, amount=qty,
                                        params={'reduceOnly': True, 'marginMode': 'isolated'})
        except Exception as e:
            print(f"🚨 [平倉錯誤] {sym}: {e}")
            return
    # 紀錄交易結果
    record_trade_result(
        symbol=sym,
        entry_reason=s.get("entry_reason", "UNKNOWN"),
        exit_reason=full_reason,
        profit_pct=profit_pct,
        current_atr=s.get("current_atr", 0.0),
        max_profit_reached=s.get("max_profit", 0.0),
        expected_entry=real_avg,
        expected_exit=price,
        actual_entry=real_avg,
        actual_exit=price,
        fees=0.0,
        qty=qty
    )


    remaining = abs(s["qty"]) - qty
    if remaining < 0.01:
        if remaining > 0.000001:
            print(f"🧹 [塵埃清理] {sym} 剩餘 {remaining:.6f} 視為已清")
        if s.get("exchange_stop_order_id") and not PAPER_TRADING:
            try:
                await exchange_futures.cancel_order(s["exchange_stop_order_id"], sym)
                print(f"✅ [止損單取消] {sym} 部位已全平，撤銷交易所止損單")
            except Exception as ce:
                print(f"⚠️ [取消止損單失敗] {sym}: {ce}")
                
        mark_exit(sym, is_stop_loss=is_stop_loss, reason=full_reason)
        reset_coin_state(sym)
    else:
        prec = await get_contract_precision(sym)
        raw_qty = (abs(s["qty"]) - qty) * (1 if s["qty"] > 0 else -1)
        s["qty"] = round_step(raw_qty, prec['step_size'])
        print(f"✅ [部分平] {sym} 平{qty} 剩{abs(s['qty']):.4f} {full_reason}")
        
        if s.get("exchange_stop_order_id") and not PAPER_TRADING:
            try:
                await exchange_futures.cancel_order(s["exchange_stop_order_id"], sym)
                stop_side = 'sell' if s["qty"] > 0 else 'buy'
                hard_sl_pct = s.get("hard_stop_loss_pct", 0.02)
                stop_price = s["avg_price"] * (1 - hard_sl_pct) if s["qty"] > 0 else s["avg_price"] * (1 + hard_sl_pct)
                stop_price = round_step(stop_price, prec['tick_size'])
                new_stop = await exchange_futures.create_order(
                    sym, type='STOP_MARKET', side=stop_side, amount=abs(s["qty"]),
                    params={'stopPrice': stop_price, 'reduceOnly': True}
                )
                s["exchange_stop_order_id"] = new_stop['id']
                print(f"🛡️ [止損單更新] {sym} 部分平倉後更新止損單 @ {stop_price} (數量: {abs(s['qty'])})")
            except Exception as ce:
                print(f"⚠️ [更新止損單失敗] {sym}: {ce}")


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
    prev_bar_high = s["ohlcv"][-2][2]
    prev_bar_low = s["ohlcv"][-2][3]
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
        # 防插針量能檢查
        current_vol = s.get("current_vol", 0.0)
        vol_ma20 = s.get("vol_ma20", 1.0)
        vol_ratio = current_vol / vol_ma20 if vol_ma20 > 0 else 1.0
        
        if vol_ratio > 2.5:
            print(f"⚠️ [防插針豁免] {sym} 瞬時爆發量 (Ratio: {vol_ratio:.2f}x)，視為真崩盤，取消盲區保護！")
        else:
            return

    p = s["close_price"]
    avg = s["avg_price"]
    is_long = s["qty"] > 0
    profit_pct = (p - avg) / avg if is_long else (avg - p) / avg


    # cooldown_limit 過後才進此函數，所以 120 秒邊界仍有意義（低波動情況下 60~120 秒區間）
    sl_base = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long)
    sl_mult = sl_base * 1.5 if hold_sec < 120 else sl_base
    atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)
    tp_base = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), is_long)
    
    # ── 加入最低距離保護 (Minimum Distance Floor) ──
    sl_dist = max(sl_mult * atr_val, avg * 0.005)
    tp_dist = max(tp_base * atr_val, avg * 0.015)
    
    tp = avg + tp_dist if is_long else avg - tp_dist

    # ── 動態保本防護 (Dynamic Breakeven) ──
    # 只要利潤達到 1 倍 ATR (或至少 0.2%)，就將停損永久上移至保本點 (+0.15% 確保夠付手續費)
    entry_atr_pct = (s.get("entry_atr", atr_val) / avg) if avg > 0 else 0.002
    breakeven_threshold = max(entry_atr_pct * 0.6, 0.0015)
    if s.get("highest_profit_pct", 0.0) >= breakeven_threshold:
        atr_half = s.get("current_atr", atr_val) * 0.5
        sl = avg + atr_half if is_long else avg - atr_half
    else:
        sl = avg - sl_dist if is_long else avg + sl_dist

    # --- 停損同步 (Trailing SL Sync) - Philosophy B+ ---
    if s.get("entry_count", 0) > 0:
        first_entry = s.get("first_entry_price", avg)
        atr_half = s.get("current_atr", atr_val) * 0.5
        
        if is_long:
            sl_floor = first_entry - atr_half
            sl = max(sl, sl_floor)
        else:
            sl_floor = first_entry + atr_half
            sl = min(sl, sl_floor)

    if profit_pct > s["highest_profit_pct"]:
        s["highest_profit_pct"] = profit_pct
    if profit_pct < 0:
        s["has_been_negative"] = True

    regime_decision, regime_reason = detect_market_regime(sym, p, avg, is_long)
    if regime_decision == "BREAKOUT_REVERSAL":
        cs = 'sell' if is_long else 'buy'
        print(f"🚨 [市場 regime] {sym} {regime_reason}，立即平倉並反手")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Breakout_Fail]_Fail]", is_stop_loss=True)
        s["highest_profit_pct"] = 0.0
        return


    if regime_decision == "RANGE_PROFIT_TAKE":
        cs = 'sell' if is_long else 'buy'
        print(f"📈 [盤整獲利] {sym} {regime_reason}，提前獲利了結")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
        s["highest_profit_pct"] = 0.0
        return

    # 動能衰減檢查：從最高點回落
    s["pnl_history"].append(profit_pct * 100)
    if len(s["pnl_history"]) > 8:
        s["pnl_history"].pop(0)
        
    if profit_pct > 0.015 and s["highest_profit_pct"] > 0.015:
        drawdown = (s["highest_profit_pct"] - profit_pct) / s["highest_profit_pct"]
        if drawdown >= 0.25:
            cs = 'sell' if is_long else 'buy'
            print(f"📉 [動能衰減] {sym} 利潤從最高 {s['highest_profit_pct']*100:.2f}% 回落 25% (現為 {profit_pct*100:.2f}%)，提早獲利了結")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Whipsaw_Stop]top]")
            s["highest_profit_pct"] = 0.0
            return
    if p > s["trailing_highest"]:
        s["trailing_highest"] = p
    if p < s["trailing_lowest"]:
        s["trailing_lowest"] = p

    # 1. 趨勢反轉：MACD 狀態反向 → 立即認賠出場 (修正盲區，不只看交叉瞬間)
    macd_is_down = s["macd_line"] < s["macd_signal"]
    macd_is_up = s["macd_line"] > s["macd_signal"]
    sl_pct = s.get("hard_stop_loss_pct", 0.02)
    early_exit_limit = -(sl_pct * 0.5)
    if ((is_long and macd_is_down) or (not is_long and macd_is_up)) and (profit_pct < early_exit_limit or profit_pct > 0.015):
        cs = 'sell' if is_long else 'buy'
        is_sl = profit_pct < 0.0
        print(f"📉 [反轉出場] {sym} MACD狀態反向且達門檻，立即平倉 (損益: {profit_pct*100:.2f}%)")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Trend_Follow]", is_stop_loss=is_sl)
        return

    # 2. 判斷市場狀態：強勢 / 弱勢
    is_strong = (is_long and s["current_rsi"] > 50) or (not is_long and s["current_rsi"] <= 50)

    # ── 保本鎖利與利潤防護機制 (Break-even & Capital Protection Lock) ──
    # 實施分級保本與回撤防護，防止「有利潤不平倉，最後被打到停損」
    
    # 趨勢確認：如果 MACD 仍在趨勢方向，則放寬鎖利條件
    is_trend_ok = (is_long and s["macd_line"] > s["macd_signal"]) or (not is_long and s["macd_line"] < s["macd_signal"])
    
    # ── 動態 ATR 鎖利門檻 (Dynamic ATR Lock) ──
    # 取代固定 %，改用 ATR 倍數，讓低波動幣種也能被及時保護
    atr_pct = (s.get("entry_atr", atr_val) / avg) if avg > 0 else 0.002
    
    tier3_target = max(atr_pct * 4.0, 0.012)
    tier2_target = max(atr_pct * 2.5, 0.006)
    tier1_target = max(atr_pct * 1.5, 0.0035)

    # ── 動能竭盡 (量價背離) 頂部逃頂機制 ──
    # 只要出現價格創新高/低但量能急縮，即視為動能竭盡，無條件平倉 (移除%限制)
    if len(s["ohlcv"]) >= 3:
        # 只看已經收盤的 K 線，避免被當前正在跳動的未收盤 K 線誤導
        c1 = s["ohlcv"][-2]  # 最新已收盤 K 線
        c2 = s["ohlcv"][-3]  # 前一根已收盤 K 線
        
        divergence_exit = False
        if is_long and c1[4] > c2[4] and c1[5] < c2[5] * 0.65:
            # 多單：價格創高，但量能急縮 (< 65%)
            divergence_exit = True
        elif not is_long and c1[4] < c2[4] and c1[5] < c2[5] * 0.65:
            # 空單：價格創低，但量能急縮 (< 65%)
            divergence_exit = True
            
        if divergence_exit:
            cs = 'sell' if is_long else 'buy'
            print(f"📉 [量價背離] {sym} 價格創高/低但量能急縮 (V:{c1[5]:.0f} < V_prev:{c2[5]:.0f}*0.65)，動能竭盡提前平倉！")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Vol_Divergence]")
            s["highest_profit_pct"] = 0.0
            return

    if not is_strong:
        macd_hist_expanding = False
        if len(s.get("ohlcv", [])) >= 34:
            closes = np.array([x[4] for x in s["ohlcv"]])
            try:
                # We can compute MACD or just use a simple heuristic if calculate_macd isn't accessible
                # Let's rely on s["macd_hist"] and assume we can approximate expansion
                pass
            except:
                pass
        
        # Simplified momentum expansion check without recalculating MACD arrays:
        # We assume if the current price is continuing the trend powerfully, momentum is expanding.
        # But better: calculate_macd returns 5 values (macd, signal, hist, prev_macd, prev_sig) in this codebase!
        # Wait, the codebase has `calculate_macd(closes)` returning `macd_line, macd_signal, macd_hist, prev_macd_line, prev_macd_signal`.
        # So we can calculate it:
        try:
            closes = np.array([x[4] for x in s["ohlcv"]])
            _, _, m_hist, p_line, p_sig = calculate_macd(closes)
            p_hist = p_line - p_sig
            macd_hist_expanding = abs(m_hist) > abs(p_hist)
        except:
            macd_hist_expanding = False

        if s["highest_profit_pct"] >= tier3_target and profit_pct < s["highest_profit_pct"] * (0.8 if is_trend_ok else 0.6):
            if macd_hist_expanding:
                print(f"⚡ [強勢保留] {sym} 獲利達大行情水準，雖回撤20%但動能仍在擴張，暫不鎖利！")
            else:
                cs = 'sell' if is_long else 'buy'
                print(f"🛡️ [大行情鎖利] {sym} 獲利達 {s['highest_profit_pct']*100:.3f}%(>4ATR)，觸發大行情回撤平倉")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Whipsaw_Stop]")
                s["highest_profit_pct"] = 0.0
                return
        elif s["highest_profit_pct"] >= tier2_target and profit_pct < s["highest_profit_pct"] * (0.7 if is_trend_ok else 0.5):
            cs = 'sell' if is_long else 'buy'
            print(f"🛡️ [中利鎖利] {sym} 獲利達 {s['highest_profit_pct']*100:.3f}%(>2.5ATR)，回落至 {profit_pct*100:.3f}% 平倉")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
            s["highest_profit_pct"] = 0.0
            return
        elif s["highest_profit_pct"] >= tier1_target and profit_pct < s["highest_profit_pct"] * 0.5:
            cs = 'sell' if is_long else 'buy'
            print(f"🛡️ [基本鎖利] {sym} 獲利達 {s['highest_profit_pct']*100:.3f}%(>1.5ATR)，回落至 {profit_pct*100:.3f}% 保護平倉")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
            s["highest_profit_pct"] = 0.0
            return

    # 取消固定百分比停利，改由移動停損 (Trailing Stop) 統一接管，以利捕捉最大波段

    if not is_strong:
        # ── 盤整／弱勢路線 ────────────────────────────────
        # 僵局一階：時間到 → 有任何正利潤就全平，利潤微薄(0.2%~0.5%)平50%
        stagnation_limit = get_dynamic_stagnation_limit(s["current_atr"], s["atr_ma20"])
        if hold_sec > stagnation_limit and profit_pct > 0.003:
            if not s["has_partial_closed"] and 0.003 <= profit_pct < 0.008:
                half = abs(s["qty"]) * 0.5
                cs = 'sell' if is_long else 'buy'
                print(f"⏳ [僵局一階] {sym} 持倉{stagnation_limit//60}分利潤{profit_pct*100:.2f}%，平50%")
                await close_position(sym, cs, half, p, avg, reason="[Stagnation_1]")
                s["has_partial_closed"] = True
                return
            elif profit_pct >= 0.008:
                cs = 'sell' if is_long else 'buy'
                print(f"⏳ [僵局平倉] {sym} 持倉{stagnation_limit//60}分利潤{profit_pct*100:.2f}%，全平")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Stagnation_Exit]")
                s["highest_profit_pct"] = 0.0
                return
        # 僵局二階：平過50% + 8分仍未突破1% → 全平
        if s["has_partial_closed"] and hold_sec > 480 and profit_pct < 0.01:
            cs = 'sell' if is_long else 'buy'
            print(f"⏳ [僵局二階] {sym} 剩餘50%持倉8分仍未突破1%，全平")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Stagnation_2]")
            s["highest_profit_pct"] = 0.0
            s["has_partial_closed"] = False
            return
        # 弱勢快速停利：穩健型幣種可以等更久，再決定是否落袋
        weak_tp = 0.015
        if s.get("personality") == "calm":
            weak_tp = 0.02
        if s["highest_profit_pct"] >= weak_tp:
            if not has_strong_momentum(sym, is_long):
                cs = 'sell' if is_long else 'buy'
                print(f"🎯 [快速停利] {sym} 弱勢利潤達{weak_tp*100:.1f}%，動能不足則落袋")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
                s["highest_profit_pct"] = 0.0
                return
            print(f"⚡ [保留動能] {sym} 弱勢已達{weak_tp*100:.1f}%但動能仍強，暫不停利")
    else:
        # ── 強勢路徑完全交給 ATR 移動停損 (Trailing Stop) 接管，讓利潤盡情奔跑

        # 強勢動態停利：高點回撤 0.5%
        if s["highest_profit_pct"] >= 0.01:
            if (is_long and p <= s["trailing_highest"] * 0.990) or (not is_long and p >= s["trailing_lowest"] * 1.010):
                cs = 'sell' if is_long else 'buy'
                print(f"🏃 [動態停利] {sym} 強勢回撤0.5%")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Trend_Follow]")
                s["highest_profit_pct"] = 0.0
                return
        # 強勢 ATR TP/SL
        if (is_long and p >= tp) or (not is_long and p <= tp):
            cs = 'sell' if is_long else 'buy'
            tp_pct = abs(tp - avg) / avg * 100
            print(f"🎯 [ATR停利] {sym} {tp_pct:.1f}%")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
            return
        if (is_long and p <= sl) or (not is_long and p >= sl):
            cs = 'sell' if is_long else 'buy'
            sl_pct = abs(sl - avg) / avg * 100
            reason_str = "[Breakeven_Stop]" if sl == avg else "[Trend_Follow]"
            print(f"🛑 [{reason_str}] {sym} -{sl_pct:.1f}%")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason_str, is_stop_loss=True)
            return

async def check_position_exits(exchange, sym):
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

    # [防插針與連續洗盤保護] 如果在 5 分鐘內已經發生過平倉/停損，暫停非緊急出單
    if time.time() - s.get("last_flip_time", 0) < 300 and "Stop" in s.get("last_exit_reason", ""):
        # 給予 300 秒的緩衝期，避免被連續插針洗盤
        return

    # 1. 取得 ATR 停利停損倍數
    sl_multiplier = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long)
    tp_multiplier = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), is_long)
    atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)

    # 初始的距離 (加入最小停損與停利距離保護)
    sl_dist = max(atr_val * sl_multiplier, p * 0.005)
    tp_dist = max(atr_val * tp_multiplier, p * 0.015)

    # 定義初始 TP/SL 價格
    initial_tp = avg + tp_dist if is_long else avg - tp_dist
    initial_sl = avg - sl_dist if is_long else avg + sl_dist

    # 初始化 s["dynamic_sl"] 如果還沒有
    if s.get("dynamic_sl", 0.0) == 0.0:
        s["dynamic_sl"] = initial_sl

    # 2. 保本與移動停損邏輯
    # 判斷是否達到 1:1 盈虧比 (利潤超過初始風險距離)
    profit_dist = (p - avg) if is_long else (avg - p)
    
    if profit_dist >= sl_dist * 1.5:
        # 達到 1.5 倍風險距離才啟動保本/追蹤，確保停損位移到保本點 (含 0.25% 手續費與滑價緩衝)
        breakeven_sl = avg * 1.0025 if is_long else avg * 0.9975
        
        # 接著，如果利潤繼續拉開，使用 2.2 * ATR 進行追蹤止損，給予更大的呼吸空間
        trail_dist = atr_val * 2.2
        trail_sl = p - trail_dist if is_long else p + trail_dist

        # 決定最終的動態停損位 (只會往有利方向移動)
        if is_long:
            new_sl = max(breakeven_sl, trail_sl)
            if new_sl > s.get("dynamic_sl", 0.0):
                s["dynamic_sl"] = new_sl
                print(f"🛡️ [動態停損] {sym} 移至 {new_sl:.6f} (保本/追蹤)")
        else:
            new_sl = min(breakeven_sl, trail_sl)
            current_dyn_sl = s.get("dynamic_sl", float('inf'))
            if current_dyn_sl == 0.0 or new_sl < current_dyn_sl:
                s["dynamic_sl"] = new_sl
                print(f"🛡️ [動態停損] {sym} 移至 {new_sl:.6f} (保本/追蹤)")
                 
    # 3. 執行停利或停損
    cs = 'sell' if is_long else 'buy'
    
    # 檢查是否觸發停損 (Dynamic SL or Hard Stop)
    hard_stop_loss_pct = get_effective_exit_setting(sym, "hard_stop_loss_pct", s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT), is_long)
    hard_sl = avg * (1 - hard_stop_loss_pct) if is_long else avg * (1 + hard_stop_loss_pct)
    
    active_sl = max(s["dynamic_sl"], hard_sl) if is_long else min(s["dynamic_sl"], hard_sl)
    
    if (is_long and p <= active_sl) or (not is_long and p >= active_sl):
        is_high_volume = s.get("current_vol", 0) > s.get("vol_ma20", 0) * 1.5
        
        if is_high_volume:
            reason = "[Trend_Follow]"
            print(f"🛑 [{reason}] {sym} 觸發價格 {active_sl:.6f} (現價:{p:.6f})")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason, is_stop_loss=(profit_pct < 0))
            s["sl_trigger_time"] = 0
            return
        else:
            if s.get("sl_trigger_time", 0) == 0:
                s["sl_trigger_time"] = time.time()
                print(f"⚠️ [防插針觀察] {sym} 觸發停損 {active_sl:.6f} 但量能小 ({s.get('current_vol',0):.2f} < {s.get('vol_ma20',0)*1.5:.2f})，進入 2 秒觀察期...")
                return
            elif time.time() - s["sl_trigger_time"] >= 2.0:
                reason = "[Trend_Follow]"
                print(f"🛑 [{reason}] {sym} 持續觸發停損超過 2 秒，確認執行！")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason, is_stop_loss=(profit_pct < 0))
                s["sl_trigger_time"] = 0
                return
            else:
                return
    else:
        s["sl_trigger_time"] = 0

    # 檢查是否觸發分批停利 (Partial Close at 1.5 ATR or 0.8%)
    partial_tp_dist = max(atr_val * 2.0, p * 0.012)
    partial_tp_price = avg + partial_tp_dist if is_long else avg - partial_tp_dist
    if not s.get("has_partial_closed", False) and ((is_long and p >= partial_tp_price) or (not is_long and p <= partial_tp_price)):
        half_qty = abs(s["qty"]) * 0.5
        if half_qty >= (s.get("min_qty", 0.001) if "min_qty" in s else 0.0):
            print(f"🎯 [分批停利] {sym} 觸發 1.5 ATR 或 0.8% 利潤，先平倉 50% 落袋為安")
            await close_position(sym, cs, half_qty, p, avg, reason="分批停利 50%")
            s["has_partial_closed"] = True
            # 不 return，讓剩餘倉位繼續走下面的追蹤邏輯

    # 檢查是否觸發最終停利 (TP)
    if (is_long and p >= initial_tp) or (not is_long and p <= initial_tp):
        # 強勢行情不摸頂 (Let Profits Run)
        rsi = s.get("current_rsi", 50.0)
        if (is_long and rsi > 75) or (not is_long and rsi < 25):
            print(f"🚀 [強勢行情] {sym} 觸發停利點，但 RSI 極度強勢 ({rsi:.1f})，不全平倉，改為極限追蹤停損！")
            trail_extreme = p - atr_val * 0.5 if is_long else p + atr_val * 0.5
            s["dynamic_sl"] = trail_extreme
        else:
            print(f"🎯 [ATR停利] {sym} 觸發價格 {initial_tp:.6f} (現價:{p:.6f})")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="ATR停利")
            return

    # 多層次時間停損
    if hold_sec > 3600 and profit_pct < 0.0:
        print(f"⏱️ [超時停損] {sym} 持倉過久 ({hold_sec/60:.1f}分) 且未獲利，釋放資金")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="超時無利潤出場", is_stop_loss=True)
        return
    elif hold_sec > 900 and profit_pct < -0.005:
        print(f"⏱️ [時間停損] {sym} {hold_sec/60:.1f}分仍顯著虧損")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="時間停損", is_stop_loss=True)
        return


# ── 進場邏輯 ──────────────────────────────────────────────────

async def execute_order(sym, side, price):
    s = STATES[sym]
    pk = paper_key(sym)
    lev = get_symbol_leverage(sym)
    s["leverage"] = lev
    print(f"@@LEVERAGE@@{lev}")
    if not PAPER_TRADING:
        try:
            await exchange_futures.set_leverage(lev, convert_to_ccxt_symbol(sym))
        except Exception as e:
            print(f"⚠️ [槓桿設定失敗] {sym}: {e}")
            
    margin = compute_per_coin_margin(sym)
    


    if margin <= 0:
        print(f"⚠️ [風控] {sym} 無可用保證金")
        return

    # --- 價格偏離檢查 ---
    try:
        ticker = await exchange_futures.fetch_ticker(sym)
        market_price = ticker.get('last')
        if market_price and market_price > 0:
            deviation = abs(price - market_price) / market_price
            if deviation > 0.05:
                print(f"🚨 [風控] {sym} 訂單價格 {price} 嚴重偏離市價 {market_price} (偏離 {deviation*100:.2f}%)，拒絕執行！")
                return
    except Exception as e:
        print(f"⚠️ [價格偏離檢查失敗] {e}")
    # --------------------

    now = time.time()
    if s["entry_count"] > 0:
        if now - s["last_entry_time"] < s["entry_cooldown_sec"]:
            print(f"⏳ [加倉冷卻] {sym} 距離上次加倉不足 {s['entry_cooldown_sec']} 秒")
            return
        if s["entry_count"] >= 3:
            print(f"⚠️ [加倉上限] {sym} 已達絕對層數上限 (3層)")
            return
            
        # [加倉防護 1] 虧損加倉防護
        avg_price = s.get("avg_price", 0.0)
        if avg_price > 0:
            profit_pct = (price - avg_price) / avg_price if side == 'buy' else (avg_price - price) / avg_price
            if profit_pct < 0.003:
                print(f"🛑 [虧損加倉防護] {sym} 目前利潤 {profit_pct*100:.2f}% 不足 0.3%，拒絕加倉！")
                return

        # [加倉防護 2] 價格大幅反轉過濾
        last_entry_price = s.get("last_entry_price", avg_price)
        if last_entry_price > 0:
            reversal = (last_entry_price - price) / last_entry_price if side == 'buy' else (price - last_entry_price) / last_entry_price
            if reversal > 0.01:
                print(f"🛑 [反轉過濾] {sym} 價格與上次加倉發生大幅反轉 ({reversal*100:.2f}% > 1%)，拒絕加倉！")
                return

        # [加倉防護 3] 動能一致性
        current_vol = s.get("current_vol", 0.0)
        vol_ma20 = s.get("vol_ma20", 1e-8)
        if current_vol < vol_ma20 * 0.8:
            print(f"🛑 [量能過濾] {sym} 當前量能低於均量 0.8 倍，動能不足拒絕加倉！")
            return
            
        # 動能斜率判斷: 最近兩根K線的漲跌幅度是否縮小
        if len(s.get("ohlcv", [])) >= 3:
            c1 = s["ohlcv"][-2]  # 最新已收盤 K 線
            c2 = s["ohlcv"][-3]  # 前一根已收盤 K 線
            body1 = abs(c1[4] - c1[1])
            body2 = abs(c2[4] - c2[1])
            vol1 = c1[5]
            vol2 = c2[5]
            
            is_bull1 = c1[4] > c1[1]
            is_bull2 = c2[4] > c2[1]
            
            if side == 'buy' and is_bull1 and is_bull2 and body1 < body2 * 0.8 and vol1 < vol2 * 0.8:
                print(f"🛑 [斜率過濾] {sym} 價格創高但實體與量能雙雙衰減，動能不足拒絕加碼！")
                return
            if side == 'sell' and not is_bull1 and not is_bull2 and body1 < body2 * 0.8 and vol1 < vol2 * 0.8:
                print(f"🛑 [斜率過濾] {sym} 價格創低但實體與量能雙雙衰減，動能不足拒絕加碼！")
                return
            
        # [加倉防護 4] 方向確認 (確保不在逆勢接刀)
        if len(s.get("ohlcv", [])) >= 2:
            current_close = s["ohlcv"][-1][4]
            prev_close = s["ohlcv"][-2][4]
            if side == 'buy' and current_close <= prev_close:
                print(f"🛑 [方向確認] {sym} 多單加倉失敗，當前收盤價未高於前K線，拒絕接刀！")
                return
            if side == 'sell' and current_close >= prev_close:
                print(f"🛑 [方向確認] {sym} 空單加倉失敗，當前收盤價未低於前K線，拒絕接刀！")
                return

        # 1. 空間關卡 (Space Check): 距離上一次加倉是否大於 1.5 * ATR
        current_atr = s.get("current_atr", 0.0)
        last_entry_price = s.get("last_entry_price", s.get("avg_price", 0.0))
        if last_entry_price > 0 and current_atr > 0:
            price_diff = abs(price - last_entry_price)
                    # 動態空間門檻 (依幣種性格)
        personality = s.get("personality", "balanced")
        if personality in ["trend_follower", "breakout_chaser"]: # Core_Trend
            floor_pct = 0.004
        elif personality in ["mean_reversion", "contrarian"]: # Speculative
            floor_pct = 0.002
        else: # High_Beta / Balanced
            floor_pct = 0.003
        if price_diff < max(1.5 * current_atr, price * floor_pct):
                print(f"🛑 [空間關卡] {sym} 加倉距離不足! 差距: {price_diff:.4f} < 門檻: {1.5 * current_atr:.4f}")
                return
                
        # 2. 動能關卡 (Momentum Check): 量能與 MACD 雙重確認
        if not is_entry_volume_confirmed(sym, side):
            print(f"🛑 [動能關卡] {sym} 量能不足以支持加倉!")
            return
            
        macd_line = s.get("macd_line", 0.0)
        macd_signal = s.get("macd_signal", 0.0)
        prev_macd_line = s.get("prev_macd_line", 0.0)
        prev_macd_signal = s.get("prev_macd_signal", 0.0)
        macd_hist = macd_line - macd_signal
        prev_macd_hist = prev_macd_line - prev_macd_signal
        
        # 確保方向一致
        if (side == 'buy' and macd_hist <= 0) or (side == 'sell' and macd_hist >= 0):
            print(f"🛑 [動能關卡] {sym} MACD動能不一致 (Hist: {macd_hist:.4f})，拒絕加倉!")
            return
            
        # 確保動能擴張 (MACD 柱線絕對值變長)
        # 允許動能微幅縮減 (只要沒有大幅衰退 > 30%)
        if abs(macd_hist) < abs(prev_macd_hist) * 0.7:
            print(f"🛑 [動能關卡] {sym} MACD動能大幅衰竭 (Hist: {abs(macd_hist):.5f} < Prev: {abs(prev_macd_hist):.5f}*0.7)，拒絕加倉!")
            return

        # 3. 原有的保本檢查
        if s["avg_price"] > 0 and s["close_price"] > 0:
            profit_pct = (s["close_price"] - s["avg_price"]) / s["avg_price"] if side == 'buy' else (s["avg_price"] - s["close_price"]) / s["avg_price"]
            if profit_pct < 0.001:
                print(f"🛑 [保本關卡] {sym} 目前尚未回到保本線以上，不加倉 (利潤: {profit_pct*100:.2f}%)")
                return

    # 1. 計算目標名義價值 (保證金 * 槓桿倍數)
    target_notional = margin * lev
    
    # 2. 遞減式金字塔加倉比例 (Decreasing Allocation)
    if s["entry_count"] == 0:
        allocation_pct = 0.40  # 首倉 40%
    elif s["entry_count"] == 1:
        allocation_pct = 0.30  # 次倉 30%
    else:
        allocation_pct = 0.30  # 再倉 30%
    base_notional = target_notional * allocation_pct
    
    # 最低加倉門檻保護 (確保滿足幣安合約最小下單金額 5~10 USDT)
    if base_notional < 10.0 and margin * lev >= 10.0:
        base_notional = 10.0
    
    # 3. 最大名義價值限制與風險關卡 (Risk Check)
    balance = get_balance()
    max_notional = min(1000.0, balance * 0.3)  # 絕對最大名義價值 1000 USDT
    if base_notional > max_notional:
        base_notional = max_notional
        
    # 4. 資金關卡與餘額檢查 (Capital Check)
    required_margin = base_notional / lev
    
    if not PAPER_TRADING:
        try:
            bal = await exchange_futures.fetch_balance()
            free_usdt = float(bal.get("USDT", {}).get("free", 0.0))
            # 資金關卡：確保加倉後系統依然保留總資金 10% 的可用餘額做為緩衝
            safe_free_usdt = max(0.0, free_usdt - (balance * 0.2))
            if required_margin > safe_free_usdt:
                print(f"⚠️ [資金關卡] {sym} 扣除 20% 緩衝後，安全餘額 {safe_free_usdt:.2f} < 所需保證金 {required_margin:.2f}，自動降至安全餘額下單！")
                base_notional = safe_free_usdt * lev
        except Exception as e:
            print(f"⚠️ [餘額檢查失敗] {e}")
    else:
        if required_margin > balance * 0.98:
            base_notional = (balance * 0.98) * lev

    # 5. 轉換為幣種數量並進行精度修剪
    base_amt = base_notional / price
    base_amt = await sanitize_order_qty(sym, base_amt)
    
    # 6. 幣安最小下單額限制 (Min Notional Check)
    actual_notional = base_amt * price
    if actual_notional < 6.0 and actual_notional > 0:  # 幣安合約最小下單通常為 5 USDT，抓 6 比較保險
        # 嘗試補足到 6 USDT
        min_qty = 6.0 / price
        min_qty = await sanitize_order_qty(sym, min_qty)
        # 如果補足後保證金不夠，就放棄
        if (min_qty * price) / lev > balance * 0.98:
            print(f"⚠️ [風控] {sym} 資金不足以達到最小開倉額度 6 USDT (餘額: {balance:.2f})")
            return
        base_amt = min_qty
        actual_notional = base_amt * price

    if base_amt <= 0.0:
        print(f"⚠️ [風控] {sym} 計算後開倉數量為 0")
        return

    if PAPER_TRADING:
        try:
            update_paper_state(pk, side, price, base_amt)
            if side == 'buy':
                prev_qty = abs(s["qty"])
                s["qty"] += base_amt
            else:
                prev_qty = abs(s["qty"])
                s["qty"] -= base_amt
            
            if s["avg_price"] <= 0:
                s["avg_price"] = price
                s["entry_atr"] = max(s.get("current_atr", 0.0), fill_price * 0.005 if "fill_price" in locals() else price * 0.005)
            else:
                s["avg_price"] = ((s["avg_price"] * prev_qty) + (price * base_amt)) / abs(s["qty"])
            s["open_time"] = now
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["last_entry_price"] = price
            s["entry_count"] += 1
            direction = "做多" if side == 'buy' else "做空"
            print(f"🟢 [{direction}] {sym} {base_amt:.4f} @ {price} (保證金:{margin:.2f} USDT)")
        except Exception as e:
            print(f"🛑 [模擬開倉失敗] {sym}: {e}")
    else:
        try:
            order = await exchange_futures.create_order(sym, type='market', side=side, amount=base_amt,
                                                params={'marginMode': 'isolated'})
            fill_price = float(order.get('average') or order.get('price') or price)
            if fill_price <= 0:
                fill_price = price
                
            slippage = (fill_price - price) / price if price > 0 else 0
            if side == 'sell':
                slippage = (price - fill_price) / price if price > 0 else 0
                
            print(f"✅ [實盤開倉成功] {sym} {side} | 預期: {price:.6f} | 實際: {fill_price:.6f} | 滑價: {slippage*100:.3f}%")
            
            old_qty = s["qty"]
            if side == 'buy':
                s["qty"] += base_amt
            else:
                s["qty"] -= base_amt
                
            if s["avg_price"] <= 0:
                s["avg_price"] = fill_price
                s["entry_atr"] = max(s.get("current_atr", 0.0), fill_price * 0.005 if "fill_price" in locals() else price * 0.005)
            else:
                s["avg_price"] = ((s["avg_price"] * abs(old_qty)) + (fill_price * base_amt)) / abs(s["qty"])
                
            s["open_time"] = now
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["last_entry_price"] = price
            s["entry_count"] += 1
            s["last_flip_time"] = now
            
            # --- 混合停損: 交易所掛單 (Stop Market) ---
            try:
                stop_side = 'sell' if s["qty"] > 0 else 'buy'
                hard_sl_pct = s.get("hard_stop_loss_pct", 0.02)
                stop_price = s["avg_price"] * (1 - hard_sl_pct) if s["qty"] > 0 else s["avg_price"] * (1 + hard_sl_pct)
                prec = await get_contract_precision(sym)
                stop_price = round_step(stop_price, prec['tick_size'])
                
                if s.get("exchange_stop_order_id"):
                    try:
                        await exchange_futures.cancel_order(s["exchange_stop_order_id"], sym)
                    except Exception as ce:
                        print(f"⚠️ [取消舊止損單失敗] {sym}: {ce}")
                
                stop_order = await exchange_futures.create_order(
                    sym, type='STOP_MARKET', side=stop_side, amount=abs(s["qty"]),
                    params={'stopPrice': stop_price, 'reduceOnly': True}
                )
                s["exchange_stop_order_id"] = stop_order['id']
                print(f"🛡️ [交易所掛單] {sym} 成功掛出 Stop Market 止損單 @ {stop_price} (數量: {abs(s['qty'])})")
            except Exception as se:
                print(f"🚨 [交易所止損掛單失敗] {sym}: {se}")
            # ----------------------------------------
            
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

    pin_threshold = 4.0
    candle_range = max(high - low, 1e-8)
    body_ratio = body / candle_range
    if body_ratio < 0.35 or s.get("current_vol", 0.0) < max(100.0, s.get("vol_ma20", 0.0) * 0.5):
        pin_threshold = 3.0
    ema20 = s.get("ema20", 0.0)
    if ema20 > 0:
        if side == 'buy' and close_price < ema20:
            pin_threshold = 3.0
        if side == 'sell' and close_price > ema20:
            pin_threshold = 3.0

    enabled = pin_threshold < 4.0
    if enabled:
        print(f"@@COIN_DEBUG@@ 🔧 {sym} 反插針門檻收緊為 {pin_threshold:.1f} (body_ratio={body_ratio:.2f}, vol={s.get('current_vol',0):.0f}, ema20={ema20:.4f}) [enabled]")
    else:
        print(f"@@COIN_DEBUG@@ 🔎 {sym} 反插針門檻維持寬鬆 {pin_threshold:.1f} [disabled]")

    if side == 'buy':
        if close_price <= prev_close:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 收盤價 {close_price:.4f} <= 前K收盤 {prev_close:.4f}")
            return False
        if upper_wick > body * pin_threshold:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 上影線過長 (上影線 {upper_wick:.4f} > 實體 {body:.4f} * {pin_threshold:.1f})")
            return False
        return True

    if close_price >= prev_close:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 收盤價 {close_price:.4f} >= 前K收盤 {prev_close:.4f}")
        return False
    if lower_wick > body * pin_threshold:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 下影線過長 (下影線 {lower_wick:.4f} > 實體 {body:.4f} * {pin_threshold:.1f})")
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
    
    # [Layer 3] 動態量能門檻：嚴格爆發 (至少 1.5 倍)
    vol_factor = max(1.5, s.get("volume_threshold_factor", 1.5))
        
    min_volume = vol_ma20 * vol_factor
    if current_vol < min_volume:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能過濾] 當前量 {current_vol:.2f} < 門檻 {min_volume:.2f} (均量:{vol_ma20:.2f} * {vol_factor})")
        return False

    # --- R:R (盈虧比) 過濾 ---
    is_long = (side == 'buy')
    sl_multiplier = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long)
    tp_multiplier = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), is_long)
    
    expected_profit = tp_multiplier * s.get("current_atr", 0.0)
    expected_risk = sl_multiplier * s.get("current_atr", 0.0)
    
    rr_ratio = expected_profit / expected_risk if expected_risk > 0 else 0
    if rr_ratio < 1.99:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [盈虧比過濾] 預計R:R ({rr_ratio:.2f}) < 2.0 (TP: {tp_multiplier}x, SL: {sl_multiplier}x)")
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
    
    # 均線過濾器已移除 - 寬鬆進場模式允許價格在SMA200上下開單
    # if s.get("sma200_15m", 0) > 0:
    #     ma200 = s["sma200_15m"]
    #     if side == 'buy' and cp <= ma200:
    #         return False
    #     if side == 'sell' and cp >= ma200:
    #         return False
            
    if len(s["ohlcv"]) < 20:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線不足] 當前長度 {len(s['ohlcv'])} < 20")
        return False
        
    # --- MTF 1H & 15m 趨勢過濾 (強化防護) ---
    if s.get("mtf_filter", True):
        ema50_1h = s.get("ema50_1h", 0)
        sma200_15m = s.get("sma200_15m", 0)
        
        if ema50_1h > 0:
            if side == 'buy' and cp <= ema50_1h:
                print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [MTF過濾] 1H大趨勢向下 (現價 {cp:.4f} <= 1H_EMA50 {ema50_1h:.4f})，禁止逆勢做多")
                return False
            # 空單強化過濾：必須同時低於 1H EMA50 與 15m SMA200，確保上方有重重反壓才准空
            if side == 'sell':
                if cp >= ema50_1h:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [MTF過濾] 1H大趨勢向上 (現價 {cp:.4f} >= 1H_EMA50 {ema50_1h:.4f})，禁止逆勢做空")
                    return False
                if sma200_15m > 0 and cp >= sma200_15m:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [MTF過濾] 15m趨勢向上 (現價 {cp:.4f} >= 15m_SMA200 {sma200_15m:.4f})，禁止逆勢做空")
                    return False
            
    # --- 盤整/低波動過濾 (Choppiness) ---
    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)
    
    # 判斷波動太小的條件：當前 ATR 小於 24H 平均 ATR 的 60%，或 BB 區間太窄
    bb_up = s.get("bb_up", 0.0)
    bb_down = s.get("bb_down", 0.0)
    bb_width_pct = (bb_up - bb_down) / cp if cp > 0 else 0
    
    if atr_24h_avg > 0 and current_atr < atr_24h_avg * 0.25: # 原 0.6，放寬至允許 25% 的極低波動
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [波動率過濾] 當前 ATR 過小，處於極度盤整 (current={current_atr:.5f}, avg={atr_24h_avg:.5f})")
        return False
    if bb_width_pct > 0 and bb_width_pct < 0.0015: # 原 0.005，放寬至布林帶寬度 0.15%
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [波動率過濾] 布林帶極度收斂 (寬度={bb_width_pct*100:.2f}%)，避免洗盤")
        return False
    if not is_entry_pin_safe(sym, side):
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [插針過濾] 反向長影線/方向未確認")
        return False
        
    # 量能確認過濾器 (衰竭進場策略 Exhaustion_Entry 允許低量能)
    if route != "Exhaustion_Entry" and not is_entry_volume_confirmed(sym, side):
        return False
        
    # ADX 趨勢強度限制
    highs = np.array([x[2] for x in s["ohlcv"]])
    lows = np.array([x[3] for x in s["ohlcv"]])
    closes = np.array([x[4] for x in s["ohlcv"]])
    adx_val = calculate_adx(highs, lows, closes)
    if adx_val < 5: # 原 10，大幅放寬 ADX 趨勢強度門檻
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [ADX過濾] 趨勢強度 ADX {adx_val:.1f} < 8")
        return False

    # 實盤最小量限制 (移除 1000 絕對門檻，改用動態 10% 均量)
    min_volume = s["vol_ma20"] * 0.05
    if s["current_vol"] < min_volume:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [實盤最小量過濾] 當前 {s['current_vol']:.2f} < 均量 10% ({min_volume:.2f})")
        return False
    return True

def compute_signal_strength(sym):
    s = STATES[sym]
    if len(s["closes"]) < 20:
        return (None, 0)

    # --- 新增 C：動能/成交量過濾 ---
    # 確保當前 K 線成交量不要低得離譜 (放寬至 0.15 倍均量即可通過)
    vol_ma10 = s.get("vol_ma10", 0.0)
    current_vol = s.get("current_vol", 0.0)
    if vol_ma10 > 0 and current_vol < vol_ma10 * 0.15:
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

    # --- 強化防看錯方向：嚴格要求最後 2 根 K 線方向一致 (連續2根綠K/紅K) ---
    last_two_candles_long = len(s["ohlcv"]) >= 3 and \
                              s["ohlcv"][-1][4] > s["ohlcv"][-2][4] and \
                              s["ohlcv"][-2][4] > s["ohlcv"][-3][4]
    last_two_candles_short = len(s["ohlcv"]) >= 3 and \
                               s["ohlcv"][-1][4] < s["ohlcv"][-2][4] and \
                               s["ohlcv"][-2][4] < s["ohlcv"][-3][4]

    ema50 = s.get("ema50", 0.0)
    trend_confluence_long = ema50 == 0.0 or close > ema50
    trend_confluence_short = ema50 == 0.0 or close < ema50

    is_above_sma200 = s.get("sma200_15m", 0) > 0 and close > s.get("sma200_15m", 0) * 0.999
    is_below_sma200 = s.get("sma200_15m", 0) > 0 and close < s.get("sma200_15m", 0) * 1.001

    # 限制開倉不要太偏離短期趨勢線，避免追價開倉
    close_near_ema20_long = ema20 <= 0 or close <= ema20 * 1.05
    close_near_ema20_short = ema20 <= 0 or close >= ema20 * 0.95
    is_in_bb_zone_long = s.get("bb_low", 0) > 0 and close <= s["bb_low"] * 1.01
    is_in_bb_zone_short = s.get("bb_up", 0) > 0 and close >= s["bb_up"] * 0.99

    print(f"@@COIN_DEBUG@@ 🔍 {sym} 條件檢測 | RSI動能(L>50/S<50): {rsi > 50.0}/{rsi < 50.0} | SMA200長線(L/S): {is_above_sma200}/{is_below_sma200} | MACD多頭/空頭: {macd_hist > 0}/{macd_hist < 0} | 收盤價確認(L/S): {last_two_candles_long}/{last_two_candles_short} | EMA20距離(L/S): {close_near_ema20_long}/{close_near_ema20_short} | BB區(L/S): {is_in_bb_zone_long}/{is_in_bb_zone_short} | EMA50確認(L/S): {trend_confluence_long}/{trend_confluence_short}")

    # 寬鬆的RSI條件：>50時正常，或在45-55中立區但MACD確認時也允許
    rsi_ok_long = rsi > 45.0 or (rsi >= 40.0 and (long_macd_cross or macd_hist > 0))
    rsi_ok_short = rsi < 55.0 or (rsi <= 60.0 and (short_macd_cross or macd_hist < 0))

    # Route A (Trend Following): 嚴格版 - 需要 SMA200 長線確認
    
    # --- 優化：加入趨勢加分機制 ---
    # 如果方向與 EMA50 趨勢一致，加 5 分，否則扣 5 分
    trend_score = 0
    if trend_confluence_long and (long_macd_cross or macd_hist > 0):
        trend_score = 5
    elif trend_confluence_short and (short_macd_cross or macd_hist < 0):
        trend_score = 5
    elif (trend_confluence_long and (short_macd_cross or macd_hist < 0)) or \
         (trend_confluence_short and (long_macd_cross or macd_hist > 0)):
        trend_score = -5
    else:
        trend_score = 0

    route_a_long = (
        is_above_sma200 and
        (long_macd_cross or macd_hist > 0) and 
        last_two_candles_long and 
        rsi_ok_long and 
        close_near_ema20_long
    )
    
    route_a_short = (
        is_below_sma200 and
        (short_macd_cross or macd_hist < 0) and 
        last_two_candles_short and 
        rsi_ok_short and 
        close_near_ema20_short
    )

    long_base_ok = route_a_long
    short_base_ok = route_a_short

    if long_base_ok:
        route = "a"
        strength = 12.0 + ((close - ema20) / max(ema20, 1e-8) * 100)
        if long_macd_cross:
            strength += 5.0
        strength += trend_score
        return ("buy", strength if strength >= 6.0 else 0.0, route)

    if short_base_ok:
        route = "a"
        strength = 12.0 + ((ema20 - close) / max(ema20, 1e-8) * 100)
        if short_macd_cross:
            strength += 5.0
        strength += trend_score
        return ("sell", strength if strength >= 6.0 else 0.0, route)

    # --- Route C: 量能衰竭進場策略 (Exhaustion Entry) ---
    # 專門抓大趨勢回檔時的「價跌量縮」潛在底部
    if len(s["ohlcv"]) >= 50:
        c1 = s["ohlcv"][-2]  # 最新已收盤 (驗證K線)
        c2 = s["ohlcv"][-3]  # 前一根已收盤 (縮量衰竭K線)
        c2_vol_low = c2[5] < s.get("vol_ma20", 1) * 0.8
        
        # 多單：抓回檔底部
        if c2[4] < c2[1] and c2_vol_low:  # c2 價跌且量縮
            recent_low_50 = min([x[3] for x in s["ohlcv"][-50:]])
            support_ok = (c1[3] <= s.get("bb_low", 0) * 1.005) or (c1[3] <= recent_low_50 * 1.005)
            reversal_ok = (c1[4] > c1[1]) and ((min(c1[1], c1[4]) - c1[3]) > abs(c1[4] - c1[1]) * 0.5)
            bounce_ok = (c1[4] > c1[1]) and (c1[5] > c2[5] * 1.2)
            
            ema50_1h = s.get("ema50_1h", 0)
            trend_ok = (ema50_1h == 0) or (close > ema50_1h * 0.99)
            
            if trend_ok and (support_ok or reversal_ok or bounce_ok):
                print(f"🌟 [量能衰竭] {sym} 觸發多單低接條件！(Support:{support_ok}, Rev:{reversal_ok}, Bounce:{bounce_ok})")
                return ("buy", 15.0, "Exhaustion_Entry")
                
        # 空單：抓反彈頂部
        if c2[4] > c2[1] and c2_vol_low:  # c2 價漲且量縮
            recent_high_50 = max([x[2] for x in s["ohlcv"][-50:]])
            support_ok = (c1[2] >= s.get("bb_up", 0) * 0.995) or (c1[2] >= recent_high_50 * 0.995)
            reversal_ok = (c1[4] < c1[1]) and ((c1[2] - max(c1[1], c1[4])) > abs(c1[4] - c1[1]) * 0.5)
            bounce_ok = (c1[4] < c1[1]) and (c1[5] > c2[5] * 1.2)
            
            ema50_1h = s.get("ema50_1h", 0)
            trend_ok = (ema50_1h == 0) or (close < ema50_1h * 1.01)
            
            if trend_ok and (support_ok or reversal_ok or bounce_ok):
                print(f"🌟 [量能衰竭] {sym} 觸發空單高空條件！(Support:{support_ok}, Rev:{reversal_ok}, Bounce:{bounce_ok})")
                return ("sell", 15.0, "Exhaustion_Entry")

    return (None, 0, None)

async def check_entries():
    open_count = get_open_position_count()
    remaining_slots = MAX_POSITIONS - open_count

    candidates = []
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s["status"] != "ACTIVE":
            continue
            
        has_position = abs(s["qty"]) > 0.000001
        current_direction = "buy" if s["qty"] > 0 else "sell" if s["qty"] < 0 else None
        
        # 開倉數限制 (針對新開倉)
        if not has_position and open_count >= MAX_POSITIONS:
            continue

        current_candle_time = s["ohlcv"][-1][0] if s["ohlcv"] else 0

        # --- 新增：等待收盤確認機制 ---
        if s.get("pending_side"):
            if current_candle_time <= s.get("pending_time", 0):
                continue
            
            # 換線了，檢查前一根(訊號K線)是否反轉
            if len(s["ohlcv"]) >= 2:
                prev_candle = s["ohlcv"][-2]
                prev_open = prev_candle[1]
                prev_close = prev_candle[4]
                
                is_valid = False
                if s["pending_side"] == "buy":
                    # [Layer 3] 嚴格K線：實體綠K且上影線 < 實體的 50%
                    body = prev_close - prev_open
                    upper_shadow = prev_candle[2] - prev_close
                    if body > 0 and upper_shadow < body * 0.5:
                        is_valid = True
                elif s["pending_side"] == "sell":
                    # [Layer 3] 嚴格K線：實體紅K且下影線 < 實體的 50%
                    body = prev_open - prev_close
                    lower_shadow = prev_close - prev_candle[3]
                    if body > 0 and lower_shadow < body * 0.5:
                        is_valid = True
                        
                if is_valid:
                    print(f"✅ [訊號確認] {sym} {s['pending_side']} 訊號已確認 (K線收盤無反轉)")
                    side = s["pending_side"]
                    strength = s.get("pending_strength", 5.0)
                    route = s.get("pending_route", "confirmed")
                    s["pending_side"] = None
                    
                    # 再測一次大環境 (MTF & RR)，因為換線了可能改變
                    p = s["close_price"]
                    if s.get("mtf_filter", True):
                        ema50_1h = s.get("ema50_1h", 0.0)
                        if ema50_1h > 0:
                            if side == "buy" and p < ema50_1h:
                                print(f"📉 [1H 過濾] {sym} 確認階段：1H 趨勢向下，捨棄訊號")
                                continue
                            if side == "sell" and p > ema50_1h:
                                print(f"📈 [1H 過濾] {sym} 確認階段：1H 趨勢向上，捨棄訊號")
                                continue

                    atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)
                    sl_multiplier = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), side == "buy")
                    tp_multiplier = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), side == "buy")
                    
                    sl_dist = max(atr_val * sl_multiplier, p * 0.005)
                    tp_dist = max(atr_val * tp_multiplier, p * 0.015)
                    
                    expected_rr = tp_dist / sl_dist if sl_dist > 0 else 0
                    if expected_rr < 2.0:
                        print(f"⚠️ [盈虧比過濾] {sym} 預期盈虧比 {expected_rr:.2f} < 2.0，放棄")
                        continue
                        
                    # [Layer 4] 布林帶空間過濾
                    if side == "buy" and s.get("bb_up", 0) > 0:
                        space = s["bb_up"] - p
                        if space < sl_dist * 0.5:
                            print(f"⚠️ [空間過濾] {sym} 做多距布林上軌僅 {space:.4f} < 0.5*SL({sl_dist*0.5:.4f})，拒絕進場")
                            continue
                    if side == "sell" and s.get("bb_low", 0) > 0:
                        space = p - s["bb_low"]
                        if space < sl_dist * 0.5:
                            print(f"⚠️ [空間過濾] {sym} 做空距布林下軌僅 {space:.4f} < 0.5*SL({sl_dist*0.5:.4f})，拒絕進場")
                            continue
                        
                    candidates.append((sym, side, strength, route))
                    continue
                else:
                    print(f"❌ [訊號失效] {sym} {s['pending_side']} 訊號 K 線收盤反轉，取消開倉。")
                    s["pending_side"] = None
            else:
                s["pending_side"] = None
            continue

        # 原本的計算邏輯
        side_strength = compute_signal_strength(sym)
        if side_strength[0] is None:
            continue
        side, strength, route = side_strength
        
        # [Layer 1] 大盤過濾 (4H BTC Trend)
        if side == "buy" and MARKET_WIND.get("btc_trend_4h") != "BULL":
            print(f"🛑 [大盤過濾] {sym} 訊號為多，但 BTC 4H 趨勢為 {MARKET_WIND.get('btc_trend_4h')}，禁止做多！")
            continue
        if side == "sell" and MARKET_WIND.get("btc_trend_4h") != "BEAR":
            print(f"🛑 [大盤過濾] {sym} 訊號為空，但 BTC 4H 趨勢為 {MARKET_WIND.get('btc_trend_4h')}，禁止做空！")
            continue
            
        # [Layer 2] MTF 中線與大趨勢過濾
        cp = s["close_price"]
        ema50_1h = s.get("ema50_1h", 0)
        sma200_15m = s.get("sma200_15m", 0)
        
        if side == "buy":
            if ema50_1h > 0 and cp < ema50_1h:
                print(f"🛑 [MTF過濾] {sym} 多單被攔截：價格低於 1H EMA50 ({ema50_1h:.4f})")
                continue
            if sma200_15m > 0 and cp < sma200_15m:
                print(f"🛑 [MTF過濾] {sym} 多單被攔截：價格低於 15m SMA200 ({sma200_15m:.4f})")
                continue
        else: # sell
            if ema50_1h > 0 and cp > ema50_1h:
                print(f"🛑 [MTF過濾] {sym} 空單被攔截：價格高於 1H EMA50 ({ema50_1h:.4f})")
                continue
            if sma200_15m > 0 and cp > sma200_15m:
                print(f"🛑 [MTF過濾] {sym} 空單被攔截：價格高於 15m SMA200 ({sma200_15m:.4f})")
                continue
        
        # --- 方向鎖定 (Direction Lock) ---
        if has_position and side != current_direction:
            # 已經有持倉，不允許反向訊號加倉
            continue

        if not is_entry_allowed(sym, side, route):
            continue

        # --- 反手冷卻時間 (min_flip_time) 過濾 ---
        last_trade_side = s.get("last_trade_side", "")
        if last_trade_side != "" and side != last_trade_side:
            flip_elapsed = time.time() - s.get("last_trade_time", 0)
            # 動態冷卻：如果上次是停損出場，代表趨勢已逆轉，允許更快的反手 (縮短為 60 秒)
            last_exit = s.get("last_exit_reason", "")
            is_stop_loss = "Stop" in last_exit or "Loss" in last_exit or "Trailing" in last_exit or "Momentum_Fade" in last_exit
            min_flip = 60 if is_stop_loss else s.get("min_flip_time", 900)
            
            if flip_elapsed < min_flip:
                print(f"⏳ [方向鎖定] {sym} 欲 {side}，但距離上次做 {last_trade_side} 僅 {flip_elapsed:.0f}s (冷卻需 {min_flip}s, 原因:{last_exit})，禁止頻繁反手。")
                continue

        # --- 1H 多重時間週期 (Multi-Timeframe) 過濾 ---
        p = s["close_price"]
        if s.get("mtf_filter", True):
            ema50_1h = s.get("ema50_1h", 0.0)
            if ema50_1h > 0:
                if side == "buy" and p < ema50_1h:
                    print(f"📉 [1H 過濾] {sym} 1H 趨勢向下 (現價 {p:.4f} < EMA50 {ema50_1h:.4f})，忽略買入訊號")
                    continue
                if side == "sell" and p > ema50_1h:
                    print(f"📈 [1H 過濾] {sym} 1H 趨勢向上 (現價 {p:.4f} > EMA50 {ema50_1h:.4f})，忽略賣出訊號")
                    continue

        # --- R:R 盈虧比過濾 (Risk:Reward Filter) ---
        atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)
        sl_multiplier = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), side == "buy")
        tp_multiplier = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), side == "buy")
        
        sl_dist = max(atr_val * sl_multiplier, p * 0.005)
        tp_dist = max(atr_val * tp_multiplier, p * 0.015)
        
        expected_rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if expected_rr < 1.5:
            print(f"⚠️ [盈虧比過濾] {sym} 預期盈虧比 {expected_rr:.2f} < 1.5，放棄暫存")
            continue

        # 通過初步過濾，進入 pending 狀態等待下一根 K 線確認
        s["pending_side"] = side
        s["pending_time"] = current_candle_time
        s["pending_strength"] = strength
        s["pending_route"] = route
        
        # --- 新增：Minimum Flip Buffer (防止快速反手) ---
        # 如果當前訊號與上次開倉方向相反，檢查是否超過 300 秒 (5分鐘)
        if side == 'buy' and s.get("last_flip_time", 0) > 0:
            # 檢查上次是否是做空 (qty < 0)
            # 這裡我們用 last_flip_time 記錄最後一次開倉的時間
            # 如果我們想更精確，可以記錄 last_flip_side
            pass # 這裡先實作基礎時間檢查，稍後優化
        
        # 為了精確，我們需要知道上次是哪一邊。
        # 由於我們在 execute_order 裡更新了 last_flip_time，
        # 我們可以檢查當前訊號與上次開倉的狀態。
        # 但最簡單的防禦是：如果剛才才平倉/開倉，就不要馬上反手。
        if current_candle_time - s.get("last_flip_time", 0) < 300:
            print(f"⏳ [Flip Buffer] {sym} 訊號 {side} 被攔截 (距離上次翻轉僅 {current_candle_time - s.get('last_flip_time', 0):.0f}s)")
            continue

        print(f"⏳ [等待確認] {sym} 產生 {side} 訊號 ({route})，等待目前 K 線收盤確認...")

    if not candidates:
        return

    candidates.sort(key=lambda x: -x[2])
    print(f"📊 [訊號排行] {' | '.join(f'{sym}:{side}({strength:.2f})' for sym, side, strength, _ in candidates[:3])}")

    for sym, side, strength, route in candidates:
        s = STATES[sym]
        has_pos = abs(s["qty"]) > 0.000001
        
        if not has_pos:
            if remaining_slots <= 0:
                continue
            remaining_slots -= 1
            print(f"⚡ [即時開倉] {sym} 觸發訊號 ({route} 路線)，即刻首倉進場！")
        else:
            print(f"⚡ [順勢加倉] {sym} 觸發加倉訊號 ({route} 路線)，準備執行加碼！")
            
        await execute_order(sym, side, s["close_price"])
        s["pending_side"] = None
        s["pending_confirm_high"] = 0
        s["pending_confirm_low"] = 0

# ── 主循環 ──────────────────────────────────────────────────

async def watch_symbol_trades(exchange, sym):
    while True:
        try:
            trades = await exchange_futures.fetch_trades(sym, limit=50)
            if isinstance(trades, list):
                for trade in trades:
                    update_trade_signal(sym, trade)
            elif trades:
                update_trade_signal(sym, trades)
        except Exception as e:
            print(f"⚠️ [成交流監聽異常] {sym}: {e}")
        await asyncio.sleep(3)
    global WATCH_TASKS
    desired_symbols = set(ALL_SYMBOLS)
    current_symbols = set(WATCH_TASKS.keys())

    for sym in current_symbols - desired_symbols:
        task = WATCH_TASKS.pop(sym, None)
        if task is not None:
            task.cancel()

    for sym in desired_symbols - current_symbols:
        WATCH_TASKS[sym] = asyncio.create_task(watch_symbol_trades(sym, exchange))


async def ensure_watch_tasks(exchange):
    """No-op stub kept for compatibility."""
    pass


async def market_wind_loop(exchange):
    while True:
        try:
            await update_market_wind(exchange)
        except Exception as e:
            print(f"⚠️ [大盤風向更新失敗] {e}")
        await asyncio.sleep(60)

async def main_loop(exchange):
    asyncio.create_task(market_wind_loop(exchange))
    global ALL_SYMBOLS
    """初始化後進入主交易循環"""



    try:
        await asyncio.wait_for(exchange_futures.load_markets(), timeout=15)
    except Exception as e:
        print(f"⚠️ load_markets 失敗 ({e})，使用預設市場清單")

    global ALL_SYMBOLS
    ALL_SYMBOLS = filter_valid_symbols(exchange, ALL_SYMBOLS)
    save_symbol_pool(ALL_SYMBOLS)

    print(f"📋 監控幣種: {', '.join(ALL_SYMBOLS)}")
    try:
        await asyncio.wait_for(initialize_atr_history(exchange), timeout=60)
    except (asyncio.TimeoutError, Exception) as e:
        print(f"⏳ [初始化] ATR 歷史預熱超時或失敗 ({e})，將在運行中慢慢加熱")
    await fetch_real_balance()
    await load_open_positions()
    await fetch_all_sma200(exchange)
    await fetch_all_ema50_1h(exchange)

    last_balance_update = time.time()

    while True:
        try:
            loop_start = time.time()
            if not PAPER_TRADING and loop_start - last_balance_update > 30:
                await fetch_real_balance()
                last_balance_update = loop_start

            open_syms = [sym for sym in ALL_SYMBOLS if abs(STATES[sym]["qty"]) > 0.000001]
            closed_syms = [sym for sym in ALL_SYMBOLS if abs(STATES[sym]["qty"]) <= 0.000001]
            # 將有持倉的幣種排在最後面，確保日誌輸出時位於最底端（最容易看到）
            ALL_SYMBOLS = closed_syms + open_syms

            for sym in ALL_SYMBOLS:
                STATES[sym]["adjusted_this_tick"] = False
            # await update_market_wind(exchange)  # 已移至獨立 Task
            await fetch_all_klines(exchange)
            for sym in ALL_SYMBOLS:
                try:
                    compute_indicators(sym)
                except Exception as e:
                    print(f"⚠️ [指標計算異常] {sym} 處理失敗，跳過此幣種本次更新: {e}")
            
            # --- 狀態更新區塊 ---
            try:
                update_states()
                update_all_dynamic_personalities()
            except Exception as e:
                print(f"⚠️ [狀態更新異常]: {e}")

            # --- AI 大腦診斷 ---
            if time.time() % 1800 < 6: # 每 30 分鐘執行一次
                asyncio.create_task(ai_engine.run_ai_diagnosis_cycle())

            # --- 出場檢查區塊 (最關鍵的防禦) ---
            for sym in ALL_SYMBOLS:
                try:
                    await check_exits(sym)
                except Exception as e:
                    # 如果某個幣種的 check_exits 崩潰，只會報錯並跳過，不會影響到其他幣種
                    print(f"⚠️ [出場檢查異常] {sym} 出場邏輯報錯，跳過此幣種檢查: {e}")

            # --- 進場檢查區塊 ---
            try:
                await check_entries()
            except Exception as e:
                print(f"⚠️ [進場檢查異常]: {e}")

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
            error_msg = f"發生未預期的錯誤：\n{str(e)}\n{traceback.format_exc()}"
            print(f"❌ [系統錯誤] {error_msg}")
            
            # 觸發通知 (如果有定義 send_alert 的話)
            try:
                send_alert(error_msg)
            except NameError:
                pass
            
            CONSECUTIVE_ERRORS += 1
            if CONSECUTIVE_ERRORS >= 3:
                try:
                    send_alert("⚠️ [嚴重警告] 機器人連續報錯 3 次以上，請立即檢查系統狀態！")
                except NameError:
                    pass
                cooldown = min(120, 15 * (CONSECUTIVE_ERRORS - 2))
                print(f"🚨 [連續API錯誤風控] 已連續錯誤 {CONSECUTIVE_ERRORS} 次，觸發風控冷卻，暫停 {cooldown} 秒...")
                await asyncio.sleep(cooldown)
            else:
                await asyncio.sleep(5)

async def periodic_htf_update(exchange):
    while True:
        await asyncio.sleep(900)
        await fetch_all_sma200(exchange)
        await fetch_all_ema50_1h(exchange)
        print("🔄 [HTF] 已更新所有幣種 15m SMA200 與 1H EMA50")

async def periodic_status_log():
    while True:
        await asyncio.sleep(60)
        active = sum(1 for s in STATES.values() if s["status"] == "ACTIVE")
        cooldown = sum(1 for s in STATES.values() if s["status"] == "COOLDOWN")
        banned = sum(1 for s in STATES.values() if s["status"] == "BANNED")
        open_syms = get_open_symbols()
        open_str = ', '.join(f"{sym}({'多' if STATES[sym]['qty']>0 else '空'})" for sym in open_syms) if open_syms else "無"
        print(f"📊 [狀態] 監控池={active} 冷卻={cooldown} 禁賽={banned} | 當前持倉({len(open_syms)}/{MAX_POSITIONS}): {open_str}")

async def main():
    asyncio.create_task(periodic_htf_update(exchange_futures))
    asyncio.create_task(periodic_status_log())
    
    while True:
        try:
            await main_loop(exchange_futures)
        except Exception as e:
            import traceback
            print(f"🚨 [致命錯誤] main_loop 崩潰: {e}")
            traceback.print_exc()
            print("⏳ 將在 10 秒後由內部自動重啟主程序...")
            await asyncio.sleep(10)
        finally:
            # 如果因為某種原因跳出，確保資源有被釋放或嘗試重新連接
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 程式已被手動終止")
    finally:
        # 在退出前關閉交易所連接
        async def cleanup():
            await exchange_futures.close()
            await exchange_spot.close()
        asyncio.run(cleanup())
