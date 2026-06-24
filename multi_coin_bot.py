import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from ai_manager import ai_engine
import ccxt
import ccxt.pro as ccxtpro
import numpy as np
import json
import signal
import time
import uuid
import fcntl
import math
import requests
import traceback
import inspect
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
TIMEFRAME = '5m'
TRADE_HISTORY_FILE = "trade_history.json"
MAX_GLOBAL_CONCURRENT_TRADES = 2
DEFAULT_LEVERAGE = 5

# 限價單監控表 (Pending Limit Orders)
# 格式: { order_id: { "sym", "side", "qty", "price", "timestamp" } }
PENDING_LIMIT_ORDERS = {}

COIN_PROFILE_CONFIG = {
    # --- 第一類：核心趨勢層 (Core Trend) - 穩健趨勢，較高槓桿 ---
    "SOLUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 15.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "rr_threshold": 1.3},
    "LINKUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.4, "min_flip_time": 1800, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "rr_threshold": 1.3},
    "TIAUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "rr_threshold": 1.3},

    # --- 第二類：高彈性動能層 (High-Beta Momentum) - 快速爆發，中等槓桿 ---
    "RENDERUSDT": {"sl_atr_multiplier": 3.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.4, "breakeven_trigger": 0.35, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 1.3},
    "SUIUSDT":   {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 20.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.7, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 1.3},
    "INJUSDT":   {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 20.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.6, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 1.3},
    "NEARUSDT":  {"sl_atr_multiplier": 3.5, "tp_atr_multiplier": 16.0, "volume_threshold_factor": 1.4, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 1.3},
    "FETUSDT":   {"sl_atr_multiplier": 3.5, "tp_atr_multiplier": 16.0, "volume_threshold_factor": 1.4, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 1.3},
    "TAOUSDT":   {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 20.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.6, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 1.3},
    "SEIUSDT":   {"sl_atr_multiplier": 3.5, "tp_atr_multiplier": 16.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 1.3},

    # --- 第三類：投機與特定風險層 (Speculative_Risk) - 極端防禦，低槓桿 ---
    "AVAXUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "rr_threshold": 1.3},
    "DOGEUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 20.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.8, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "Speculative_Risk", "leverage": 2, "rr_threshold": 1.3}

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
    "SOLUSDT", "LINKUSDT", "TRXUSDT", "TIAUSDT", "RENDERUSDT",
    "SUIUSDT", "INJUSDT", "NEARUSDT", "VELVETUSDT", "LABUSDT",
    "AVAXUSDT", "DOGEUSDT"
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
    aggressive_coins = {"DOGEUSDT", "SHIBUSDT"}
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


def get_dynamic_atr_multiplier(sym, base_multiplier):
    """
    計算當前 ATR 與 24 小時平均 ATR 的比值，並動態調整乘數。
    當波動激增（大於平均 1.5 倍）時，等比例放大乘數以給予價格防插針空間；
    當市場死水（小於平均 0.7 倍）時，縮小乘數以節省潛在停損距離。
    """
    s = STATES.get(sym)
    if not s:
        return base_multiplier
        
    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)
    
    if atr_24h_avg > 0:
        vol_ratio = current_atr / atr_24h_avg
        if vol_ratio > 1.5:
            # 波動激增，放大乘數 (最高 1.5 倍)
            return base_multiplier * min(vol_ratio, 1.5)
        elif vol_ratio < 0.7:
            # 波動死水，縮小乘數 (固定 0.8 倍)
            return base_multiplier * 0.8
            
    return base_multiplier


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
        "profile_type", "leverage", "mtf_filter",
        "rsi_extreme_low", "rsi_extreme_high", "rsi_recovery_hook", "volatility_cap",
        "min_rr", "min_profit_pct",
        "trailing_activation_atr", "trailing_distance_atr", "profit_lock_atr"
    ]:
        if key in profile:
            state[key] = profile[key]
    state["personality"] = personality
    state["personality_source"] = personality_source
    if personality_source == "manual":
        state["last_personality_update"] = time.time()


def apply_all_symbol_profiles():
    default_profile = {
        "sl_atr_multiplier": 1.5,
        "tp_atr_multiplier": 3.0,
        "min_rr": 1.2,
        "min_profit_pct": 0.001,
        "trailing_activation_atr": 1.0,
        "trailing_distance_atr": 0.8,
        "profit_lock_atr": 2.0
    }
    for sym in ALL_SYMBOLS:
        json_profile = SYMBOL_PROFILES.get(sym, {})
        if not json_profile:
            json_profile = default_profile.copy()
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

MAX_POSITIONS = 2
COOLDOWN_SEC = 1800
MAIN_LOOP_INTERVAL_SEC = 6
PENDING_CONFIRM_SEC = 2
BAN_WINDOW = 3600
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 3
SL_ATR_MULTIPLIER = 2.5
TP_ATR_MULTIPLIER = 3.0
HARD_STOP_LOSS_PCT = 0.04  # 放寬至 2.5%（原 1.5%），避免高波動行情被插針直接砍倉

def build_symbol_state(sym):
    conf = COIN_PROFILE_CONFIG.get(sym, {})
    return {
        "status": "ACTIVE",
        "error_strikes": 0,
        "is_banned": False,
        "sync_required": False,
        "last_exit_time": 0,
        "status_reason": "",
        "next_status_time": 0,
        "stop_count": 0,
        "first_stop_time": 0,
        "qty": 0.0,
        "avg_price": 0.0,
        "trailing_stop_price": 0.0,
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
        "volume_threshold_factor": conf.get("volume_threshold_factor", 1.4),
        "volume_multiplier": conf.get("volume_multiplier", 1.0),
        "sl_atr_multiplier": conf.get("sl_atr_multiplier", 1.5),
        "tp_atr_multiplier": conf.get("tp_atr_multiplier", 2.5),
        "hard_stop_loss_pct": HARD_STOP_LOSS_PCT,
        "personality": "balanced",
        "personality_source": "infer",
        "last_personality_update": 0.0,
        "last_entry_time": 0.0,
        "is_ordering": False,
        "last_action_time": 0.0,
        "rsi_extreme_low": conf.get("rsi_extreme_low", 20),
        "rsi_extreme_high": conf.get("rsi_extreme_high", 75),
        "rsi_recovery_hook": conf.get("rsi_recovery_hook", 30),
        "volatility_cap": conf.get("volatility_cap", 3.0),
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
    if current_atr < atr_ma20 * 0.5:
        return 180
    elif current_atr < atr_ma20:
        return 300
    return 480

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

def compute_per_coin_margin(sym=None, allocation_pct=0.33):
    balance = get_balance()
    if balance <= 0 or not sym:
        return 0

    # 動態資金分配 (預設 33%)
    return balance * allocation_pct * 0.95

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
    s["entries"] = []
    s["open_time"] = 0.0
    s["trailing_highest"] = 0.0
    s["trailing_lowest"] = float('inf')
    s["highest_profit_pct"] = 0.0
    s["has_partial_closed"] = False
    s["is_breakeven_locked"] = False
    s["stop_loss"] = 0.0
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
        btc_ohlcv_1h = await exchange.fetch_ohlcv("BTC/USDT", '1h', limit=50)
        btc_ohlcv_4h = await exchange.fetch_ohlcv("BTC/USDT", '4h', limit=50)
        
        MARKET_WIND["allow_long"] = True
        MARKET_WIND["allow_short"] = True
        
        if len(btc_ohlcv_1h) >= 20:
            btc_closes_1h = [x[4] for x in btc_ohlcv_1h]
            # Simple EMA20 for 1H
            alpha = 2 / 21
            ema = btc_closes_1h[0]
            for val in btc_closes_1h[1:]: ema = alpha * val + (1 - alpha) * ema
            btc_price_1h = btc_closes_1h[-1]
            MARKET_WIND["btc_trend_1h"] = "BULL" if btc_price_1h > ema else "BEAR"
        else:
            MARKET_WIND["btc_trend_1h"] = "NEUTRAL"

        if len(btc_ohlcv_4h) >= 20:
            btc_closes_4h = [x[4] for x in btc_ohlcv_4h]
            # Simple EMA20 for 4H
            alpha_4h = 2 / 21
            ema_4h = btc_closes_4h[0]
            for val in btc_closes_4h[1:]: ema_4h = alpha_4h * val + (1 - alpha_4h) * ema_4h
            btc_price_4h = btc_closes_4h[-1]
            MARKET_WIND["btc_trend_4h"] = "BULL" if btc_price_4h > ema_4h else "BEAR"
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
            
        # 1. 瀑布防護 (極端風暴：2.5% 震幅，避免 ETH 正常回調誤觸)
        if btc_change_15m < -0.025 or eth_change_15m < -0.025:
            MARKET_WIND["allow_long"] = False
            print(f"⚠️ [大盤瀑布風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣多單開倉！")
        elif btc_change_15m > 0.025 or eth_change_15m > 0.025:
            MARKET_WIND["allow_short"] = False
            print(f"⚠️ [大盤暴漲風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣空單開倉！")
            
    except Exception as e:
        print(f"⚠️ [更新大盤風向失敗]: {e}")

# ── 資料獲取 ──────────────────────────────────────────────────

async def initialize_atr_history(exchange, batch_size: int = ATR_WARMUP_BATCH_SIZE, limit: int = ATR_WARMUP_LIMIT, pause_sec: float = ATR_WARMUP_PAUSE_SEC):
    target_symbols = ALL_SYMBOLS[:ATR_WARMUP_SYMBOL_COUNT]
    
    # --- 讀取本地 ATR 快取 ---
    import os, json
    loaded_symbols = set()
    try:
        if os.path.exists("atr_history_cache.json"):
            with open("atr_history_cache.json", "r") as f:
                cache_data = json.load(f)
            for sym in cache_data:
                if sym in STATES and sym in target_symbols:
                    STATES[sym]["atr_history"] = cache_data[sym]
                    loaded_symbols.add(sym)
            if loaded_symbols:
                print(f"💾 [快取] 成功從本地載入 {len(loaded_symbols)} 個幣種的 ATR 歷史資料！")
    except Exception as e:
        print(f"⚠️ [快取] 讀取失敗: {e}")

    # 只針對沒有快取的幣種進行網路請求
    target_symbols = [sym for sym in target_symbols if sym not in loaded_symbols]
    if not target_symbols:
        print("✅ [初始化] 所有幣種皆已從快取載入，跳過網路預熱！")
        return

    print(f"⏳ [初始化] 尚有 {len(target_symbols)} 個幣種需要網路獲取，開始分批獲取 {limit} 根 {TIMEFRAME} K線...")
    total = len(target_symbols)

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

async def fetch_ema_15m(exchange, sym):
    try:
        async with request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '15m', limit=100)
        if not ohlcv or len(ohlcv) == 0:
            return 0.0, 0.0
        closes = np.array([x[4] for x in ohlcv])
        ema20 = calculate_ema(closes, 20)
        ema50 = calculate_ema(closes, 50)
        return float(ema20), float(ema50)
    except Exception as e:
        print(f"⚠️ [15m EMA獲取失敗] {sym}: {e}")
        return 0.0, 0.0

async def fetch_all_ema_15m(exchange):
    tasks = [fetch_ema_15m(exchange, sym) for sym in ALL_SYMBOLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            ema20, ema50 = results[i]
            STATES[sym]["ema20_15m"] = ema20
            STATES[sym]["ema50_15m"] = ema50

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


async def fetch_bb_4h(exchange, sym):
    try:
        async with request_semaphore:
            ohlcv = await exchange.fetch_ohlcv(sym, '4h', limit=50)
        if not ohlcv or len(ohlcv) == 0:
            return None, None
        closes = np.array([x[4] for x in ohlcv])
        mbb, upper, lower = calculate_bollinger_bands(closes, 20, 2)
        return float(upper[-1]), float(lower[-1])
    except Exception as e:
        print(f"⚠️ [4H BB獲取失敗] {sym}: {e}")
        return None, None

async def fetch_all_bb_4h(exchange):
    tasks = [fetch_bb_4h(exchange, sym) for sym in ALL_SYMBOLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            upper, lower = results[i]
            if upper is not None and lower is not None:
                STATES[sym]["bb_upper_4h"] = upper
                STATES[sym]["bb_lower_4h"] = lower

async def load_open_positions():
    if not PAPER_TRADING:
        return
    try:
        with open("paper_state.json", "r") as f:
            state = json.load(f)
            
        current_time = time.time()
        
        # 遍歷所有紀錄的倉位，如果不在此次監控清單但有持倉，自動將其加回監控清單
        positions_dict = state.get("positions", {})
        for pk, pos in positions_dict.items():
            qty = float(pos.get("qty", 0.0))
            if abs(qty) > 0.000001:
                # 把 paper_key "BTC:USDT" 轉回 "BTCUSDT"
                sym = pk.replace(":", "")
                if sym not in ALL_SYMBOLS:
                    print(f"⚠️ [發現未監控持倉] {sym} 仍有未平倉位，自動加回監控清單並在介面顯示！")
                    ALL_SYMBOLS.append(sym)
                    STATES[sym] = build_symbol_state(sym)
                    apply_symbol_profile(sym, SYMBOL_PROFILES.get(sym, {}))
                
                STATES[sym]["qty"] = qty
                STATES[sym]["avg_price"] = float(pos.get("avg_price", 0.0))
                STATES[sym]["entries"] = pos.get("entries", [])

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

def detect_divergence(sym):
    s = STATES.get(sym)
    if not s or "rsi_history" not in s or len(s["rsi_history"]) < 3 or len(s.get("ohlcv", [])) < 3:
        return None
        
    closes = [x[4] for x in s["ohlcv"][-3:]]
    rsis = s["rsi_history"][-3:]
    
    # 價格創新低，但 RSI 沒創新低 (底背離)
    if closes[2] < closes[0] and rsis[2] > rsis[0]:
        return f"{sym} 出現底背離！價格破底 ({closes[0]:.4f}->{closes[2]:.4f}) 但 RSI 墊高 ({rsis[0]:.1f}->{rsis[2]:.1f})"
    return None

def check_all_divergence_logic():
    """自動掃描所有幣種的底背離訊號"""
    divergence_results = []
    for sym in ALL_SYMBOLS:
        res = detect_divergence(sym)
        if res:
            divergence_results.append(res)
    return divergence_results

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
        if "rsi_history" not in s:
            s["rsi_history"] = []
        s["rsi_history"].append(s["current_rsi"])
        if len(s["rsi_history"]) > 10:
            s["rsi_history"].pop(0)
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
    if len(closes) >= 15:
        s["adx"] = calculate_adx(highs, lows, closes, 14)
    if len(closes) >= 20:
        up, mid, low = calculate_bollinger_bands(closes)
        s["bb_up"] = up
        s["bb_mid"] = mid
        s["bb_low"] = low

    # --- Divergence Detection ---
    s["divergence"] = "none"
    if len(closes) >= 15 and len(s.get("rsi_history", [])) >= 10:
        window_closes = closes[-15:]
        window_rsi = s["rsi_history"][-10:]
        
        c_min = np.min(window_closes)
        c_max = np.max(window_closes)
        r_min = np.min(window_rsi)
        r_max = np.max(window_rsi)
        
        curr_c = closes[-1]
        curr_r = s["current_rsi"]
        prev_r = s["rsi_history"][-2] if len(s["rsi_history"]) >= 2 else curr_r
        
        if curr_c <= c_min and curr_r > r_min and curr_r > prev_r:
            s["divergence"] = "bullish"
        elif curr_c >= c_max and curr_r < r_max and curr_r < prev_r:
            s["divergence"] = "bearish"

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

    # 安全性：限制 ATR 極端跳動影響強平價防禦
    atr_history = s.get("atr_history", [])
    atr_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else atr_val
    safe_atr = min(atr_val, atr_avg * 3) if atr_avg > 0 else atr_val

    # --- 3-Stage Trailing Logic ---
    trailing_activation_atr = s.get("trailing_activation_atr", 0.0)
    trailing_distance_atr = s.get("trailing_distance_atr", s.get("trailing_stop_multiplier", 2.0))
    profit_lock_atr = s.get("profit_lock_atr", 0.0)
    
    avg_price = s["avg_price"]
    leverage = s.get("leverage", 8)
    mm_ratio = 0.004
    if is_long:
        liq_price = avg_price * (1 - 1.0 / leverage) / (1 - mm_ratio) if leverage > 0 else 0.0
    else:
        liq_price = avg_price * (1 + 1.0 / leverage) / (1 + mm_ratio) if leverage > 0 else 0.0

    profit_pct = (current_price - avg_price) / avg_price if is_long else (avg_price - current_price) / avg_price
    s["highest_profit_pct"] = max(s.get("highest_profit_pct", 0.0), profit_pct)
    
    profit_atr_multiple = (current_price - avg_price) / atr_val if is_long else (avg_price - current_price) / atr_val

    if is_long:
        if current_price > s.get("trailing_highest", 0.0):
            s["trailing_highest"] = current_price
            
        trail_sl = s["trailing_stop_price"] # default to current SL
        
        # Stage 3: Profit Lock
        if profit_lock_atr > 0 and profit_atr_multiple >= profit_lock_atr:
            locked_sl = avg_price * 1.001
            trail_sl = max(trail_sl, locked_sl)
        # Stage 2: Trailing Mode
        elif trailing_activation_atr > 0 and profit_atr_multiple >= trailing_activation_atr:
            dynamic_sl = s["trailing_highest"] - (atr_val * trailing_distance_atr)
            trail_sl = max(trail_sl, dynamic_sl)
        # Fallback to standard logic if no custom Swing settings
        elif trailing_activation_atr == 0:
            trailing_multiplier = s.get("trailing_stop_multiplier", 2.0)
            if s["highest_profit_pct"] > 0.03:
                trailing_multiplier = 3.0
            elif s["highest_profit_pct"] < 0.01:
                trailing_multiplier = 1.5
            dynamic_sl = s["trailing_highest"] - (atr_val * trailing_multiplier)
            
            # Legacy Breakeven
            trigger_mult = s.get("breakeven_trigger", s.get("sl_atr_multiplier", 1.5))
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = avg_price + sl_dist_atr
            if current_price >= breakeven_trigger:
                dynamic_sl = max(dynamic_sl, avg_price)
                
            trail_sl = max(trail_sl, dynamic_sl)

        safe_min_sl = liq_price * 1.2
        new_sl = max(trail_sl, safe_min_sl)
        
        if new_sl > s["trailing_stop_price"]:
            s["trailing_stop_price"] = new_sl
            print(f"🛡️ [Trailing_SL] {sym} 移動止損上移至 {new_sl:.4f} (獲利倍數: {profit_atr_multiple:.1f}x ATR)")

    else:
        if current_price < s.get("trailing_lowest", float('inf')):
            s["trailing_lowest"] = current_price
            
        trail_sl = s["trailing_stop_price"]
        if trail_sl == 0.0:
            trail_sl = float('inf')
            
        # Stage 3: Profit Lock
        if profit_lock_atr > 0 and profit_atr_multiple >= profit_lock_atr:
            locked_sl = avg_price * 0.999
            trail_sl = min(trail_sl, locked_sl)
        # Stage 2: Trailing Mode
        elif trailing_activation_atr > 0 and profit_atr_multiple >= trailing_activation_atr:
            dynamic_sl = s["trailing_lowest"] + (atr_val * trailing_distance_atr)
            trail_sl = min(trail_sl, dynamic_sl)
        # Fallback to standard logic if no custom Swing settings
        elif trailing_activation_atr == 0:
            trailing_multiplier = s.get("trailing_stop_multiplier", 2.0)
            if s["highest_profit_pct"] > 0.03:
                trailing_multiplier = 3.0
            elif s["highest_profit_pct"] < 0.01:
                trailing_multiplier = 1.5
            dynamic_sl = s["trailing_lowest"] + (atr_val * trailing_multiplier)
            
            trigger_mult = s.get("breakeven_trigger", s.get("sl_atr_multiplier", 1.5))
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = avg_price - sl_dist_atr
            if current_price <= breakeven_trigger:
                dynamic_sl = min(dynamic_sl, avg_price)
                
            trail_sl = min(trail_sl, dynamic_sl)

        safe_max_sl = liq_price * 0.8
        new_sl = min(trail_sl, safe_max_sl)
        
        if s["trailing_stop_price"] == 0.0 or new_sl < s["trailing_stop_price"]:
            s["trailing_stop_price"] = new_sl
            print(f"🛡️ [Trailing_SL] {sym} 移動止損下移至 {new_sl:.4f} (獲利倍數: {profit_atr_multiple:.1f}x ATR)")
            
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
        
    # 紀錄虧損時間 (用於同向虧損冷卻期)
    if profit_pct < -0.002:
        # close_side 是 'sell' 代表原先是多單 (Long)
        if close_side == "sell":
            s["last_loss_time_long"] = time.time()
        else:
            s["last_loss_time_short"] = time.time()
        
    full_reason = f"{pnl_tag} {reason}".strip()
    s["last_exit_time"] = time.time()

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
        
        # FIFO Entry removal
        qty_to_remove = qty
        if "entries" in s:
            while qty_to_remove > 0.000001 and len(s["entries"]) > 0:
                first_entry = s["entries"][0]
                if first_entry["qty"] <= qty_to_remove + 0.000001:
                    qty_to_remove -= first_entry["qty"]
                    s["entries"].pop(0)
                else:
                    first_entry["qty"] -= qty_to_remove
                    qty_to_remove = 0

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


async def execute_panic_sell_all_positions():
    print("🚨🚨 [緊急清倉] 開始強制市價平掉所有倉位！")
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if abs(s["qty"]) > 0.000001:
            is_long = s["qty"] > 0
            cs = 'sell' if is_long else 'buy'
            p = s.get("close_price", s["avg_price"])
            print(f"🚨 [緊急清倉] 正在平倉 {sym}...")
            try:
                await close_position(sym, cs, abs(s["qty"]), p, s["avg_price"], reason="[GLOBAL_MELTDOWN]", is_stop_loss=True)
            except Exception as e:
                print(f"⚠️ [緊急清倉失敗] {sym}: {e}")

def get_total_wallet_balance():
    if PAPER_TRADING:
        # Paper trading assumption: basic capital + sum of all PnL
        try:
            with open(PAPER_STATE_FILE, 'r') as f:
                st = json.load(f)
                realized = sum(v.get('realized_pnl', 0.0) for v in st.get('positions', {}).values())
                return 1500.0 + realized # Assuming 1500 base paper capital
        except:
            return 1500.0
    else:
        # Live balance is not tracked locally as a single float reliably in states, 
        # but we can fallback to a fixed estimation or fetch it.
        # Assuming we have REAL_WALLET_BALANCE if we fetched it, or we use a fixed 1500
        return 1500.0 # Modify as per actual logic if available

def check_total_equity_protection():
    total_unrealized_pnl = 0.0
    has_positions = False
    
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        qty = s.get("qty", 0.0)
        if abs(qty) > 0.000001:
            has_positions = True
            p = s.get("close_price", s.get("avg_price", 0.0))
            avg = s.get("avg_price", 0.0)
            if qty > 0:
                pnl = (p - avg) * abs(qty)
            else:
                pnl = (avg - p) * abs(qty)
            total_unrealized_pnl += pnl

    if not has_positions:
        return True

    total_balance = get_total_wallet_balance()
    if total_balance <= 0:
        return True
        
    loss_percentage = (total_unrealized_pnl / total_balance) * 100
    GLOBAL_LOSS_THRESHOLD = -4.0 

    if loss_percentage <= GLOBAL_LOSS_THRESHOLD:
        print(f"\n🚨🚨🚨 [全局風控熔斷] 警告！當前總未實現虧損已達 {loss_percentage:.2f}%")
        print(f"🛑 超過安全防線 {GLOBAL_LOSS_THRESHOLD}%！觸發系統緊急黑天鵝熔斷機制...")
        return False
    return True

async def check_exits(sym):
    s = STATES[sym]
    if s.get("adjusted_this_tick", False):
        return
    if abs(s["qty"]) < 0.000001 or s["avg_price"] <= 0:
        return
        
    # 新增防禦：確保 ATR 已初始化且非 0
    if s.get("current_atr", 0.0) <= 0:
        # print(f"⚠️ [跳過檢查] {sym} ATR 尚未預熱完成")
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

    # --- A. 自動反手偵測邏輯 (Global Reverse Engine) ---
    bb_upper = s.get('bb_up', 0)
    bb_lower = s.get('bb_low', 0)
    vol_ma20 = s.get('vol_ma20', 0)
    current_vol = s.get('current_vol', 0)

    # Debug 模式輸出 (每分鐘)
    if not s.get("debug_start_time"):
        s["debug_start_time"] = time.time()
        
    if time.time() - s["debug_start_time"] < 600:
        if time.time() - s.get('last_debug_pressure_time', 0) > 60:
            print(f"🔍 [DEBUG_PRESSURE] {sym}: Upper={bb_upper:.4f}, Lower={bb_lower:.4f}, Vol_MA={vol_ma20:.2f}")
            s['last_debug_pressure_time'] = time.time()

    is_breakout_up = (not is_long and bb_upper > 0 and p > bb_upper and current_vol > (vol_ma20 * 1.1))
    is_breakout_down = (is_long and bb_lower > 0 and p < bb_lower and current_vol > (vol_ma20 * 1.1))

    if is_breakout_up or is_breakout_down:
        last_reverse = s.get('last_reverse_time', 0)
        if time.time() - last_reverse > 1800:
            print(f"⚠️ [INITIATING_REVERSE] 偵測到 {sym} 強力突破！準備執行反手...")
            new_direction = "buy" if is_breakout_up else "sell"
            
            # 1. 平倉現有倉位
            cs = "sell" if is_long else "buy"
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[AUTOMATIC_REVERSE]")
            
            # 2. 安全性強制校準
            await asyncio.sleep(1)
            
            # 3. 反手開倉
            s['last_reverse_time'] = time.time()
            await execute_order(sym, new_direction, p)
            print(f"⚡⚡ [AUTOMATIC_REVERSE] ⚡⚡ | Sym: {sym} | Action: {'buy' if is_long else 'sell'} -> {new_direction}")
            return
    # --- A.1 第一階段：動能獵殺 (Momentum Exit) ---
    atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)
    profit_atr_mult = (p - avg) / atr_val if is_long else (avg - p) / atr_val
    
    if profit_atr_mult > 3.0:
        macd_hist = s.get("macd_hist", 0.0)
        prev_macd_hist = s.get("prev_macd_hist", 0.0)
        rsi = s.get("current_rsi", 50.0)
        prev_rsi = s.get("prev_rsi", rsi)
        
        momentum_failing = False
        if is_long:
            if macd_hist < prev_macd_hist or rsi <= prev_rsi:
                momentum_failing = True
        else:
            if macd_hist > prev_macd_hist or rsi >= prev_rsi:
                momentum_failing = True
                
        if momentum_failing:
            print(f"✅ [Momentum_Exit] {sym} 獲利達標 (3.0 ATR) 且動能衰竭，早期獲利平倉！")
            cs = "sell" if is_long else "buy"
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Momentum_Exit]")
            return

    # --- 第三階段：主動觸發救援撤退 (Rescue DCA Trigger) ---
    loss_limit = get_effective_exit_setting(sym, "risk_threshold_pct", 0.0025, is_long)
    if profit_pct <= -loss_limit and s.get("entry_count", 0) == 1:
        print(f"⚠️ [Rescue_DCA_Triggered] {sym} 虧損突破 {loss_limit*100:.4f}%，啟動緊急救援加碼！")
        cs = "buy" if is_long else "sell"
        # 繞過常規防護
        await execute_order(sym, cs, p, allocation_pct=0.33, is_rescue_dca=True)
        return

    # --- B. 救援式 DCA 速戰速決系統 (Rescue Mode) ---
    if s.get("entry_count", 0) > 0:
        time_since_last_entry = time.time() - s.get("last_entry_time", 0.0)
        
        # 1. 15分鐘強制撤離
        rescue_timeout_min = get_effective_exit_setting(sym, "rescue_timeout_min", 15, is_long)
        if time_since_last_entry > rescue_timeout_min * 60:
            print(f"⚠️ [RESCUE_TIMEOUT] {sym} 救援模式逾時 {rescue_timeout_min} 分鐘未達標，強制平倉撤退！")
            cs = "sell" if is_long else "buy"
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Rescue_Timeout]", is_stop_loss=True)
            return

        # 2. 動態追蹤脫困邏輯 (Dynamic Trailing Rescue)
        rescue_floor = get_effective_exit_setting(sym, "rescue_tp_floor_pct", 0.002, is_long)
        rescue_trail_atr = get_effective_exit_setting(sym, "rescue_trailing_atr", 0.75, is_long)

        if profit_pct >= rescue_floor:
            # 追蹤最高/最低價
            if is_long:
                s["rescue_highest"] = max(s.get("rescue_highest", 0.0), p)
                trail_sl = s["rescue_highest"] - (atr_val * rescue_trail_atr)
                if p <= trail_sl:
                    print(f"✅ [RESCUE_TRAIL] {sym} 救援模式動態追蹤觸發！(獲利 {profit_pct*100:.2f}%)，獲利入袋！")
                    await close_position(sym, "sell", abs(s["qty"]), p, avg, reason="[Rescue_Trailing_Stop]")
                    return
            else:
                s["rescue_lowest"] = min(s.get("rescue_lowest", float('inf')), p) if s.get("rescue_lowest", 0) > 0 else p
                trail_sl = s["rescue_lowest"] + (atr_val * rescue_trail_atr)
                if p >= trail_sl:
                    print(f"✅ [RESCUE_TRAIL] {sym} 救援模式動態追蹤觸發！(獲利 {profit_pct*100:.2f}%)，獲利入袋！")
                    await close_position(sym, "buy", abs(s["qty"]), p, avg, reason="[Rescue_Trailing_Stop]")
                    return
            
            if time.time() - s.get("last_rescue_log_time", 0) > 60:
                print(f"👀 [RESCUE_RUNNER] {sym} 救援模式啟動追蹤！目前獲利 {profit_pct*100:.2f}% (目標底線: {rescue_floor*100:.2f}%)")
                s["last_rescue_log_time"] = time.time()
            return


    # cooldown_limit 過後才進此函數，所以 120 秒邊界仍有意義（低波動情況下 60~120 秒區間）
    sl_base_raw = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long)
    
    # --- 動態停損乘數 (Dynamic ATR Multiplier) ---
    sl_base = get_dynamic_atr_multiplier(sym, sl_base_raw)

    sl_mult = sl_base * 1.5 if hold_sec < 120 else sl_base
    atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)
    tp_base = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), is_long)
    
    # ── 加入最低距離保護 (Minimum Distance Floor) ──
    sl_dist = max(sl_mult * atr_val, avg * 0.005)
    tp_dist = max(tp_base * atr_val, avg * 0.015)
    
    tp = avg + tp_dist if is_long else avg - tp_dist

    # ── 動態保本防護 (Dynamic Breakeven) ──
    # 只要利潤達到 0.6 倍 ATR (或至少 0.15%)，就將停損鎖定至保本點
    entry_atr_pct = (s.get("entry_atr", atr_val) / avg) if avg > 0 else 0.002
    breakeven_threshold = max(entry_atr_pct * 0.6, 0.0015)
    
    # 1. 根據交易模式動態決定保本緩衝
    slippage_buffer = 0.0 if PAPER_TRADING else 0.0005

    if s.get("highest_profit_pct", 0.0) >= breakeven_threshold:
        # 2. 計算移動保本線
        if is_long:
            breakeven_price = avg * (1 + slippage_buffer)
            # 做多時：如果算出新的保本價比原本的止損價還高，才往上鎖定
            if breakeven_price > s.get('stop_loss', 0):
                s['stop_loss'] = breakeven_price
                if not s.get('is_breakeven_locked'):
                    s['is_breakeven_locked'] = True
                    print(f"🛡️ [{sym}] 獲利達標，移動保本線已鎖定在：{breakeven_price:.4f}")
        else:
            breakeven_price = avg * (1 - slippage_buffer)
            # 做空時：如果算出新的保本價比原本的止損價還低，才往下鎖定
            if s.get('stop_loss', float('inf')) > breakeven_price:
                s['stop_loss'] = breakeven_price
                if not s.get('is_breakeven_locked'):
                    s['is_breakeven_locked'] = True
                    print(f"🛡️ [{sym}] 獲利達標，移動保本線已鎖定在：{breakeven_price:.4f}")
                    
    # 如果還沒鎖定保本，設定為預設的 sl_dist
    if not s.get("is_breakeven_locked"):
        s["stop_loss"] = avg - sl_dist if is_long else avg + sl_dist

    # 使用狀態變數的 stop_loss
    sl = s.get("stop_loss", avg)

    # --- 停損同步 (Trailing SL Sync) - Philosophy B+ ---
    if s.get("entry_count", 0) > 0:
        first_entry = s.get("first_entry_price", avg)
        atr_half = s.get("current_atr", atr_val) * 0.5
        
        if is_long:
            sl_floor = first_entry - atr_half + avg * 0.001
            sl = max(sl, sl_floor)
        else:
            sl_floor = first_entry + atr_half - avg * 0.001
            sl = min(sl, sl_floor)

    # ── 最低停損距離保護 (Minimum Stop-Loss Distance) ──
    hard_sl_pct = get_effective_exit_setting(sym, "hard_stop_loss_pct", s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT), is_long)
    
    if is_long:
        hard_sl_limit = avg * (1 - hard_sl_pct)
        # A. 距離保護：確保停損點距離現價至少 0.5% (如果太近，強制往下壓低至 0.5% 距離)
        if sl > p * 0.995:
            sl = p * 0.995
        
        # B. 硬性停損限制：停損點絕對不能低於硬性停損線 (不能虧損超過 2%)
        if sl < hard_sl_limit:
            sl = hard_sl_limit
    else:
        hard_sl_limit = avg * (1 + hard_sl_pct)
        # A. 距離保護：確保停損點距離現價至少 0.5% (如果太近，強制往上拉高至 0.5% 距離)
        if sl < p * 1.005:
            sl = p * 1.005
        
        # B. 硬性停損限制：停損點絕對不能高於硬性停損線 (不能虧損超過 2%)
        if sl > hard_sl_limit:
            sl = hard_sl_limit

    # [新增] 事件觸發型縮短停損 (Event-triggered SL Shrink)
    is_bear_market = not MARKET_WIND.get("allow_long", True)
    is_bull_market = not MARKET_WIND.get("allow_short", True)
    if hold_sec > 1800: # 持倉超過 30 分鐘
        if (is_long and is_bear_market) or (not is_long and is_bull_market):
            shrink_ratio = 0.5
            new_sl_dist = atr_val * sl_atr_multiplier * shrink_ratio
            if is_long:
                new_sl = avg - new_sl_dist
                if new_sl > sl:
                    sl = new_sl
                    print(f"⚠️ [事件觸發防護] {sym} 持倉>30分且大盤逆風，強制縮短停損至 {sl_atr_multiplier*shrink_ratio:.2f} ATR (新停損價: {sl:.4f})")
            else:
                new_sl = avg + new_sl_dist
                if new_sl < sl:
                    sl = new_sl
                    print(f"⚠️ [事件觸發防護] {sym} 持倉>30分且大盤逆風，強制縮短停損至 {sl_atr_multiplier*shrink_ratio:.2f} ATR (新停損價: {sl:.4f})")

    if profit_pct > s["highest_profit_pct"]:
        s["highest_profit_pct"] = profit_pct
    if profit_pct < 0:
        s["has_been_negative"] = True

    # ── [新增] Trailing Stop 機制 ──
    # 當未實現獲利 >= 1.0% 時，啟動移動停損；若從最高點回撤 1.2%，則平倉鎖利。
    ts_activation_pct = 0.010
    ts_retracement_pct = 0.012
    if s["highest_profit_pct"] >= ts_activation_pct:
        if profit_pct <= (s["highest_profit_pct"] - ts_retracement_pct):
            cs = 'sell' if is_long else 'buy'
            print(f"📉 [移動停損鎖利] {sym} 從最高獲利 {s['highest_profit_pct']*100:.2f}% 回撤 {ts_retracement_pct*100:.2f}%，觸發出場。")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Trailing_Stop]")
            s["highest_profit_pct"] = 0.0
            return

    regime_decision, regime_reason = detect_market_regime(sym, p, avg, is_long)
    if regime_decision == "BREAKOUT_REVERSAL":
        cs = 'sell' if is_long else 'buy'
        print(f"🚨 [市場 regime] {sym} {regime_reason}，立即平倉並考慮反手")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Breakout_Fail]_Fail]", is_stop_loss=True)
        s["highest_profit_pct"] = 0.0
        
        # 自動反手邏輯：設置 pending_reverse，並防護避免短時間來回反手
        last_reverse = s.get("last_reverse_time", 0)
        if time.time() - last_reverse > 1800:  # 30分鐘內不允許連續反手
            s["pending_reverse"] = "sell" if is_long else "buy"
            s["pending_reverse_time"] = time.time()
            s["last_reverse_time"] = time.time()
        else:
            print(f"⏳ [反手冷卻] {sym} 距離上次反手不到 30 分鐘，為了防禦假震盪，本次放棄反手。")
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
            macd_hist_expanding = False
            try:
                closes = np.array([x[4] for x in s["ohlcv"]])
                _, _, m_hist, p_line, p_sig = calculate_macd(closes)
                p_hist = p_line - p_sig
                macd_hist_expanding = abs(m_hist) > abs(p_hist)
            except:
                pass
            
            if not macd_hist_expanding:
                cs = 'sell' if is_long else 'buy'
                print(f"📉 [動能衰減] {sym} 利潤從最高 {s['highest_profit_pct']*100:.2f}% 回落 25% (現為 {profit_pct*100:.2f}%) 且 MACD 衰退，提早獲利了結")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Whipsaw_Stop_top]")
                s["highest_profit_pct"] = 0.0
                return
    if p > s["trailing_highest"]:
        s["trailing_highest"] = p
    if p < s["trailing_lowest"]:
        s["trailing_lowest"] = p

    # 1. 趨勢反轉：MACD 連續兩根狀態反向 → 認賠出場 (避免單根 MACD 雜訊)
    macd_is_down = (s["macd_line"] < s["macd_signal"]) and (s.get("prev_macd_line", 0.0) < s.get("prev_macd_signal", 0.0))
    macd_is_up = (s["macd_line"] > s["macd_signal"]) and (s.get("prev_macd_line", 0.0) > s.get("prev_macd_signal", 0.0))
    sl_pct = s.get("hard_stop_loss_pct", 0.02)
    early_exit_limit = -(sl_pct * 0.5)
    if ((is_long and macd_is_down) or (not is_long and macd_is_up)) and (profit_pct < early_exit_limit or profit_pct > 0.015):
        cs = 'sell' if is_long else 'buy'
        is_sl = profit_pct < 0.0
        print(f"📉 [反轉出場] {sym} MACD連續兩根確認反向且達門檻，立即平倉 (損益: {profit_pct*100:.2f}%)")
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
    
    # 引入「個性化」停利目標 (Personality-based TP)
    personality = s.get("personality", "steady_trend")
    tier_mult = 1.0
    if personality == "calm_range":
        tier_mult = 0.8
    elif personality == "volatile_breakout":
        tier_mult = 1.2
        
    tier3_target = max(atr_pct * 4.0 * tier_mult, 0.012 * tier_mult, 0.008)
    tier2_target = max(atr_pct * 2.5 * tier_mult, 0.006 * tier_mult, 0.006)
    tier1_target = max(atr_pct * 1.5 * tier_mult, 0.003 * tier_mult, 0.003)
    # ── 動能竭盡 (量價背離) 頂部逃頂機制 (升級版) ──
    # 結合了「爆發後衰竭」、「位移停滯」、「位置過濾」三大核心
    if len(s["ohlcv"]) >= 5:
        c1 = s["ohlcv"][-2]  # 最新已收盤 K 線
        c2 = s["ohlcv"][-3]  # 前一根已收盤 K 線
        
        # 1. 爆發後的衰竭 (Climax-Exhaustion)
        # 檢查過去 4 根已收盤 K 線中，是否曾出現過高量爆發 (> 1.5倍均量)
        recent_vols = [x[5] for x in s["ohlcv"][-5:-1]]
        vol_ma20 = s.get("vol_ma20", 0)
        has_recent_climax = max(recent_vols) > vol_ma20 * 1.5 if vol_ma20 > 0 else True
        
        # 2. 價格位移進展 (Price Progress)
        # 如果價格仍在創新高/低，視為健康休整，不觸發衰竭
        is_moving_progress = (p > c1[2]) if is_long else (p < c1[3])
        
        # 3. 價格位置過濾 (Location Confluence)
        sma200 = s.get("sma200_15m", 0)
        bb_up = s.get("bb_up", 0)
        bb_low = s.get("bb_low", 0)
        
        near_resistance = (bb_up > 0 and p >= bb_up * 0.99) or (sma200 > 0 and p >= sma200 * 1.01)
        near_support = (bb_low > 0 and p <= bb_low * 1.01) or (sma200 > 0 and p <= sma200 * 0.99)
        extreme_resistance = bb_up > 0 and p >= bb_up
        extreme_support = bb_low > 0 and p <= bb_low
        
        # 極端區域直接視為有效位置，否則需接近壓力/支撐
        is_valid_location = (is_long and (near_resistance or extreme_resistance)) or (not is_long and (near_support or extreme_support))
        
        # 判斷是否為盤整區間 (ATR 低於 24 小時平均的 80%)
        is_in_consolidation = (current_atr > 0 and atr_24h_avg > 0 and current_atr < atr_24h_avg * 0.8)
        
        # 盤整區間要求更嚴格的衰竭門檻 (量能小於 50%)，否則使用一般門檻 (65%)
        vol_threshold = 0.50 if is_in_consolidation else 0.65
        
        divergence_exit = False
        # 綜合觸發：曾有爆發 + 已經停滯 + 位於關鍵區 + 量縮
        if has_recent_climax and not is_moving_progress and is_valid_location:
            if is_long and c1[4] > c2[4] and c1[5] < c2[5] * vol_threshold:
                divergence_exit = True
            elif not is_long and c1[4] < c2[4] and c1[5] < c2[5] * vol_threshold:
                divergence_exit = True
            
        if divergence_exit and profit_pct >= 0.0015:
            cs = 'sell' if is_long else 'buy'
            print(f"📉 [量價背離] {sym} 抵達關鍵區位且量縮停滯 (V:{c1[5]:.0f} < {vol_threshold:.2f}x)，動能竭盡提前平倉！")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Vol_Divergence]")
            s["highest_profit_pct"] = 0.0
            return

    if True: # 動態回吐防護 移動停利 (Trailing Stop)
        # [新增] 獲利達標強制停止加倉
        if profit_pct > 0.02 and s.get("entry_count", 0) > 0 and s.get("max_additional_entries", 0) > 0:
            print(f"🎯 [強制鎖利] {sym} 獲利已達 2%，鎖定利潤，禁止繼續加倉")
            s["max_additional_entries"] = 0

        # 只要利潤達到基本門檻 (tier1_target)，就啟動動態移動停利
        if s["highest_profit_pct"] >= tier1_target:
            atr_val = s.get("current_atr", 0)
            atr_ma20 = s.get("atr_ma20", 0)
            trail_trigger = 0.80 if atr_val > atr_ma20 else 0.85
            
            # 多層放寬回撤門檻 (Trailing Stop Flexibility)
            if len(s.get("entries", [])) > 1:
                trail_trigger -= 0.05  # 給大多頭趨勢更多的呼吸空間
            
            # 當前回落超過動態觸發點
            if profit_pct <= s["highest_profit_pct"] * trail_trigger:
                cs = 'sell' if is_long else 'buy'
                print(f"🛡️ [動態移動停利] {sym} 利潤從最高 {s['highest_profit_pct']*100:.3f}% 回吐 (觸發點 {trail_trigger:.2f})，於 {profit_pct*100:.3f}% 鎖定利潤出場")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason=f"[Trailing_Stop_{trail_trigger}]")
                s["highest_profit_pct"] = 0.0
                return

    # 取消固定百分比停利，改由移動停損 (Trailing Stop) 統一接管，以利捕捉最大波段

    if True: # 移除 is_strong 阻擋，讓時間衰減正常運作
        # ── 盤整／弱勢路線 ────────────────────────────────
        # 將「時間僵局」轉向「量能僵局」 (Volume Stagnation)
        recent_vols = [x[5] for x in s["ohlcv"][-4:-1]] if len(s["ohlcv"]) >= 4 else []
        vol_ma20 = s.get("vol_ma20", 1)
        is_vol_stagnant = len(recent_vols) >= 3 and all(v < vol_ma20 * 0.6 for v in recent_vols)
        bb_width = s.get("bb_up", 0) - s.get("bb_low", 0)
        is_range_tight = (bb_width / p) < 0.003 if p > 0 else False
        
        # 絕對時間衰減出局 (Time-Decay Exit)
        entry_layers = len(s.get("entries", []))
        time_decay_limit = 900 if entry_layers <= 1 else 2700  # 單層15分鐘，多層放寬至45分鐘
        
        if hold_sec > time_decay_limit and profit_pct >= 0.0015:
            cs = 'sell' if is_long else 'buy'
            print(f"⏳ [時間衰減] {sym} 持倉已達 {hold_sec//60} 分鐘且有微利，不想浪費時間，直接全平！")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Time_Decay_Exit]")
            s["highest_profit_pct"] = 0.0
            return
        
        stagnation_limit = get_dynamic_stagnation_limit(s["current_atr"], s["atr_ma20"])
        if hold_sec > stagnation_limit and profit_pct >= 0.0015:
            if is_vol_stagnant and is_range_tight:
                if not s["has_partial_closed"]:
                    # 若利潤大於 0.6% 則先平 50% 鎖定部分利潤，否則微利情況下直接全平以釋放資金
                    if 0.006 <= profit_pct < 0.008:
                        half = abs(s["qty"]) * 0.5
                        cs = 'sell' if is_long else 'buy'
                        print(f"⏳ [量能僵局] {sym} 持倉{stagnation_limit//60}分且量縮橫盤，平50%")
                        await close_position(sym, cs, half, p, avg, reason="[Vol_Stagnation_1]")
                        s["has_partial_closed"] = True
                        return
                    else:
                        cs = 'sell' if is_long else 'buy'
                        reason = "[Vol_Stagnation_Exit]" if profit_pct >= 0.008 else "[Stagnation_BreakEven]"
                        print(f"⏳ [量能僵局] {sym} 持倉{stagnation_limit//60}分且量縮橫盤，全平釋放資金")
                        await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason)
                        s["highest_profit_pct"] = 0.0
                        return
        # 僵局二階：平過50% + 8分仍未突破1% → 全平 (加入最小獲利緩衝 0.15%)
        if s["has_partial_closed"] and hold_sec > 480 and 0.0015 <= profit_pct < 0.01:
            if is_vol_stagnant and is_range_tight:
                cs = 'sell' if is_long else 'buy'
                print(f"⏳ [量能僵局] {sym} 剩餘50%持倉8分仍未突破1%且量縮橫盤，全平")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Vol_Stagnation_2]")
                s["highest_profit_pct"] = 0.0
                return
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

        # 強勢動態停利：依據利潤給予不同的回撤保護
        if s["highest_profit_pct"] >= 0.005:
            retrace_limit = 0.005 if s["highest_profit_pct"] >= 0.01 else 0.0025
            limit_up = 1.0 + retrace_limit
            limit_down = 1.0 - retrace_limit
            
            if (is_long and p <= s["trailing_highest"] * limit_down) or (not is_long and p >= s["trailing_lowest"] * limit_up):
                cs = 'sell' if is_long else 'buy'
                print(f"🏃 [動態停利] {sym} 利潤達標後回撤 {retrace_limit*100:.2f}%")
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




# ── 進場邏輯 ──────────────────────────────────────────────────

async def execute_order(sym, side, price, allocation_pct=0.33, is_rescue_dca=False):
    s = STATES[sym]
    pk = paper_key(sym)
    lev = get_symbol_leverage(sym)
    s["leverage"] = lev
    print(f"@@LEVERAGE@@{lev}")
    
    # [新增] Order Book Disbalance - 動態盤口門檻 (Adaptive OrderFlow)
    if not is_rescue_dca:
        try:
            orderbook = await exchange_futures.fetch_order_book(sym, limit=20)
            bids = sum(x[1] for x in orderbook.get('bids', []))
            asks = sum(x[1] for x in orderbook.get('asks', []))
            # 依 VolMode 動態調整門檻：低波動 → 0.85，高波動 → 0.95
            _s = STATES.get(sym, {})
            _atr_hist_of = _s.get("atr_history", [])
            _atr_avg_of = float(np.mean(_atr_hist_of)) if len(_atr_hist_of) > 0 else 0.0
            _atr_cur_of = _s.get("current_atr", 0.0)
            _is_low_vol_of = (_atr_avg_of > 0 and _atr_cur_of <= _atr_avg_of)
            _flow_threshold = 0.85 if _is_low_vol_of else 0.95
            _flow_label = f"低波動放寬 {_flow_threshold}" if _is_low_vol_of else f"高波動嚴格 {_flow_threshold}"
            if side == 'buy':
                if asks == 0 or bids / asks < _flow_threshold:
                    print(f"🛑 [Filter:OrderFlow] {sym} 買盤支撐不足 (BidVol: {bids:.2f} / AskVol: {asks:.2f} < {_flow_threshold} | {_flow_label})，疑似假突破，拒絕做多！")
                    return
            else:
                if bids == 0 or asks / bids < _flow_threshold:
                    print(f"🛑 [Filter:OrderFlow] {sym} 賣盤壓力不足 (AskVol: {asks:.2f} / BidVol: {bids:.2f} < {_flow_threshold} | {_flow_label})，疑似假跌破，拒絕做空！")
                    return
        except Exception as e:
            print(f"⚠️ [OrderFlow] 讀取掛單簿失敗 {sym}: {e}")
    if not PAPER_TRADING:
        try:
            await exchange_futures.set_leverage(lev, convert_to_ccxt_symbol(sym))
        except Exception as e:
            print(f"⚠️ [槓桿設定失敗] {sym}: {e}")
            
    margin = compute_per_coin_margin(sym, allocation_pct)
    


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
    if s["entry_count"] > 0 and not is_rescue_dca:
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
            if profit_pct < 0.008:
                print(f"🛑 [金字塔防護] {sym} 目前利潤 {profit_pct*100:.2f}% 未達安全門檻 0.8%，拒絕加倉以防拉高成本！")
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
            if current_vol < vol_ma20 * 0.6:
                print(f"🛑 [量能過濾] {sym} 當前量能低於均量 0.6 倍，動能不足拒絕加倉！")
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
            
                # [新增] RSI 背離與強度權重
                macd_hist = s.get("macd_hist", 0.0)
                prev_macd_hist = 0.0
                if len(s.get("ohlcv", [])) >= 34:
                    try:
                        import numpy as np
                        closes = np.array([x[4] for x in s["ohlcv"]])
                        _, _, m_hist, p_line, p_sig = calculate_macd(closes)
                        macd_hist = m_hist
                        prev_macd_hist = p_line - p_sig
                    except:
                        pass
            
                rsi = s.get("current_rsi", 50.0)
                is_strong_long = rsi > 70 and macd_hist > 0 and macd_hist > prev_macd_hist
                is_strong_short = rsi < 30 and macd_hist < 0 and macd_hist < prev_macd_hist
            
                if side == 'buy' and is_bull1 and is_bull2 and body1 < body2 * 0.8 and vol1 < vol2 * 0.8:
                    if is_strong_long:
                        print(f"@@COIN_DEBUG@@ ⚡ [斜率過濾] {sym} 強勢突破中，忽略實體與量能衰減")
                    else:
                        print(f"🛑 [斜率過濾] {sym} 價格創高但實體與量能雙雙衰減，動能不足拒絕加碼！")
                        return
                if side == 'sell' and not is_bull1 and not is_bull2 and body1 < body2 * 0.8 and vol1 < vol2 * 0.8:
                    if is_strong_short:
                        print(f"@@COIN_DEBUG@@ ⚡ [斜率過濾] {sym} 強勢跌破中，忽略實體與量能衰減")
                    else:
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

        # 1. 空間關卡 (Space Check): 距離上一次加倉是否大於 1.0 * ATR
        current_atr = s.get("current_atr", 0.0)
        last_entry_price = s.get("last_entry_price", s.get("avg_price", 0.0))
        if last_entry_price > 0 and current_atr > 0:
            price_diff = abs(price - last_entry_price)
                    # 動態空間門檻 (依幣種性格)
        personality = s.get("personality", "balanced")
        profile_type = COIN_PROFILE_CONFIG.get(sym, {}).get("profile_type", "")
        # 根據性格調整空間門檻 (Core_Trend 允許更緊湊的加碼)
        if profile_type in ["Core_Trend", "High_Beta_Momentum"]:
            space_threshold = 0.8 * current_atr
        else:
            space_threshold = 1.0 * current_atr
            
        if price_diff < max(space_threshold, price * 0.005):
                print(f"🛑 [空間關卡] {sym} 加倉距離不足! 差距: {price_diff:.4f} < 門檻: {max(space_threshold, price * 0.005):.4f}")
                return
                
        # 2. 動能關卡 (Momentum Check): 量能與 MACD 雙重確認
        macd_line = s.get("macd_line", 0.0)
        macd_signal = s.get("macd_signal", 0.0)
        prev_macd_line = s.get("prev_macd_line", 0.0)
        prev_macd_signal = s.get("prev_macd_signal", 0.0)
        macd_hist = macd_line - macd_signal
        prev_macd_hist = prev_macd_line - prev_macd_signal
        
        # [新增] 強勢行情豁免邏輯 (High Momentum Exemption)
        rsi = s.get("current_rsi", 50.0)
        is_strong_momentum_long = (side == 'buy' and rsi > 75 and macd_hist > 0 and macd_hist > prev_macd_hist)
        is_strong_momentum_short = (side == 'sell' and rsi < 25 and macd_hist < 0 and macd_hist < prev_macd_hist)
        
        if is_strong_momentum_long or is_strong_momentum_short:
            print(f"@@COIN_DEBUG@@ 🚀 [強勢豁免] {sym} RSI與MACD動能極強，豁免量能過濾直接加倉！")
        else:
            if not is_entry_volume_confirmed(sym, side):
                print(f"🛑 [動能關卡] {sym} 量能不足以支持加倉!")
                return
        
        # 確保方向一致
        if (side == 'buy' and macd_hist <= 0) or (side == 'sell' and macd_hist >= 0):
            print(f"🛑 [動能關卡] {sym} MACD動能不一致 (Hist: {macd_hist:.4f})，拒絕加倉!")
            return
            
        # 確保動能擴張 (MACD 柱線絕對值必須嚴格擴張)
        if abs(macd_hist) <= abs(prev_macd_hist):
            print(f"🛑 [動能關卡] {sym} MACD動能未擴張 (Hist: {abs(macd_hist):.5f} <= Prev: {abs(prev_macd_hist):.5f})，拒絕加倉!")
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
        base_allocation = 0.40  # 首倉 40%
    elif s["entry_count"] == 1:
        base_allocation = 0.25  # 次倉 25%
    else:
        base_allocation = 0.15  # 再倉 15%
        
    atr_val = s.get("current_atr", 0.0)
    atr_ma20 = s.get("atr_ma20", atr_val)
    
    if atr_val > atr_ma20 * 1.5:
        volatility_penalty = 0.7  # 波動過大時，只投 70% 的預定比例
    else:
        volatility_penalty = 1.0
        
    allocation_pct = base_allocation * volatility_penalty
    base_notional = target_notional * allocation_pct
    
    # [新增] 強制加入一個保護層：單筆加倉上限為總資產的 10% (轉換為名義價值)
    base_notional = min(base_notional, get_balance() * 0.1 * lev)
    
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
                
            if "entries" not in s:
                s["entries"] = []
            s["entries"].append({"price": price, "qty": base_amt, "time": now, "side": side})

            s["open_time"] = now
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["last_entry_price"] = price
            s["last_entry_direction"] = side
            s["entry_count"] += 1
            
            if s["entry_count"] == 1:
                s["is_breakeven_locked"] = False
                s["highest_profit_pct"] = 0.0
            
            # 強制校準 trailing stop
            update_trailing_stop(sym, price, side == 'buy')
            
            # 保本鎖定
            if s["entry_count"] >= 2:
                first_entry_price = s["entries"][0]["price"]
                if side == 'buy':
                    s["trailing_stop_price"] = max(s["trailing_stop_price"], first_entry_price)
                else:
                    s["trailing_stop_price"] = min(s["trailing_stop_price"], first_entry_price) if s["trailing_stop_price"] > 0 else first_entry_price
                s["is_breakeven_locked"] = True
                
            direction = "做多" if side == 'buy' else "做空"
            print(f"🟢 [{direction}] {sym} {base_amt:.4f} @ {price} (保證金:{margin:.2f} USDT)")
        except Exception as e:
            print(f"🛑 [模擬開倉失敗] {sym}: {e}")
    else:
        try:
            # === 1. 對價限價單策略 (Aggressive Limit Order) ===
            # 抳取盤口 Ask1/Bid1 作為限價，最大化成交機率
            try:
                ob = await exchange_futures.fetch_order_book(sym, limit=5)
                asks = ob.get('asks', [])
                bids = ob.get('bids', [])
                ask1 = float(asks[0][0]) if asks else price
                bid1 = float(bids[0][0]) if bids else price

                # === 右側掛單策略 (Right-Side Limit Execution) ===
                # 判斷是否為逆勢做多（熊市防禦模式下的 buy）
                _btc_4h = MARKET_WIND.get("btc_trend_4h")
                _btc_1h = MARKET_WIND.get("btc_trend_1h")
                _is_counter_trend_long = (side == 'buy' and _btc_4h == "BEAR" and _btc_1h == "BEAR")

                if _is_counter_trend_long:
                    # 逆勢做多：嚴格掛在 Ask1（右側），只有價格反彈才成交，防止「接飛刀」
                    limit_price = ask1
                    print(f"📌 [Right-Side Limit] {sym} 逆勢多單，嚴格掛 Ask1: {limit_price:.6f} (信號價: {price:.6f})")
                elif side == 'buy':
                    # 順勢多單：掛在 Bid1 下方等被動成交（享 Maker 優勢）
                    tick = (ask1 - bid1) if ask1 > bid1 else price * 0.0001
                    limit_price = round(bid1 - tick * 0.5, 8)
                    print(f"📌 [Passive Limit] {sym} 順勢多單，掛 Bid1 附近: {limit_price:.6f} (Bid1: {bid1:.6f})")
                else:
                    # 做空（順勢 or 逆勢）：掛在 Bid1（右側），只有買盤接貨才成交
                    limit_price = bid1
                    print(f"📌 [Right-Side Limit] {sym} 空單，掛 Bid1: {limit_price:.6f} (信號價: {price:.6f})")
            except Exception:
                limit_price = price  # Fallback to signal price

            order = await exchange_futures.create_order(
                sym, type='limit', side=side, amount=base_amt, price=limit_price,
                params={'marginMode': 'isolated', 'timeInForce': 'GTC'}
            )
            order_id = order['id']
            order_ts = time.time()

            # 記錄挂單到監控表
            PENDING_LIMIT_ORDERS[order_id] = {
                "sym": sym, "side": side, "qty": base_amt,
                "price": limit_price, "timestamp": order_ts
            }
            print(f"⏳ [限價單挂出] {sym} {side} {base_amt:.4f} @ {limit_price:.6f} (ID: {order_id})")

            # === 2. 等待成交 (3 秒內快速確認) ===
            await asyncio.sleep(3)
            try:
                fetched = await exchange_futures.fetch_order(order_id, sym)
                status = fetched.get('status', '')
                filled_qty = float(fetched.get('filled', 0.0))
            except Exception:
                status = 'unknown'
                filled_qty = 0.0

            if status == 'closed' or filled_qty >= base_amt * 0.99:
                # 完全成交：移出監控表
                PENDING_LIMIT_ORDERS.pop(order_id, None)
                fill_price = float(fetched.get('average') or fetched.get('price') or limit_price)
                print(f"✅ [限價成交] {sym} {side} {filled_qty:.4f} @ {fill_price:.6f}")
            elif filled_qty > 0:
                # 部分成交：留在監控表，以實際成交量計算
                fill_price = float(fetched.get('average') or limit_price)
                base_amt = filled_qty  # 更正為實際成交量
                print(f"⚠️ [部分成交] {sym} 實際成交: {filled_qty:.4f} (OK率: {filled_qty/base_amt*100:.1f}%)")
            else:
                # 尚未成交：已在監控表，等待止單機制處理
                print(f"⏳ [等待成交] {sym} 限價單 {order_id} 尚未成交，由逃期止單機制接管")
                return  # 不更新狀態，等逐期止單後再同步

            # === 3. 實體持倉同步 (Actual Position Sync) ===
            try:
                positions = await exchange_futures.fetch_positions([sym])
                actual_pos = next((p for p in positions if p.get('symbol') == sym and abs(float(p.get('contracts', 0) or 0)) > 0), None)
                if actual_pos:
                    actual_qty = float(actual_pos.get('contracts', 0) or 0)
                    actual_side_sign = 1 if side == 'buy' else -1
                    s["qty"] = actual_qty * actual_side_sign
                    print(f"📊 [持倉同步] {sym} 交易所實際持倉: {s['qty']:.4f}")
                else:
                    # Fallback: 用內對計算量
                    old_qty = s["qty"]
                    if side == 'buy':
                        s["qty"] += base_amt
                    else:
                        s["qty"] -= base_amt
            except Exception as pe:
                print(f"⚠️ [持倉同步失敗] {sym}: {pe}")
                old_qty = s["qty"]
                if side == 'buy':
                    s["qty"] += base_amt
                else:
                    s["qty"] -= base_amt

            slippage = abs(fill_price - price) / price if price > 0 else 0
            print(f"✅ [實盤開倉成功] {sym} {side} | 信號價: {price:.6f} | 限價: {limit_price:.6f} | 實際: {fill_price:.6f} | 滑價: {slippage*100:.3f}%")

            if s["avg_price"] <= 0:
                s["avg_price"] = fill_price
                s["entry_atr"] = max(s.get("current_atr", 0.0), fill_price * 0.005)
            else:
                old_abs_qty = abs(old_qty) if 'old_qty' in locals() else 0.0
                s["avg_price"] = ((s["avg_price"] * old_abs_qty) + (fill_price * base_amt)) / abs(s["qty"])

            if "entries" not in s:
                s["entries"] = []
            s["entries"].append({"price": fill_price, "qty": base_amt, "time": now, "side": side})

            s["open_time"] = now
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["last_entry_price"] = fill_price
            s["last_entry_direction"] = side
            s["entry_count"] += 1

            if s["entry_count"] == 1:
                s["is_breakeven_locked"] = False
                s["highest_profit_pct"] = 0.0

            # 強制校準 trailing stop
            update_trailing_stop(sym, fill_price, side == 'buy')

            # 保本鎖定
            if s["entry_count"] >= 2:
                first_entry_price = s["entries"][0]["price"]
                if side == 'buy':
                    s["trailing_stop_price"] = max(s["trailing_stop_price"], first_entry_price)
                else:
                    s["trailing_stop_price"] = min(s["trailing_stop_price"], first_entry_price) if s["trailing_stop_price"] > 0 else first_entry_price
                s["is_breakeven_locked"] = True

            s["last_flip_time"] = now

            # --- 混合停損: 交易所挂單 (Stop Market) ---
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
                print(f"🛡️ [交易所挂單] {sym} 成功挂出 Stop Market 止損單 @ {stop_price} (數量: {abs(s['qty'])})")
            except Exception as se:
                print(f"🚨 [交易所止損挂單失敗] {sym}: {se}")
            # ----------------------------------------

        except Exception as e:
            print(f"🚨 [開倉錯誤] {sym}: {e}")

async def check_stale_limit_orders():
    """
    逆期止單機制 (Stale Order Cancellation)
    每 30 秒檢查一次 PENDING_LIMIT_ORDERS，
    超過 60 秒尚未成交的限價單自動撤銷。
    """
    while True:
        await asyncio.sleep(30)
        if PAPER_TRADING:
            continue
        for order_id in list(PENDING_LIMIT_ORDERS.keys()):
            info = PENDING_LIMIT_ORDERS.get(order_id)
            if not info:
                continue
            elapsed = time.time() - info["timestamp"]
            if elapsed > 60:
                sym = info["sym"]
                try:
                    await exchange_futures.cancel_order(order_id, sym)
                    print(f"⏰ [逆期止單] {sym} 限價單 {order_id} 還未成交且已超 60 秒，自動撤銷。")
                except Exception as ce:
                    print(f"⚠️ [撤銷失敗] {sym} {order_id}: {ce}")
                PENDING_LIMIT_ORDERS.pop(order_id, None)
                # 撤銷後同步實際持倉
                try:
                    positions = await exchange_futures.fetch_positions([sym])
                    actual_pos = next((p for p in positions if p.get('symbol') == sym and abs(float(p.get('contracts', 0) or 0)) > 0), None)
                    s = STATES.get(sym)
                    if s and actual_pos:
                        actual_qty = float(actual_pos.get('contracts', 0) or 0)
                        side_sign = 1 if actual_pos.get('side', '') == 'long' else -1
                        s["qty"] = actual_qty * side_sign
                        print(f"📊 [持倉同步] {sym} 撤銷後實際持倉: {s['qty']:.4f}")
                    elif s:
                        # 撤銷成功且無持倉，重置狀態
                        if s.get('qty', 0) != 0 and s.get('entry_count', 0) > 0:
                            print(f"⚠️ [撤銷同步] {sym} 限價單完全未成交，持倉為零")
                except Exception as pe:
                    print(f"⚠️ [持倉同步失敗] {sym}: {pe}")

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
    
    # [新增] MACD 動能強勁且持續放大時，放寬容錯空間
    macd_hist = s.get("macd_hist", 0.0)
    prev_macd_hist = 0.0
    try:
        if len(s.get("ohlcv", [])) >= 34:
            import numpy as np
            closes = np.array([x[4] for x in s["ohlcv"]])
            _, _, m_hist, p_line, p_sig = calculate_macd(closes)
            macd_hist = m_hist
            prev_macd_hist = p_line - p_sig
    except:
        pass

    is_strong_macd = False
    if side == 'buy' and macd_hist > 0 and macd_hist > prev_macd_hist:
        is_strong_macd = True
    elif side == 'sell' and macd_hist < 0 and macd_hist < prev_macd_hist:
        is_strong_macd = True

    if is_strong_macd:
        pin_threshold = max(pin_threshold, 5.0)
        enabled = False

    if enabled:
        print(f"@@COIN_DEBUG@@ 🔧 {sym} 反插針門檻收緊為 {pin_threshold:.1f} (body_ratio={body_ratio:.2f}, vol={s.get('current_vol',0):.0f}, ema20={ema20:.4f}) [enabled]")
    else:
        if is_strong_macd:
            print(f"@@COIN_DEBUG@@ 🚀 {sym} MACD動能強勁，放寬反插針門檻至 {pin_threshold:.1f} [relaxed]")
        else:
            print(f"@@COIN_DEBUG@@ 🔎 {sym} 反插針門檻維持寬鬆 {pin_threshold:.1f} [disabled]")

    if side == 'buy':
        # 移除嚴格的 prev_close 比較，允許提早進場抄底
        if upper_wick > body * pin_threshold:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 上影線過長 (上影線 {upper_wick:.4f} > 實體 {body:.4f} * {pin_threshold:.1f})")
            return False
        return True

    if side == 'sell':
        # Doji 辨別：實體越小於 0.05% 價格時，用 ATR 絕對値替代比例判斷
        atr_val = s.get("current_atr", 0.0)
        min_body = max(atr_val * 0.05, close_price * 0.0005) if atr_val > 0 else close_price * 0.0005
        if body < min_body:
            # Doji 蔓燭，跳過影線過濾直接送出
            return True
        if lower_wick > body * pin_threshold:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線過濾] 下影線過長 (下影線 {lower_wick:.4f} > 實體 {body:.4f} * {pin_threshold:.1f})")
            return False
        return True


def get_dynamic_volume_factor(states):
    total_current_vol = 0.0
    total_ma20_vol = 0.0
    
    for s in states.values():
        total_current_vol += s.get("current_vol", 0)
        total_ma20_vol += s.get("vol_ma20", 0)
    
    # 防止除以零的錯誤
    if total_ma20_vol == 0:
        return 1.2
        
    market_volume_state = total_current_vol / total_ma20_vol
    
    # 邏輯：如果整體市場量能低於平均水準，放寬門檻至 1.0
    # 否則保持 1.2 的高標準過濾
    return 1.0 if market_volume_state < 1.0 else 1.2


def is_entry_volume_confirmed(sym, side):
    s = STATES[sym]
    if len(s["ohlcv"]) < 2:
        return False
    current_vol = s["current_vol"]
    vol_ma20 = s["vol_ma20"]
    if vol_ma20 <= 0:
        return False
    
    # [Layer 3] 動態量能門檻 (根據幣種性格動態調整)
    base_vol_factor = s.get("volume_threshold_factor", 0.4)
    vol_factor = base_vol_factor
    
    # [新增] 大盤量能過濾：環境感知
    market_dynamic_factor = get_dynamic_volume_factor(STATES)
    if market_dynamic_factor == 1.0:
        vol_factor = 0.15
        print(f"@@COIN_DEBUG@@ ⚡ {sym} 整體市場量縮，動態放寬量能門檻至 0.15x")
    else:
        # [新增] RSI 強勢放寬量能門檻 vs 極端狂熱提高門檻
        rsi = s.get("current_rsi", 50.0)
        if s.get("is_extreme_high_rsi", False):
            vol_factor = vol_factor * 1.2
            print(f"@@COIN_DEBUG@@ ⚠️ {sym} 觸發 [極值防禦] RSI ({rsi:.1f}) 處於狂熱頂點，強制提高量能門檻至 {vol_factor:.2f}x 防追高")
        elif rsi > 70 or rsi < 30:
            vol_factor = 0.15
            print(f"@@COIN_DEBUG@@ ⚡ {sym} 行情強勢 (RSI: {rsi:.1f})，動態放寬量能門檻至 0.15x")
        else:
            # [新增] 根據 ATR 高低自動動態調整倍數
            atr_24h_avg = s.get("atr_24h_avg", 0.0)
            current_atr = s.get("current_atr", 0.0)
            atr_ratio = (current_atr / atr_24h_avg) if atr_24h_avg > 0 else 1.0
            
            if atr_ratio >= 1.5:
                vol_factor = min(1.1, vol_factor)
                print(f"@@COIN_DEBUG@@ ⚡ {sym} 波動率極高 (ATR ratio: {atr_ratio:.2f})，動態降低量能門檻至 {vol_factor}x")
            elif atr_ratio >= 1.2:
                vol_factor = max(1.0, vol_factor - 0.2)
                print(f"@@COIN_DEBUG@@ ⚡ {sym} 波動率偏高 (ATR ratio: {atr_ratio:.2f})，微調量能門檻至 {vol_factor:.1f}x")
        
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
    rr_threshold = s.get("rr_threshold", 1.3)
    if rr_ratio < rr_threshold:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [盈虧比過濾] 預計R:R ({rr_ratio:.2f}) < {rr_threshold} (TP: {tp_multiplier}x, SL: {sl_multiplier}x)")
        return False

    return True


def is_entry_allowed(sym, side, route="a", strength=0.0):
    s = STATES[sym]
    cp = s["close_price"]

    if route == "Automatic_Reverse":
        print(f"@@COIN_DEBUG@@ ⚡ [反手豁免] {sym} 來自強勢反手，跳過空間/趨勢/大盤過濾")
        return True

    # =========================================================================
    # 🔴 STAGE 0: MACRO CIRCUIT BREAKER (宏觀熔斷機制)
    # BTC 4H + 1H 雙熊 → 啟動「熊市防禦模式」，封鎖所有做多訊號
    # 除非滿足「極端超賣 RSI < 32」或「底背離確認」
    # =========================================================================
    btc_4h = MARKET_WIND.get("btc_trend_4h")
    btc_1h = MARKET_WIND.get("btc_trend_1h")
    bear_defense_mode = (btc_4h == "BEAR" and btc_1h == "BEAR")
    if bear_defense_mode and side == 'buy':
        current_rsi_macro = s.get("current_rsi", 50.0)
        divergence_confirmed = (s.get("divergence", "none") == "bullish")
        extreme_oversold    = (current_rsi_macro < 32.0)
        if not extreme_oversold and not divergence_confirmed:
            print(f"🔴 [MACRO_BLOCK] {sym} 熊市防禦模式啟動！BTC 4H+1H 雙熊，做多訊號被封鎖。"
                  f"(RSI: {current_rsi_macro:.1f} >= 32 且 無底背離)")
            return False
        reason = "極端超賣" if extreme_oversold else "底背離確認"
        print(f"⚡ [MACRO_ALLOW] {sym} 熊市防禦模式下通過特赦：{reason}！(RSI: {current_rsi_macro:.1f}, Div: {s.get('divergence', 'none')})")

    # =========================================================================
    # 🛑 STAGE 1: HARD GATES (硬門檻 - 不通過直接攔截)
    # =========================================================================
    # 1. 動態量能門檻過濾 (Adaptive Volume Gate)
    # 低波動模式下放寬至 60%，避免過度攔截安靜行情
    current_volume = s["ohlcv"][-1][5] if s.get("ohlcv") else 0
    volume_ma20 = s.get("vol_ma20", 0.0)
    atr_history_v = s.get("atr_history", [])
    atr_24h_avg_v = float(np.mean(atr_history_v)) if len(atr_history_v) > 0 else 0.0
    current_atr_v = s.get("current_atr", 0.0)
    is_low_vol_mode = (atr_24h_avg_v > 0 and current_atr_v <= atr_24h_avg_v)
    vol_multiplier = 0.6 if is_low_vol_mode else 1.0
    dynamic_vol_threshold = volume_ma20 * vol_multiplier
    if current_volume <= dynamic_vol_threshold:
        mode_label = "低波動放寬模式 60%" if is_low_vol_mode else "高波動嚴格模式 100%"
        print(f"🛑 [REJECT] [Filter:Volume] {sym} 量能未達標 (當前: {current_volume:.1f} <= 門檻: {dynamic_vol_threshold:.1f} | {mode_label})，判定為死水行情。")
        return False
        
    # 2. 空單 RSI 極限保護
    current_rsi = s.get("current_rsi", 50.0)
    if side == 'sell' and current_rsi < 30.0:
        print(f"🛑 [REJECT] [Filter:RSI_Limit] {sym} 觸發RSI極限保護 (RSI: {current_rsi:.1f} < 30.0)，拒絕在極端超賣區追空。")
        return False
        
    # 3. 15m 跨時框趨勢對齊 (Multi-Timeframe Alignment)
    ema20_15m = s.get("ema20_15m", 0.0)
    ema50_15m = s.get("ema50_15m", 0.0)
    if ema20_15m > 0 and ema50_15m > 0:
        if side == 'sell' and ema20_15m > ema50_15m:
            print(f"🛑 [REJECT] [Filter:MTF_Trend] {sym} 15m 大趨勢向上 (EMA20: {ema20_15m:.4f} > EMA50: {ema50_15m:.4f})，拒絕 5m 短線逆勢做空。")
            return False
        elif side == 'buy' and ema20_15m < ema50_15m:
            print(f"🛑 [REJECT] [Filter:MTF_Trend] {sym} 15m 大趨勢向下 (EMA20: {ema20_15m:.4f} < EMA50: {ema50_15m:.4f})，拒絕 5m 短線逆勢做多。")
            return False
            
    # 4. 收盤確認 (Candle Close Check)
    if len(s["ohlcv"]) >= 2:
        prev_close = s["ohlcv"][-2][4]
        open_price = s["ohlcv"][-1][1]
        close_price = s["ohlcv"][-1][4]
        if side == 'buy' and not (close_price > prev_close or close_price > open_price):
            print(f"🛑 [REJECT] [Filter:Candle_Close] {sym} 收盤未確認 (當前收盤: {close_price:.4f} <= 前收: {prev_close:.4f} 且 <= 開盤: {open_price:.4f})。")
            return False
        elif side == 'sell' and not (close_price < prev_close or close_price < open_price):
            print(f"🛑 [REJECT] [Filter:Candle_Close] {sym} 收盤未確認 (當前收盤: {close_price:.4f} >= 前收: {prev_close:.4f} 且 >= 開盤: {open_price:.4f})。")
            return False

    # [新增] MTF Correlation Lock (4H)
    upper_4h = s.get("bb_upper_4h")
    lower_4h = s.get("bb_lower_4h")
    atr = s.get("current_atr", 0.0)
    if upper_4h is not None and lower_4h is not None and atr > 0:
        if side == 'buy' and (upper_4h - cp) < atr * 0.5:
            print(f"🛑 觸發 [MTF 4H 強壓力位] {sym} 現價 {cp} 距離 4H 布林上軌 {upper_4h:.4f} 過近，禁止多單開倉防接刀")
            return False
        if side == 'sell' and (cp - lower_4h) < atr * 0.5:
            print(f"🛑 觸發 [MTF 4H 強壓力位] {sym} 現價 {cp} 距離 4H 布林下軌 {lower_4h:.4f} 過近，禁止空單開倉防地板空")
            return False

    is_trend = route == "a"
    if side == 'buy' and not MARKET_WIND.get("allow_long", True) and is_trend:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤瀑布風控] 大盤異常跌勢，禁止開多")
        return False
    if side == 'sell' and not MARKET_WIND.get("allow_short", True) and is_trend:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [大盤上漲風控] 大盤異常漲勢，禁止開空")
        return False

    # --- [BTC 1H 趨勢大盤過濾] ---
    btc_1h = MARKET_WIND.get("btc_trend_1h")
    if is_trend and btc_1h is not None:
        if side == 'buy' and btc_1h == "BEAR":
            print(f"⚠️ [BTC 1H 大盤過濾] BTC 1H 確認為熊市跌勢，但已依指示放寬，允許小幣逆勢做多")

    # --- [過熱噴發過濾 (Moving Average Deviation Filter)] ---
    if is_trend:
        ema20 = s.get("ema20", 0.0)
        if ema20 > 0:
            deviation = (cp - ema20) / ema20
            if strength <= 20.0:
                if side == "buy" and deviation > 0.08:
                    print(f"🛑 {sym} 觸發 [過熱過濾] 順勢做多但價格偏離 EMA20 已達 {deviation*100:.2f}% (> 8%)，視為過熱噴發，拒絕進場防接刀")
                    return False
                if side == "sell" and deviation < -0.08:
                    print(f"🛑 {sym} 觸發 [過熱過濾] 順勢做空但價格偏離 EMA20 已達 {abs(deviation)*100:.2f}% (> 8%)，視為過熱下挫，拒絕進場防地板空")
                    return False

    # --- [15m EMA 趨勢過濾] ---
    if is_trend:
        if strength >= 10.0:
            pass # 強勢 Override，跳過 15m EMA 過濾
        else:
            ema20_15m = s.get("ema20_15m", 0.0)
            if ema20_15m > 0:
                if side == 'buy' and cp < ema20_15m:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [15m EMA過濾] 5m 趨勢做多，但 15m EMA 向下 (現價 {cp:.4f} < 15m_EMA20 {ema20_15m:.4f})")
                    return False
                if side == 'sell' and cp > ema20_15m:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [15m EMA過濾] 5m 趨勢做空，但 15m EMA 向上 (現價 {cp:.4f} > 15m_EMA20 {ema20_15m:.4f})")
                    return False

    # --- [BTC 4H 趨勢過濾] ---
    btc_4h = MARKET_WIND.get("btc_trend_4h")  # 可能值: "BULL", "BEAR", "NEUTRAL", None
    if is_trend and btc_4h is not None:
        if side == 'buy' and btc_4h == "BEAR":
            print(f"@@COIN_DEBUG@@ ⚠️ {sym} [4H大盤過濾] BTC 4H 確認熊市，但為提高開倉頻率已放行做多")
        if side == 'sell' and btc_4h == "BULL":
            print(f"@@COIN_DEBUG@@ ⚠️ {sym} [4H大盤過濾] BTC 4H 確認牛市，但為提高開倉頻率已放行做空")

    if len(s["ohlcv"]) < 20:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [K線不足] 當前長度 {len(s['ohlcv'])} < 20")
        return False
        
    # --- MTF 1H & 15m 趨勢過濾 (強化防護) ---
    if s.get("mtf_filter", True):
        ema50_1h = s.get("ema50_1h", 0)
        sma200_15m = s.get("sma200_15m", 0)
        
        if ema50_1h > 0:
            if side == 'buy' and cp <= ema50_1h:
                print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [Filter:Trend_Mismatch] 1H大趨勢向下 (現價 {cp:.4f} <= 1H_EMA50 {ema50_1h:.4f})，禁止逆勢做多")
                return False
            # 空單 MTF 1H EMA50 過濾：Exhaustion_Entry 不受限（反轉策略）
            if side == 'sell' and route != "Exhaustion_Entry":
                if cp >= ema50_1h:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [Filter:Trend_Mismatch] 1H大趨勢向上 (現價 {cp:.4f} >= 1H_EMA50 {ema50_1h:.4f})，禁止逆勢做空")
                    return False
                if sma200_15m > 0 and cp >= sma200_15m:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [Filter:Trend_Mismatch] 15m趨勢向上 (現價 {cp:.4f} >= 15m_SMA200 {sma200_15m:.4f})，禁止逆勢做空")
                    return False
            
    # --- 盤整/低波動過濾 (Choppiness) ---
    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)
    
    # 判斷波動太小的條件：當前 ATR 小於 24H 平均 ATR 的 25%，或 BB 區間太窄
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
    if route != "Exhaustion_Entry" and strength <= 15.0 and not is_entry_volume_confirmed(sym, side):
        return False
    elif route != "Exhaustion_Entry" and strength > 15.0:
        # 強勢訊號只保留最低限度的量能要求 (5% 均量)
        if s["current_vol"] < s["vol_ma20"] * 0.05:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 強勢訊號但量能極度枯竭 (當前 {s['current_vol']:.0f} < 均量 5%)，攔截")
            return False
            
        # 加入「量能背離」過濾 (強度 15~20 適用，>20 豁免)
        if strength <= 20.0:
            if s["current_vol"] >= s["vol_ma20"] * 1.5:
                print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [量能背離過濾] 強勢訊號({strength:.1f})但當前量 ({s['current_vol']:.0f}) 過大 (>= 1.5x均量 {s['vol_ma20']*1.5:.0f})，視為趨勢延續，攔截")
                return False

        # 價格結構確認 (Price Action Confirmation)，擴大至過去 3 根 K 線
        if len(s["ohlcv"]) >= 2:
            current_close = s["ohlcv"][-1][4]
            lookback = min(3, len(s["ohlcv"]) - 1)
            past_candles = s["ohlcv"][-lookback-1:-1]
            past_highs = [c[2] for c in past_candles]
            past_lows = [c[3] for c in past_candles]
            avg_high = sum(past_highs) / len(past_highs)
            avg_low = sum(past_lows) / len(past_lows)
            
            if side == "sell":
                struct_ok = (current_close < avg_high) or (current_close < max(past_lows))
                if not struct_ok:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [結構過濾] 空單強勢({strength:.1f})但收盤價 ({current_close:.4f}) 未低於3K平均高點({avg_high:.4f})且未破任一低點({max(past_lows):.4f})，攔截")
                    return False
            if side == "buy":
                struct_ok = (current_close > avg_low) or (current_close > min(past_highs))
                if not struct_ok:
                    print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [結構過濾] 多單強勢({strength:.1f})但收盤價 ({current_close:.4f}) 未高於3K平均低點({avg_low:.4f})且未破任一高點({min(past_highs):.4f})，攔截")
                    return False

        # --- 新增：三道轉折防護機制 (High-Point Decay, RSI History, Cooldown) ---
        # 1. 同向虧損冷卻期 (Same-Side Cooldown) - 優先檢查，節省資源
        COOLDOWN_HOURS = 4
        COOLDOWN_SEC = COOLDOWN_HOURS * 3600
        now = time.time()
        
        last_loss_time = s.get("last_loss_time_short", 0) if side == "sell" else s.get("last_loss_time_long", 0)
        if now - last_loss_time < COOLDOWN_SEC:
            remaining_mins = (COOLDOWN_SEC - (now - last_loss_time)) / 60
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [同向虧損冷卻] 過去 4 小時內曾發生同向({side})虧損平倉，冷卻剩餘 {remaining_mins:.1f} 分鐘，攔截進場")
            return False

        # 2. 判斷是否為「逆勢轉折交易」 (判斷依據：與 SMA200 長線趨勢相反，或是明確的 Extreme_Reversal 路由)
        sma200_15m = s.get("sma200_15m", 0)
        is_counter_trend = False
        
        if route == "Extreme_Reversal":
            is_counter_trend = True
        else:
            if side == "sell":
                # 在 SMA200 之上做空，視為逆勢轉折
                if sma200_15m > 0 and current_close > sma200_15m:
                    is_counter_trend = True
            else: # buy
                # 在 SMA200 之下做多，視為逆勢轉折
                if sma200_15m > 0 and current_close < sma200_15m:
                    is_counter_trend = True

        # 以下兩道「嚴格空間防禦」僅針對「逆勢轉折交易」開啟
        if is_counter_trend:
            # 3. 距離高低點衰減過濾 (High-Point Decay Filter)
            if len(s["ohlcv"]) >= 20:
                past_20 = s["ohlcv"][-20:]
                highest_20 = max([c[2] for c in past_20])
                lowest_20 = min([c[3] for c in past_20])
                COUNTER_TREND_MAX_DECAY_PCT = 0.025
                
                if side == "sell":
                    decay_pct = (highest_20 - current_close) / highest_20 if highest_20 > 0 else 0
                    if decay_pct > COUNTER_TREND_MAX_DECAY_PCT:
                        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [衰減過濾] 逆勢空單強勢({strength:.1f})但現價({current_close})距離20K高點({highest_20})已跌落 {decay_pct*100:.1f}% (> 2.5%)，視為半山腰追空，攔截")
                        return False
                else:
                    decay_pct = (current_close - lowest_20) / lowest_20 if lowest_20 > 0 else 0
                    if decay_pct > COUNTER_TREND_MAX_DECAY_PCT:
                        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [衰減過濾] 逆勢多單強勢({strength:.1f})但現價({current_close})距離20K低點({lowest_20})已反彈 {decay_pct*100:.1f}% (> 2.5%)，視為半山腰追多，攔截")
                        return False

            # 4. RSI 超買/超賣歷史確認 (RSI History Confirmation)
            if "rsi_history" in s and len(s["rsi_history"]) > 0:
                recent_rsis = s["rsi_history"][-10:] # 取最近 10 根 RSI (最多 10 根)
                if side == "sell":
                    highest_rsi = max(recent_rsis)
                    if highest_rsi < 68.0:
                        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [RSI歷史確認] 逆勢空單進場前，近 10 根 RSI 最高僅 {highest_rsi:.1f} (< 68.0)，未經歷極度過熱，視為逆勢空單假突破，攔截")
                        return False
                else:
                    lowest_rsi = min(recent_rsis)
                    if lowest_rsi > 32.0:
                        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [RSI歷史確認] 逆勢多單進場前，近 10 根 RSI 最低僅 {lowest_rsi:.1f} (> 32.0)，未經歷極度超賣，視為逆勢多單假突破，攔截")
                        return False
        
    # 實盤最小量限制 (移除 1000 絕對門檻，改用動態 10% 均量)
    min_volume = s["vol_ma20"] * 0.05
    if s["current_vol"] < min_volume:
        print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [實盤最小量過濾] 當前 {s['current_vol']:.2f} < 均量 10% ({min_volume:.2f})")
        return False

    # =========================================================================
    # 🪙 STAGE 2 & 3: BONUS SYSTEM & EXECUTION THRESHOLD (加分系統與最終審查)
    # =========================================================================
    # 1. 基礎分 (Base Score)
    macd_line = s.get("macd_line", 0.0)
    macd_signal = s.get("macd_signal", 0.0)
    prev_macd_line = s.get("prev_macd_line", 0.0)
    prev_macd_signal = s.get("prev_macd_signal", 0.0)
    macd_hist = macd_line - macd_signal
    prev_macd_hist = prev_macd_line - prev_macd_signal

    macd_score = 0.0
    rsi_score = 0.0

    if side == 'buy':
        if prev_macd_line <= prev_macd_signal and macd_line > macd_signal:
            macd_score = 5.0
        elif macd_hist > prev_macd_hist:
            macd_score = 3.0
        if current_rsi > 48.0:
            rsi_score = 4.0
    else:
        if prev_macd_line >= prev_macd_signal and macd_line < macd_signal:
            macd_score = 5.0
        elif macd_hist < prev_macd_hist:
            macd_score = 3.0
        if current_rsi < 52.0:
            rsi_score = 4.0

    base_score = 7.0 + macd_score + rsi_score

    # 2. 加分項目 A (強勢訊號): signal_strength > 20.0, 給予 +5
    bonus_a = 0.0
    if strength > 20.0:
        bonus_a = 5.0

    # 3. 加分項目 B (量價協同): is_volume_price_aligned 為真, 給予 +3
    is_volume_price_aligned = False
    bonus_b = 0.0
    if len(s["ohlcv"]) >= 2:
        c_close = s["ohlcv"][-1][4]
        c_open = s["ohlcv"][-1][1]
        c_vol = s["ohlcv"][-1][5]
        p_vol = s["ohlcv"][-2][5]
        if side == 'buy':
            if c_close > c_open and c_vol > p_vol:
                is_volume_price_aligned = True
        elif side == 'sell':
            if c_close < c_open and c_vol > p_vol:
                is_volume_price_aligned = True

    if is_volume_price_aligned:
        bonus_b = 3.0

    total_score = base_score + bonus_a + bonus_b
    MIN_ENTRY_SCORE = 15.0

    if total_score < MIN_ENTRY_SCORE:
        print(f"🛑 [REJECT] {sym}: 硬條件通過，但總分未達標 (綜合得分: {total_score:.1f} < 門檻: {MIN_ENTRY_SCORE:.1f})")
        return False

    print(f"💚 [PASS] {sym}: 完美通過全套風控，准予開倉！(總得分: {total_score:.1f}, 基礎分: {base_score:.1f}, 加分A: {bonus_a:.1f}, 加分B: {bonus_b:.1f})")
    return True

def compute_signal_strength(sym):
    s = STATES[sym]
    if len(s["closes"]) < 20:
        return (None, 0, None)

    # --- 新增 C：動能/成交量過濾 ---
    # 確保當前 K 線成交量不要低得離譜 (放寬至 0.15 倍均量即可通過)
    vol_ma10 = s.get("vol_ma10", 0.0)
    current_vol = s.get("current_vol", 0.0)
    if vol_ma10 > 0 and current_vol < vol_ma10 * 0.15:
        return (None, 0, None)

    # --- 第三層防禦：極值檢查 (Extreme Value Defense) ---
    rsi = s.get("current_rsi", 50.0)
    rsi_extreme_low = s.get("rsi_extreme_low", 20)
    rsi_extreme_high = s.get("rsi_extreme_high", 75)

    if rsi < rsi_extreme_low:
        # 場景 A：防止「接跌刀」- 僅在 MACD 不確認下行趨勢時攔截
        # 若 MACD 向下，代表趨勢持續下跌，應允許空單訊號繼續生成
        macd_line_v = s.get("macd_line", 0.0)
        macd_sig_v = s.get("macd_signal", 0.0)
        macd_trending_down = (macd_line_v - macd_sig_v) < 0
        rsi_history = s.get("rsi_history", [])
        is_hooking_up = len(rsi_history) >= 2 and rsi_history[-1] > rsi_history[-2]
        # MACD 向下且 RSI 未轉折 → 可能是空單訊號，允許繼續
        # MACD 非向下且 RSI 未轉折 → 可能買多假訊號，攔截
        if not macd_trending_down and not is_hooking_up:
            print(f"@@COIN_DEBUG@@ 🛑 {sym} 觸發 [極值防禦] RSI ({rsi:.1f}) < {rsi_extreme_low} 且未見轉折向上，拒絕進場防接刀")
            return (None, 0)

    if rsi > rsi_extreme_high:
        # 場景 B：防止「追高頂點」
        # 標記高 RSI 狀態供後續放大量能門檻
        s["is_extreme_high_rsi"] = True
    else:
        s["is_extreme_high_rsi"] = False


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

    # --- 放寬：只需最後 1 根 K 線方向一致即可（原需連續2根） ---
    last_candle_long  = len(s["ohlcv"]) >= 2 and s["ohlcv"][-1][4] > s["ohlcv"][-2][4]
    last_candle_short = len(s["ohlcv"]) >= 2 and s["ohlcv"][-1][4] < s["ohlcv"][-2][4]
    # 保留原連2根判斷供加分使用
    last_two_candles_long  = len(s["ohlcv"]) >= 3 and s["ohlcv"][-1][4] > s["ohlcv"][-2][4] and s["ohlcv"][-2][4] > s["ohlcv"][-3][4]
    last_two_candles_short = len(s["ohlcv"]) >= 3 and s["ohlcv"][-1][4] < s["ohlcv"][-2][4] and s["ohlcv"][-2][4] < s["ohlcv"][-3][4]

    ema50 = s.get("ema50", 0.0)
    trend_confluence_long  = ema50 == 0.0 or close > ema50
    trend_confluence_short = ema50 == 0.0 or close < ema50

    sma200 = s.get("sma200_15m", 0)
    is_above_sma200 = sma200 > 0 and close > sma200 * 0.999
    is_below_sma200 = sma200 > 0 and close < sma200 * 1.001
    # SMA200 不明確時（值為0）：不惩罰，視為中立
    sma200_neutral   = sma200 == 0

    # 限制開倉不要太偏離短期趨勢線，避免追價開倉（放寬至 ±8%，原 ±5%）
    close_near_ema20_long  = ema20 <= 0 or close <= ema20 * 1.08
    close_near_ema20_short = ema20 <= 0 or close >= ema20 * 0.92
    is_in_bb_zone_long  = s.get("bb_low", 0) > 0 and close <= s["bb_low"] * 1.01
    is_in_bb_zone_short = s.get("bb_up",  0) > 0 and close >= s["bb_up"]  * 0.99

    # 預先計算供 Log 顯示的預估強度
    l_ts = 0; s_ts = 0
    if is_above_sma200: l_ts += 4; s_ts -= 3
    elif is_below_sma200 and not sma200_neutral: l_ts -= 3; s_ts += 4
    if trend_confluence_long and (long_macd_cross or macd_hist > 0): l_ts += 5
    if trend_confluence_short and (short_macd_cross or macd_hist < 0): s_ts += 5
    if trend_confluence_short and (long_macd_cross or macd_hist > 0): l_ts -= 5
    if trend_confluence_long and (short_macd_cross or macd_hist < 0): s_ts -= 5
    if last_two_candles_long: l_ts += 3
    if last_two_candles_short: s_ts += 3

    raw_long_str = 12.0 + ((close - ema20) / max(ema20, 1e-8) * 100) + l_ts + (5.0 if long_macd_cross else 0.0)
    raw_short_str = 12.0 + ((ema20 - close) / max(ema20, 1e-8) * 100) + s_ts + (5.0 if short_macd_cross else 0.0)
    if rsi >= 80.0: raw_short_str = 15.0 + ((rsi - 80.0) / 2.0)
    if rsi <= 20.0: raw_long_str = 15.0 + ((20.0 - rsi) / 2.0)

    print(f"@@COIN_DEBUG@@ 🔍 {sym} 條件檢測 | 預估強度(L/S): {raw_long_str:.1f}/{raw_short_str:.1f} | RSI動能(L>48/S<52): {rsi > 48.0}/{rsi < 52.0} | SMA200長線(L/S): {is_above_sma200}/{is_below_sma200} | MACD多頭/空頭: {macd_hist > 0}/{macd_hist < 0} | 收盤價確認(L/S): {last_candle_long}/{last_candle_short} | 連2根(L/S): {last_two_candles_long}/{last_two_candles_short} | EMA20距離(L/S): {close_near_ema20_long}/{close_near_ema20_short} | BB區(L/S): {is_in_bb_zone_long}/{is_in_bb_zone_short} | EMA50確認(L/S): {trend_confluence_long}/{trend_confluence_short}")

    # 💥 極端反轉路線 (Extreme Reversal)
    # 當市場極端超買/超賣時，鎖定反向開倉，並給予極高強度交由 is_entry_allowed 結構過濾把關
    if rsi >= 80.0:
        strength = 15.0 + ((rsi - 80.0) / 2.0)  # RSI 80->15, 85->17.5, 90->20
        return ("sell", strength, "Extreme_Reversal")
        
    if rsi <= 20.0:
        strength = 15.0 + ((20.0 - rsi) / 2.0)
        return ("buy", strength, "Extreme_Reversal")

    # 放寬 RSI 門檻（做多 > 32，做空 < 68，且 MACD 確認時再放寬至 25/75）
    # 修改：限制順勢策略不要在極端超買區追多 (RSI < 75)，不要在極端超賣區追空 (RSI > 25)
    rsi_ok_long  = rsi < 75.0 and (rsi > 32.0 or (rsi >= 25.0 and (long_macd_cross  or macd_hist > 0)))
    rsi_ok_short = rsi > 25.0 and (rsi < 68.0 or (rsi <= 75.0 and (short_macd_cross or macd_hist < 0)))

    # --- 加分機制：SMA200 加分、連2根K線加分、EMA50 順向加分 ---
    long_trend_score = 0
    short_trend_score = 0
    
    # SMA200 順向 +4 / 逆向 -3（不再是硬性攔截）
    if is_above_sma200:
        long_trend_score += 4
        short_trend_score -= 3
    elif is_below_sma200 and not sma200_neutral:
        long_trend_score -= 3
        short_trend_score += 4
        
    # EMA50 順向加分
    if trend_confluence_long and (long_macd_cross or macd_hist > 0):
        long_trend_score += 5
    if trend_confluence_short and (short_macd_cross or macd_hist < 0):
        short_trend_score += 5
        
    # 逆勢扣分
    if trend_confluence_short and (long_macd_cross or macd_hist > 0):
        long_trend_score -= 5
    if trend_confluence_long and (short_macd_cross or macd_hist < 0):
        short_trend_score -= 5
        
    # 連2根同向加分
    if last_two_candles_long:
        long_trend_score += 3
    if last_two_candles_short:
        short_trend_score += 3

    # Route A (Trend Following): SMA200 改為加分而非硬性要求
    route_a_long = (
        (long_macd_cross or macd_hist > 0) and
        last_candle_long and                      # 放寬：只需1根
        rsi_ok_long and
        close_near_ema20_long
    )

    route_a_short = (
        (short_macd_cross or macd_hist < 0) and
        last_candle_short and                     # 放寬：只需1根
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
        strength += long_trend_score
        return ("buy", strength if strength >= 12.0 else 0.0, route)

    if short_base_ok:
        route = "a"
        strength = 12.0 + ((ema20 - close) / max(ema20, 1e-8) * 100)
        if short_macd_cross:
            strength += 5.0
        strength += short_trend_score
        return ("sell", strength if strength >= 12.0 else 0.0, route)

    # --- Route C: 量能衰竭進場策略 (Exhaustion Entry) ---
    # 專門抓大趨勢回檔時的「價跌量縮」潛在底部
    if len(s["ohlcv"]) >= 50:
        c1 = s["ohlcv"][-2]  # 最新已收盤 (驗證K線)
        c2 = s["ohlcv"][-3]  # 前一根已收盤 (縮量衰竭K線)
        
        # 4. 波動率過濾 (Volatility Filter)
        # 避免接刀：如果是極端瀑布行情 (ATR大於2倍均值)，禁止進場
        current_atr = s.get("current_atr", 0.0)
        atr_history = s.get("atr_history", [])
        atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
        if atr_24h_avg > 0 and current_atr > atr_24h_avg * 2.0:
            return (None, 0, None)

        c2_vol_low = c2[5] < s.get("vol_ma20", 1) * 0.65
        
        # 1. 位置過濾 (Location Awareness)
        recent_low_50 = min([x[3] for x in s["ohlcv"][-50:]])
        recent_high_50 = max([x[2] for x in s["ohlcv"][-50:]])
        sma200 = s.get("sma200_15m", 0)
        
        # 多單：抓回檔底部
        if c2[4] < c2[1] and c2_vol_low:  # c2 價跌且量縮
            bb_low = s.get("bb_low", 0)
            # 必須是在真正的底部：低於 BB 下軌，或是非常靠近 SMA200 / 近期低點 (差距小於 0.5%)
            is_near_sma = (sma200 > 0) and (abs(c1[3] - sma200) / sma200 < 0.005)
            is_near_low = (recent_low_50 > 0) and (c1[3] <= recent_low_50 * 1.005)
            support_ok = (bb_low > 0 and c1[3] <= bb_low * 1.005) or is_near_sma or is_near_low
            
            # 2. 價格結構確認 (Price Action)
            # 收盤價回升且有下影線 (Hammer)
            price_rebound = c1[4] > c2[4]
            has_lower_wick = (min(c1[1], c1[4]) - c1[3]) > abs(c1[4] - c1[1]) * 0.5
            pa_ok = price_rebound and has_lower_wick
            bounce_ok = (c1[4] > c1[1]) and (c1[5] > c2[5] * 1.2)
            
            trend_ok = True
            
            if trend_ok and support_ok and (pa_ok or bounce_ok):
                print(f"🌟 [量能衰竭] {sym} 觸發多單低接條件！(Support:{support_ok}, PA:{pa_ok}, Bounce:{bounce_ok})")
                return ("buy", 15.0, "Exhaustion_Entry")
                
        # 空單：抓反彈頂部
        if c2[4] > c2[1] and c2_vol_low:  # c2 價漲且量縮
            bb_up = s.get("bb_up", 0)
            is_near_sma_res = (sma200 > 0) and (abs(c1[2] - sma200) / sma200 < 0.005)
            is_near_high = (recent_high_50 > 0) and (c1[2] >= recent_high_50 * 0.995)
            resistance_ok = (bb_up > 0 and c1[2] >= bb_up * 0.995) or is_near_sma_res or is_near_high
            
            # 2. 價格結構確認 (Price Action)
            # 收盤價回落且有上影線 (Shooting Star)
            price_rebound = c1[4] < c2[4]
            has_upper_wick = (c1[2] - max(c1[1], c1[4])) > abs(c1[4] - c1[1]) * 0.5
            pa_ok = price_rebound and has_upper_wick
            bounce_ok = (c1[4] < c1[1]) and (c1[5] > c2[5] * 1.2)
            
            trend_ok = True
            
            if trend_ok and resistance_ok and (pa_ok or bounce_ok):
                print(f"🌟 [量能衰竭] {sym} 觸發空單高空條件！(Resistance:{resistance_ok}, PA:{pa_ok}, Bounce:{bounce_ok})")
                return ("sell", 15.0, "Exhaustion_Entry")

    # 所有路線均不符合，無訊號
    return (None, 0, None)

async def is_reversal_still_valid(sym, pending_side):
    s = STATES.get(sym)
    if not s or not s.get("ohlcv") or len(s["ohlcv"]) < 2:
        return False
    
    current_price = s["close_price"]
    prev_candle = s["ohlcv"][-2]
    prev_close = prev_candle[4]
    
    # 做多反手：現價不能低於訊號 K 線收盤價的 0.5% (防止暴跌接刀)
    if pending_side == "buy":
        if current_price < prev_close * 0.995:
            print(f"📉 [Reversal_Invalid] {sym} 做多反手失效：現價 {current_price} 低於訊訊號收盤價 {prev_close} 超過 0.5% (防止接刀)")
            return False
    # 做空反手：現價不能高於訊號 K 線收盤價的 0.5% (防止地板空)
    elif pending_side == "sell":
        if current_price > prev_close * 1.005:
            print(f"📈 [Reversal_Invalid] {sym} 做空反手失效：現價 {current_price} 高於訊訊號收盤價 {prev_close} 超過 0.5% (防止地板空)")
            return False
            
    return True

async def is_eligible_for_reverse(sym, current_strength):
    s = STATES.get(sym)
    if not s or s.get("is_banned"):
        return False
    
    # 強度門檻 (>= 12.0)
    if current_strength < 12.0:
        return False
    
    # 冷卻時間門檻 (30 分鐘 = 1800 秒)
    last_exit = s.get("last_exit_time", 0)
    if (time.time() - last_exit) < 1800:
        print(f"⏳ [REVERSE_DENIED] {sym} 尚未過冷卻期 (距離上次平倉不足 30 分鐘)")
        return False
        
    return True


def get_dynamic_cooldown(current_atr, avg_atr, adx_value, base_cooldown=15):
    volatility_ratio = current_atr / avg_atr if avg_atr > 0 else 1.0
    vol_factor = 1.0 + (max(0, volatility_ratio - 1.0) * 0.5)

    if adx_value > 30:
        trend_factor = 0.8
    elif adx_value < 20:
        trend_factor = 1.5
    else:
        trend_factor = 1.0

    dynamic_cooldown = base_cooldown * vol_factor * trend_factor
    return max(5, min(60, round(dynamic_cooldown)))

def check_pyramiding_eligibility(s):
    if not s.get('entries'):
        return False, 0

    last_entry = s['entries'][-1]
    last_entry_time = last_entry['time']
    
    current_atr = s.get('current_atr', 0.0)
    avg_atr = s.get('atr_ma20', current_atr)
    adx_value = s.get('adx', 25.0)

    dynamic_cooldown_mins = get_dynamic_cooldown(current_atr, avg_atr, adx_value)
    
    current_time = time.time()
    seconds_passed = current_time - last_entry_time
    minutes_passed = seconds_passed / 60
    
    is_cooldown_over = minutes_passed >= dynamic_cooldown_mins
    is_under_max_layers = len(s['entries']) < 3
    
    if is_cooldown_over and is_under_max_layers:
        price_gap = abs(s['close_price'] - s.get('avg_price', s['close_price'])) / s.get('avg_price', s['close_price'])
        if price_gap < 0.05:
            return True, dynamic_cooldown_mins
            
    return False, dynamic_cooldown_mins

async def check_entries():
    open_count = get_open_position_count()
    remaining_slots = MAX_POSITIONS - open_count

    candidates = []
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        
        # --- 自動反手快速通道 ---
        pending_rev = s.get("pending_reverse")
        if pending_rev:
            if time.time() - s.get("pending_reverse_time", 0) < 300: # 5 分鐘內有效
                if not s.get("is_ordering"):
                    print(f"🔄 [自動反手執行] {sym} 偵測到反手訊號 ({pending_rev})，開始建倉！")
                    price = s["close_price"]
                    s["pending_reverse"] = None
                    s["is_ordering"] = True
                    
                    async def _rev_task(sym, pending_rev, price):
                        try:
                            await execute_order(sym, pending_rev, price)
                        finally:
                            STATES[sym]["is_ordering"] = False
                            await load_open_positions()
                            
                    asyncio.create_task(_rev_task(sym, pending_rev, price))
                continue
            else:
                s["pending_reverse"] = None

        if s["status"] != "ACTIVE":
            continue
            
        has_position = abs(s["qty"]) > 0.000001
        current_direction = "buy" if s["qty"] > 0 else "sell" if s["qty"] < 0 else None
        
        # 開倉數限制 (針對新開倉)
        if not has_position and open_count >= MAX_POSITIONS:
            continue

        current_candle_time = s["ohlcv"][-1][0] if s["ohlcv"] else 0

        # --- [新增] 自動反手訊號緩衝與 K 線收盤確認機制 ---
        if s.get("pending_reverse_trigger"):
            pending_rev_data = s["pending_reverse_trigger"]
            if current_candle_time > pending_rev_data.get("time", 0):
                print(f"⏳ [{sym}] 進入新 K 線，驗證自動反手趨勢持續性...")
                if await is_reversal_still_valid(sym, pending_rev_data["side"]):
                    print(f"⚡ [{sym}] [Reversal_Confirmed] 反手趨勢確認！執行平倉並反手建倉。")
                    # 1. 平倉舊倉位
                    await close_position(sym, current_direction, abs(s["qty"]), s["close_price"], s["avg_price"], reason="[AUTOMATIC_REVERSE]")
                    await asyncio.sleep(1)
                    reset_coin_state(sym)
                    # 2. 反手建倉
                    await execute_order(sym, pending_rev_data["side"], s["close_price"])
                else:
                    print(f"❌ [{sym}] [Reversal_Cancelled] 觀察期間趨勢失效，取消反手，保留原倉位。")
                
                s["pending_reverse_trigger"] = None
                continue
            else:
                # 還在同一根 K 線，繼續觀察
                continue

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
                    # [Layer 3] 嚴格K線：放寬容忍度至實體的 150%
                    body = prev_close - prev_open
                    upper_shadow = prev_candle[2] - prev_close
                    if body > 0 and upper_shadow < body * 1.5:
                        is_valid = True
                elif s["pending_side"] == "sell":
                    # [Layer 3] 嚴格K線：放寬容忍度至實體的 150%
                    body = prev_open - prev_close
                    lower_shadow = prev_close - prev_candle[3]
                    if body > 0 and lower_shadow < body * 1.5:
                        is_valid = True
                        
                if is_valid:
                    # [新增] Second-Bar Confirmation
                    current_price = s["close_price"]
                    trigger_high = prev_candle[2]
                    trigger_low = prev_candle[3]
                    
                    if s["pending_side"] == "buy" and current_price < trigger_high * 0.98:
                        print(f"❌ [防二次誘騙] {sym} 第二根 K 線現價 {current_price} 未能維持在觸發 K 線高點 {trigger_high} 的 98% ({trigger_high*0.98:.4f}) 以上，疑似插針假突破，取消多單。")
                        is_valid = False
                    elif s["pending_side"] == "sell" and current_price > trigger_low * 1.02:
                        print(f"❌ [防二次誘騙] {sym} 第二根 K 線現價 {current_price} 未能維持在觸發 K 線低點 {trigger_low} 的 102% ({trigger_low*1.02:.4f}) 以下，疑似插針假跌破，取消空單。")
                        is_valid = False

                if is_valid:
                    print(f"✅ [訊號確認] {sym} {s['pending_side']} 訊號已確認 (K線收盤無反轉且通過防二次誘騙)")
                    side = s["pending_side"]
                    strength = s.get("pending_strength", 5.0)
                    route = s.get("pending_route", "confirmed")
                    s["pending_side"] = None
                    
                    p = s["close_price"]
                    atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)
                    sl_multiplier_raw = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), side == "buy")
                    tp_multiplier = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), side == "buy")
                    
                    # 套用相同的動態倍數邏輯
                    sl_multiplier = get_dynamic_atr_multiplier(sym, sl_multiplier_raw)
                    
                    sl_dist = max(atr_val * sl_multiplier, p * 0.005)
                    tp_dist = max(atr_val * tp_multiplier, p * 0.015)

                    expected_rr = tp_dist / sl_dist if sl_dist > 0 else 0
                    min_rr = s.get("min_rr", 1.0)
                    if expected_rr < min_rr:
                        print(f"🛑 [Filter:RiskReward] {sym} 預期盈虧比太差 ({expected_rr:.2f} < {min_rr:.1f})，放棄進場")
                        continue
                        
                    expected_profit_pct = tp_dist / p
                    min_profit_pct = s.get("min_profit_pct", 0.0)
                    if expected_profit_pct < min_profit_pct:
                        print(f"🛑 [Filter:MinProfit] {sym} 預期獲利空間過小 ({expected_profit_pct*100:.2f}% < {min_profit_pct*100:.2f}%)，利潤無法覆蓋手續費與摩擦成本，拒絕進場")
                        continue
                        
                    # 再測一次大環境 (MTF & RR)，因為換線了可能改變
                    if s.get("mtf_filter", True):
                        if strength > 15.0:
                            print(f"🚀 [強勢訊號 Override] {sym} 強度 {strength:.2f} 極高，跳過 MTF 趨勢過濾直接允許進場")
                        else:
                            ema50_1h = s.get("ema50_1h", 0.0)
                            if ema50_1h > 0:
                                if side == "buy" and p < ema50_1h:
                                    print(f"📉 [1H 過濾] {sym} 確認階段：1H 趨勢向下，捨棄訊號")
                                    continue
                                if side == "sell" and p > ema50_1h:
                                    print(f"📈 [1H 過濾] {sym} 確認階段：1H 趨勢向上，捨棄訊號")
                                    continue

                    atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)
                    sl_multiplier_raw = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), side == "buy")
                    tp_multiplier = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), side == "buy")
                    
                    # 套用相同的動態倍數邏輯
                    sl_multiplier = get_dynamic_atr_multiplier(sym, sl_multiplier_raw)
                    
                    sl_dist = max(atr_val * sl_multiplier, p * 0.005)
                    tp_dist = max(atr_val * tp_multiplier, p * 0.015)
                    
                    expected_rr = tp_dist / sl_dist if sl_dist > 0 else 0
                    base_rr_thresh = COIN_PROFILE_CONFIG.get(sym, {}).get("rr_threshold", 1.3)
                    # 如果訊號強度極高 (> 20.0)，允許 RR 降到 1.1；(> 15.0) 降到 1.2，否則維持原本的 base_rr_thresh
                    rr_thresh = 1.1 if strength > 20.0 else (1.2 if strength > 15.0 else base_rr_thresh)
                    
                    if expected_rr < rr_thresh:
                        print(f"🛑 [Filter:RR_Low] {sym} 預期盈虧比 {expected_rr:.2f} < {rr_thresh}，放棄")
                        continue
                        
                    expected_profit_pct = tp_dist / p if p > 0 else 0
                    if expected_profit_pct < 0.005:  # Minimum 0.5% profit buffer
                        print(f"⚠️ [獲利空間過濾] {sym} 預期潛在利潤過小 ({expected_profit_pct*100:.2f}% < 0.5%)，無法覆蓋手續費與滑點，拒絕進場")
                        continue
                        
                    # [Layer 4] 動態空間過濾 (Adaptive Space Check)
                    # 強勢趨勢 (MACD擴張 + RSI強勢) -> 0.15x SL
                    # 盤整區間 (ATR低 + 橫盤) -> 0.5x SL
                    # 預設 -> 0.2x SL
                    
                    macd_hist = s.get("macd_hist", 0.0)
                    prev_macd_hist = s.get("prev_macd_hist", 0.0)
                    rsi = s.get("current_rsi", 50.0)
                    current_atr = s.get("current_atr", 0.0)
                    atr_ma20 = s.get("atr_ma20", 0.0)
                    recent_candles = s.get("ohlcv", [])
                    if len(recent_candles) >= 20:
                        highs = np.array([x[2] for x in recent_candles])
                        lows = np.array([x[3] for x in recent_candles])
                        range_width_pct = (np.max(highs) - np.min(lows)) / np.min(lows)
                    else:
                        range_width_pct = 1.0

                    space_multiplier = 0.2
                    
                    # 判斷強勢趨勢 (動能擴張且突破)
                    is_strong_trend = abs(macd_hist) > abs(prev_macd_hist) and (
                        (side == "buy" and rsi > 60.0) or (side == "sell" and rsi < 40.0)
                    )
                    
                    # 判斷盤整區間
                    is_consolidation = (atr_ma20 > 0 and current_atr < atr_ma20 * 0.8) and range_width_pct < 0.02
                    
                    if is_strong_trend or route == "Automatic_Reverse":
                        space_multiplier = 0.0  # 強勢突破或反手時，完全不看空間（允許追價）
                    elif is_consolidation:
                        space_multiplier = 0.5
                    
                    if not is_strong_trend:  # 只有非強勢突破時，才受到空間過濾限制
                        if side == "buy" and s.get("bb_up", 0) > 0 and p < s.get("bb_up", 0):
                            space = s["bb_up"] - p
                            if space < sl_dist * space_multiplier:
                                print(f"⚠️ [動態空間過濾] {sym} 做多距布林上軌僅 {space:.4f} < {space_multiplier}*SL({sl_dist * space_multiplier:.4f})，拒絕進場")
                                continue
                        if side == "sell" and s.get("bb_low", 0) > 0 and p > s.get("bb_low", 0):
                            space = p - s["bb_low"]
                            if space < sl_dist * space_multiplier:
                                print(f"⚠️ [動態空間過濾] {sym} 做空距布林下軌僅 {space:.4f} < {space_multiplier}*SL({sl_dist * space_multiplier:.4f})，拒絕進場")
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
        if side_strength is None or side_strength[0] is None:
            continue
        side, strength, route = side_strength
        
        # [Layer 1] 大盤過濾 (4H BTC Trend) - 已根據使用者要求關閉，讓小幣能走出獨立行情
        # if side == "buy" and MARKET_WIND.get("btc_trend_4h") != "BULL":
        #     print(f"🛑 [大盤過濾] {sym} 訊號為多，但 BTC 4H 趨勢為 {MARKET_WIND.get('btc_trend_4h')}，禁止做多！")
        #     continue
        # if side == "sell" and MARKET_WIND.get("btc_trend_4h") != "BEAR":
        #     print(f"🛑 [大盤過濾] {sym} 訊號為空，但 BTC 4H 趨勢為 {MARKET_WIND.get('btc_trend_4h')}，禁止做空！")
        #     continue
            
        # --- 2. 多重共振過濾區塊 (Multi-Confluence Entry Filter) ---
        cp = s["close_price"]
        ema50_1h = s.get("ema50_1h", 0)
        sma200_15m = s.get("sma200_15m", 0)
        rsi = s.get("current_rsi", 50)
        macd_hist = s.get("macd_hist", 0.0)
        vol_ma20 = s.get("vol_ma20", 0.0)
        volume = s["ohlcv"][-1][5] if len(s["ohlcv"]) > 0 else 0

        # A. 數據完整性檢查 (防止啟動初期報錯)
        if sma200_15m == 0 or vol_ma20 == 0:
            continue

        # Exhaustion_Entry 與 Extreme_Reversal 是反轉策略，不受一般動能與 RSI 限制
        if route not in ["Exhaustion_Entry", "Extreme_Reversal"]:
            # --- 趨勢過濾已由 compute_signal_strength 的 trend_score 扣分機制取代 ---
            # 這裡移除 SMA200/EMA50 的硬性攔截，讓分數(強度)決定一切
            
            # C. 動能共振過濾 (Momentum Confluence) - 已放寬
            if side == "buy":
                # 做多要求放寬：RSI > 30 (原本 35) 且 MACD 柱狀圖為正
                if rsi <= 30 or macd_hist <= 0:
                    print(f"🛑 [CONFLUENCE_FAIL] {sym}: 動能不共振 (RSI {rsi:.1f} <= 30 或 MACD {macd_hist:.6f} <= 0)")
                    continue
            else: # sell
                # 做空要求放寬：RSI < 70 (原本 65) 且 MACD 柱狀圖為負
                if rsi >= 70 or macd_hist >= 0:
                    print(f"🛑 [CONFLUENCE_FAIL] {sym}: 動能不共振 (RSI {rsi:.1f} >= 70 或 MACD {macd_hist:.6f} >= 0)")
                    continue

        # D. 真實性驗證 (Volume Confirmation) - 動態門檻
        _atr_hist_ce = s.get("atr_history", [])
        _atr_avg_ce = float(np.mean(_atr_hist_ce)) if len(_atr_hist_ce) > 0 else 0.0
        _atr_cur_ce = s.get("current_atr", 0.0)
        _is_low_vol_ce = (_atr_avg_ce > 0 and _atr_cur_ce <= _atr_avg_ce)
        _d_multiplier = 0.15 if _is_low_vol_ce else 0.2
        if volume < (vol_ma20 * _d_multiplier):
            print(f"🛑 [CONFLUENCE_FAIL] {sym}: 量能不足 (當前量 {volume:.0f} < 均量 {vol_ma20:.0f} * {_d_multiplier})")
            continue

        # E. 參與度過濾 (Participation Filter)
        if len(s["ohlcv"]) > 1:
            current_vol = volume
            prev_vol = s["ohlcv"][-2][5]
            price_change = cp - s["ohlcv"][-1][1]
            
            # 1. RVOL 檢查 (爆發力) - 動態門檻
            _rvol_multiplier = 0.15 if _is_low_vol_ce else 0.3
            rvol_check = current_vol > (vol_ma20 * _rvol_multiplier)
            
            # 2. 流動性底線 (估算 24H 交易額 > 1,000,000 USD)
            # 以 5 分鐘 K 線為例，一天有 288 根 K 線，用 vol_ma20 * cp * 288 粗估
            h24_quote_volume_est = vol_ma20 * cp * 288
            liquidity_check = h24_quote_volume_est > 1000000
            
            # 3. 量價協同 (真實性)
            volume_price_sync = False
            if side == "buy" and price_change > 0 and current_vol > prev_vol:
                volume_price_sync = True
            elif side == "sell" and price_change < 0 and current_vol > prev_vol:
                volume_price_sync = True
                
            if route != "Exhaustion_Entry":
                if not liquidity_check:
                    print(f"🛑 [LOW_PARTICIPATION] {sym} 被攔截：流動性不足 (估算24H交易額: {h24_quote_volume_est:,.0f} < 1,000,000)")
                    continue
                if not rvol_check:
                    _rvol_pct = int(_rvol_multiplier * 100)
                    print(f"🛑 [LOW_PARTICIPATION] {sym} 被攔截：量能爆發不足 (目前 {current_vol:.0f} 未達均量 {_rvol_pct}% | {'低波動放寬' if _is_low_vol_ce else '高波動嚴格'})")
                    continue
                if not volume_price_sync:
                    print(f"⚠️ [LOW_PARTICIPATION] {sym} 量價不協同 (價格變動: {price_change:.6f}, 大於前量: {current_vol > prev_vol})，但已放寬不攔截")

        # F. 極端區域防禦 (Extreme Zone Defense)
        # 強勢訊號 (strength > 15) 可突破極端 RSI 限制，捕捉極端行情反轉
        if route != "Exhaustion_Entry" and strength <= 15.0:
            if side == "buy" and rsi > 80:
                print(f"🛑 [EXTREME_ZONE_FAIL] {sym} 被攔截：RSI {rsi:.1f} 極端超買，拒絕追高做多")
                continue
            if side == "sell" and rsi < 25:
                print(f"🛑 [EXTREME_ZONE_FAIL] {sym} 被攔截：RSI {rsi:.1f} 極端超賣，拒絕殺低做空")
                continue
        elif route != "Exhaustion_Entry" and strength > 15.0:
            # 強勢訊號仍保留最極端的保護層 (超買 >88, 超賣 <12)
            if side == "buy" and rsi > 88:
                print(f"🛑 [EXTREME_ZONE_FAIL] {sym} 強勢訊號仍被攔截：RSI {rsi:.1f} 極端超買頂部")
                continue
            if side == "sell" and rsi < 12:
                print(f"🛑 [EXTREME_ZONE_FAIL] {sym} 強勢訊號仍被攔截：RSI {rsi:.1f} 極端超賣底部")
                continue

        print(f"✅ [CONFLUENCE_PASS] {sym}: {side} 四重防禦過濾皆通過！(Route: {route})")
        
        # --- 方向鎖定 (Direction Lock) 與 高門檻自動反手 ---
        if has_position:
            if side != current_direction:
                if await is_eligible_for_reverse(sym, strength):
                    if not s.get("pending_reverse_trigger"):
                        s["pending_reverse_trigger"] = {
                            "side": side,
                            "time": current_candle_time,
                            "strength": strength
                        }
                        print(f"⚡ [{sym}] [Pending_Reversal_Detected] 偵測到強勢反轉訊號 (強度: {strength:.2f})，進入觀察期，等待 K 線收盤確認...")
                    continue
                else:
                    continue
            else:
                # 金字塔加倉邏輯 (順勢加碼)
                is_eligible, cooldown_mins = check_pyramiding_eligibility(s)
                if not is_eligible:
                    print(f"⏳ [加碼防禦] {sym} 欲順勢加倉 {side}，但未達動態冷卻 ({cooldown_mins}m) 或已達上限，攔截加碼")
                    continue

        if not is_entry_allowed(sym, side, route, strength):
            continue

        # --- 反手冷卻時間 (min_flip_time) 過濾 ---
        last_trade_side = s.get("last_trade_side", "")
        if last_trade_side != "" and side != last_trade_side and route != "Automatic_Reverse":
            flip_elapsed = time.time() - s.get("last_trade_time", 0)
            # 動態冷卻：如果上次是停損出場，代表趨勢已逆轉，允許更快的反手 (縮短為 60 秒)
            last_exit = s.get("last_exit_reason", "")
            is_stop_loss = "Stop" in last_exit or "Loss" in last_exit or "Trailing" in last_exit or "Momentum_Fade" in last_exit
            
            if is_stop_loss:
                min_flip = 60
            else:
                # 前一單是獲利出場 (Take Profit)
                # 使用者要求：將冷卻時間從 2 小時縮短為 30 分鐘 (1800秒)
                min_flip = 1800
            
            if flip_elapsed < min_flip:
                print(f"⏳ [Filter:Cooldown] [獲利防反手] {sym} 欲 {side}，但距離上次做 {last_trade_side} 僅 {flip_elapsed:.0f}s (獲利後需冷卻 {min_flip}s)，保護利潤不接刀！")
                continue

        # --- 同價位防雙巴鎖 (Price Zone Lock) ---
        p = s["close_price"]
        last_entry_price = s.get("last_entry_price", 0.0)
        last_entry_dir = s.get("last_entry_direction", "")
        if last_entry_price > 0 and last_entry_dir != "" and route != "Automatic_Reverse":
            price_diff_pct = abs(p - last_entry_price) / last_entry_price
            if price_diff_pct < 0.003 and side != last_entry_dir:
                print(f"🛑 [Filter:Choppiness] {sym} 欲 {side}，但現價 {p:.4f} 距離上次進場價 {last_entry_price:.4f} 誤差小於 0.3%，陷入原地盤整，拒絕雙巴被洗！")
                continue

        # --- 動能背離過濾 (Divergence Filter) ---
        divergence_type = s.get("divergence", "none")
        if route == "Automatic_Reverse":
            if (side == "buy" and divergence_type == "bullish") or (side == "sell" and divergence_type == "bearish"):
                strength *= 1.5
                print(f"🌟 [Divergence_Boost] {sym} 偵測到強烈背離，權重提升至 {strength:.2f}")
            else:
                strength *= 0.9
        else:
            if divergence_type == "bearish" and side == "buy":
                print(f"🛑 [Filter:Divergence_Block] {sym} 趨勢多單偵測到看跌背離 (頂背離)，防範接刀追高！")
                continue
            if divergence_type == "bullish" and side == "sell":
                print(f"🛑 [Filter:Divergence_Block] {sym} 趨勢空單偵測到看漲背離 (底背離)，防範地板空！")
                continue

        # --- 1H 多重時間週期 (Multi-Timeframe) 過濾 ---
        if s.get("mtf_filter", True):
            if strength > 15.0 or route == "Automatic_Reverse":
                print(f"🚀 [強勢訊號 Override] {sym} 強度 {strength:.2f} 極高或來自反手，跳過 MTF 趨勢過濾直接允許進場")
            else:
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
        sl_multiplier_raw = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), side == "buy")
        tp_multiplier = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), side == "buy")
        
        sl_multiplier = get_dynamic_atr_multiplier(sym, sl_multiplier_raw)

        sl_dist = max(atr_val * sl_multiplier, p * 0.005)
        tp_dist = max(atr_val * tp_multiplier, p * 0.015)

        expected_rr = tp_dist / sl_dist if sl_dist > 0 else 0
        base_rr_thresh = s.get("min_rr", 1.3)
        
        # 【第二步修改：放寬 RR 門檻】
        # 如果訊號強度極高 (> 20.0)，允許 RR 降到 1.1；(> 15.0) 降到 1.2，否則維持原本的 base_rr_thresh
        rr_thresh = 1.1 if strength > 20.0 else (1.2 if strength > 15.0 else base_rr_thresh)
        # 不過，如果設定了非常嚴格的 min_rr (>= 2.0)，就不隨便放寬
        if base_rr_thresh >= 2.0:
            rr_thresh = base_rr_thresh
        
        if route != "Automatic_Reverse" and expected_rr < rr_thresh:
            print(f"🛑 [Filter:RR_Low] {sym} 預期盈虧比 {expected_rr:.2f} < {rr_thresh}，放棄暫存")
            continue
            
        expected_profit_pct = tp_dist / p if p > 0 else 0
        min_profit_pct = s.get("min_profit_pct", 0.005)
        if expected_profit_pct < min_profit_pct:  # Minimum profit buffer
            print(f"⚠️ [獲利空間過濾] {sym} 預期潛在利潤過小 ({expected_profit_pct*100:.2f}% < {min_profit_pct*100:.2f}%)，無法覆蓋手續費與滑點，放棄暫存")
            continue

        # --- Flip Buffer: 防止快速反手 (在寫入 pending 之前判斷) ---
        # 修復: 使用 last_entry_time (time.time() 秒級) 比較，而非 K 線時間戳 (ms)
        last_entry_time = s.get("last_entry_time", 0.0)
        if route != "Automatic_Reverse" and last_entry_time > 0 and (time.time() - last_entry_time) < 300:
            print(f"⏳ [Flip Buffer] {sym} 訊號 {side} 被攔截 (距離上次開倉僅 {time.time() - last_entry_time:.0f}s)")
            continue

        # 通過 Flip Buffer，進入 pending 狀態等待下一根 K 線確認
        s["pending_side"] = side
        s["pending_time"] = current_candle_time
        s["pending_strength"] = strength
        s["pending_route"] = route

        print(f"⏳ [等待確認] {sym} 產生 {side} 訊號 ({route})，等待目前 K 線收盤確認...")

    if not candidates:
        return

    candidates.sort(key=lambda x: -x[2])
    print(f"📊 [訊號排行] {' | '.join(f'{sym}:{side}({strength:.2f})' for sym, side, strength, _ in candidates[:3])}")

    # 計算當前批次的總權重
    total_weight = sum(strength for _, _, strength, _ in candidates)

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
            
        if not s.get("is_ordering"):
            s["is_ordering"] = True
            
            # --- 動態權重分配 (Dynamic Position Sizing) ---
            raw_ratio = strength / total_weight if total_weight > 0 else 1.0
            allocation_pct = min(raw_ratio, 0.6) # 最高封頂 60%
            
            weight_label = f"{allocation_pct*100:.1f}%"
            print(f"⚖️ [Allocation_Ratio] {sym} 強度 {strength:.1f} (原始佔比 {raw_ratio*100:.1f}%)，實際分配資金封頂為: {weight_label}")
            
            async def _entry_task(sym, side, price, alloc_pct):
                try:
                    await execute_order(sym, side, price, alloc_pct)
                finally:
                    STATES[sym]["is_ordering"] = False
            
            asyncio.create_task(_entry_task(sym, side, s["close_price"], allocation_pct))
            
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

async def handle_trading_error(sym):
    """
    處理交易邏輯中的異常：
    1. 增加錯誤計數
    2. 達到閾值時封鎖 (Ban)
    3. 標記為需要校準 (Sync)
    """
    s = STATES.get(sym)
    if not s: return

    s["error_strikes"] = s.get("error_strikes", 0) + 1
    print(f"⚠️ [ERROR_STRIKE] {sym} 發生第 {s['error_strikes']} 次異常")

    if s["error_strikes"] >= 3:
        s["is_banned"] = True
        print(f"🚫 [BANNED] {sym} 因連續報錯被封鎖，將停止監控。")
    
    s["sync_required"] = True
    # 重置當前幣種的暫時性持倉狀態，防止數據污染
    reset_coin_state(sym)

async def safe_execute(func, sym, *args):
    """
    安全護盾：隔離單幣種錯誤，確保一個幣種崩潰不會影響全域
    """
    s = STATES.get(sym)
    if not s or s.get("is_banned"):
        return None

    try:
        if inspect.iscoroutinefunction(func):
            return await func(sym, *args)
        else:
            return func(sym, *args)
    except Exception as e:
        print(f"🚨 [SAFE_SHIELD] {sym} 發生異常在 {func.__name__}: {e}")
        await handle_trading_error(sym)
        return None

async def calibrate_with_exchange(exchange):
    """
    與交易所進行實際持倉校準。
    若偵測到本地數據與交易所數據不符，強制覆蓋為交易所數據。
    """
    if PAPER_TRADING:
        print("ℹ️ [CALIBRATION] 紙上交易模式，跳過交易所校準。")
        return

    try:
        # 從交易所抓取所有持倉
        positions = await exchange.fetch_positions()
        for pos in positions:
            # 處理不同交易所的 symbol 格式 (如 BTC/USDT:USDT -> BTCUSDT)
            raw_symbol = pos.get('symbol', '')
            sym = raw_symbol.split(':')[0].replace('/', '')
            
            real_qty = float(pos.get('contracts', 0.0) or pos.get('info', {}).get('positionAmt', 0.0))
            if abs(real_qty) > 0.000001:
                if sym not in ALL_SYMBOLS:
                    print(f"⚠️ [發現未監控持倉] 交易所內 {sym} 仍有實盤倉位，自動加回監控清單並在介面顯示！")
                    ALL_SYMBOLS.append(sym)
                    STATES[sym] = build_symbol_state(sym)
                    apply_symbol_profile(sym, SYMBOL_PROFILES.get(sym, {}))
            
            if sym in STATES:
                current_qty = STATES[sym].get("qty", 0.0)

                # 設定容差 (例如 0.1% 以內視為一致，避免浮點數誤差)
                if abs(real_qty - current_qty) > (abs(current_qty) * 0.001) and abs(real_qty) > 0:
                    print(f"⚖️ [CALIBRATION] 校準 {sym}: 內部 {current_qty} -> 交易所 {real_qty}")
                    STATES[sym]["qty"] = real_qty
                    # 如果有持倉但內部記錄是空的，這就是關鍵的校準
                    if current_qty == 0:
                        STATES[sym]["entry_price"] = float(pos.get('entryPrice', pos.get('avg_price', 0.0)))
                        STATES[sym]["avg_price"] = STATES[sym]["entry_price"]
                        print(f"✅ [CALIBRATION] 已恢復 {sym} 的持倉數據。")
                        
    except Exception as e:
        print(f"⚠️ [CALIBRATION_FAIL] 無法連線交易所校準: {e}")

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
    
    print("🔍 [INIT] 正在啟動時校準倉位...")
    await calibrate_with_exchange(exchange)
    await fetch_real_balance()
    await load_open_positions()
    await fetch_all_sma200(exchange)
    await fetch_all_ema50_1h(exchange)
    await fetch_all_ema_15m(exchange)

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
            
            # ====== 第二點：總資金水位審查 ======
            if not getattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', False):
                is_equity_safe = check_total_equity_protection()
                if not is_equity_safe:
                    await execute_panic_sell_all_positions()
                    # 激活全局冷卻時間 (1小時)
                    print("🛑 [全局冷卻] 機器人進入 1 小時強制休眠，防禦連續虧損！")
                    setattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', True)
                    setattr(sys.modules[__name__], 'MELTDOWN_TIME', time.time())
            
            if getattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', False):
                if time.time() - getattr(sys.modules[__name__], 'MELTDOWN_TIME', 0) > 3600:
                    print("✅ [全局冷卻結束] 1小時防禦期滿，恢復正常運行。")
                    setattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', False)
                else:
                    await asyncio.sleep(60)
                    continue

            for sym in ALL_SYMBOLS:
                if STATES[sym].get("sync_required"):
                    print(f"🔄 [SYNC_REQUIRED] 正在重新校準 {sym}...")
                    # 重新讀取本地全域倉位狀態
                    await load_open_positions() 
                    STATES[sym]["sync_required"] = False

            for sym in ALL_SYMBOLS:
                STATES[sym]["adjusted_this_tick"] = False
            # await update_market_wind(exchange)  # 已移至獨立 Task
            print_multi_status()
            await fetch_all_klines(exchange)
            for sym in ALL_SYMBOLS:
                if STATES[sym].get("status") == "COOLDOWN":
                    if time.time() < STATES[sym].get("next_status_time", 0):
                        continue
                    else:
                        STATES[sym]["status"] = "ACTIVE"
                        print(f"✅ [冷卻結束] {sym} 恢復 ACTIVE 狀態")

                # 使用安全護盾執行指標計算
                await safe_execute(compute_indicators, sym)
                    
            # --- 新增背離自動掃描 ---
            # 每 5 分鐘執行一次檢查
            if time.time() % 300 < MAIN_LOOP_INTERVAL_SEC:
                div_list = check_all_divergence_logic()
                for msg in div_list:
                    print(f"🌟 [自動背離掃描] {msg}")

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
                if STATES[sym].get("status") != "ACTIVE":
                    continue
                # 使用安全護盾執行出場檢查
                await safe_execute(check_exits, sym)

            # --- 進場檢查區塊 ---
            # 由於 check_entries 本身會處理所有幣種（全域掃描），但它內部處理了每個候選名單，
            # 若發生重大全域異常，還是先保留 try-except 以免卡死
            try:
                await check_entries()
            except Exception as e:
                print(f"⚠️ [進場檢查異常]: {e}")
                traceback.print_exc()

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
            
            # 發生致命錯誤時，強制重新載入開倉部位，避免狀態卡死
            try:
                await load_open_positions()
                print("♻️ 已重新載入真實部位完成")
            except Exception as e2:
                print(f"⚠️ 重新載入部位失敗: {e2}")
            
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
        await fetch_all_ema_15m(exchange)
        print("🔄 [HTF] 已更新所有幣種 15m SMA200 與 1H EMA50 以及 15m EMA20 & EMA50")

def print_multi_status():
    """
    優化後的狀態輸出：將進行中的持倉置頂，並增加視覺分隔。
    """
    from datetime import datetime
    now = datetime.now().strftime("%H:%M:%S")
    
    # 1. 篩選出所有正在持倉的幣種
    active_positions = []
    for sym, s in STATES.items():
        if abs(s.get('qty', 0)) > 0.000001:
            # 取得當前獲利百分比，若無則顯示 0.0
            pnl = round(s.get('pnl_pct', 0.0), 2)
            direction = "多" if s.get('qty', 0) > 0 else "空"
            avg_price = s.get('avg_price', 0)
            active_positions.append(f"  🔥 持倉] {sym} | 方向:{direction} | 入場:{avg_price} | 獲利:{pnl}%")

    # 2. 開始輸出整體的儀表板
    print(f"[{now}] [__multi__] 📊 [現況]")
    
    # 如果有持倉，優先列出在最上方
    if active_positions:
        for pos in active_positions:
            print(pos)
    else:
        print("  ✨ 持倉] 目前無持倉")

    # 3. 輸出統計數據 (監控池、冷卻、禁賽、持倉數)
    total_monitored = len(STATES)
    active_count = len(active_positions)
    # 計算冷卻中數量
    cooldown_count = sum(1 for s in STATES.values() if s.get('status') == 'COOLDOWN')
    banned_count = sum(1 for s in STATES.values() if s.get('status') == 'BANNED')

    print(f"  📊 統計] 監控池={total_monitored} | 冷卻={cooldown_count} | 禁賽={banned_count} | 持倉數:{active_count}/{MAX_POSITIONS}")
    
    # 4. 使用分隔線區隔，讓每一輪掃描的開始更清晰
    print("-" * 60)

async def periodic_status_log():
    while True:
        await asyncio.sleep(60)
        # 狀態列印已移至 main_loop 的 print_multi_status
        # 保留 periodic_status_log 來定時儲存快取
        
        # 定期儲存 ATR 快取
        import json
        try:
            cache_data = {}
            for sym in STATES:
                cache_data[sym] = STATES[sym]["atr_history"][-1000:]
            with open("atr_history_cache.json", "w") as f:
                json.dump(cache_data, f)
        except Exception:
            pass

async def sync_paper_state():
    while True:
        await asyncio.sleep(1)
        if not PAPER_TRADING:
            continue
        try:
            with open("paper_state.json", "r") as f:
                state = json.load(f)
            for sym in ALL_SYMBOLS:
                pk = paper_key(sym)
                pos = state.get("positions", {}).get(pk, {})
                qty = float(pos.get("qty", 0.0))
                STATES[sym]["qty"] = qty
                STATES[sym]["avg_price"] = float(pos.get("avg_price", 0.0))
        except:
            pass

async def main():
    asyncio.create_task(sync_paper_state())
    asyncio.create_task(periodic_htf_update(exchange_futures))
    asyncio.create_task(periodic_status_log())
    asyncio.create_task(check_stale_limit_orders())  # 逆期限價單止單機制
    
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
