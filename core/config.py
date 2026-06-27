import os
from dotenv import load_dotenv

load_dotenv()

USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
PAPER_TRADING = True
TIMEFRAME = '5m'
TRADE_HISTORY_FILE = "trade_history.json"
MAX_GLOBAL_CONCURRENT_TRADES = 2
DEFAULT_LEVERAGE = 5
DUAL_SHOT_MAX_SLOTS = 2
DUAL_SHOT_LEVERAGE = 5
DUAL_SHOT_ORDER_TIMEOUT = 300
DUAL_SHOT_MIN_PROFIT_ROOM = 0.012

COIN_PROFILE_CONFIG = {
    # --- 第一類：核心趨勢層 (Core Trend) ---
    "SOLUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 8.0,  "volume_threshold_factor": 1.0, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 15.0, "disable_rescue_dca": True, "hard_sl_pct": 0.012},
    "ETHUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 0.6, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 15.0},
    "BNBUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 0.6, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 15.0},
    "XRPUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.6, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 15.0},
    "ADAUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 0.6, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 15.0},
    "DOTUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 0.6, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 15.0},
    "LTCUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 0.6, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 15.0},
    "LINKUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.6, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 15.0},
    # --- 第二類：高彈性動能層 (High-Beta Momentum) ---
    "SUIUSDT":  {"sl_atr_multiplier": 3.5, "tp_atr_multiplier": 20.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 0.7, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 2.0, "min_signal_strength": 17.0, "trailing_activation_atr": 1.2, "trailing_distance_atr": 0.7},
    "INJUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.2, "breakeven_trigger": 0.4, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 2.0, "min_signal_strength": 17.0, "hard_sl_pct": 0.015, "disable_rescue_dca": True, "trailing_activation_atr": 1.2, "trailing_distance_atr": 0.7},
    "NEARUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 16.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 2.0, "min_signal_strength": 17.0, "disable_rescue_dca": True},
    "APTUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 2.0, "min_signal_strength": 17.0},
    "ARBUSDT":  {"sl_atr_multiplier": 3.5, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 3, "rr_threshold": 2.0, "min_signal_strength": 17.0},
    "OPUSDT":   {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 3, "rr_threshold": 2.0, "min_signal_strength": 15.0},
    "HYPEUSDT": {"sl_atr_multiplier": 3.5, "tp_atr_multiplier": 12.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 3, "rr_threshold": 2.0, "min_signal_strength": 17.0},
    "AAVEUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 12.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 2.0, "min_signal_strength": 17.0, "trailing_activation_atr": 1.5, "trailing_distance_atr": 1.0, "disable_rescue_dca": True},
    # --- 第三類：投機與特定風險層 (Speculative_Risk) ---
    "AVAXUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.5, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 3, "rr_threshold": 2.0, "min_signal_strength": 17.0},
    "DOGEUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 20.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.8, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "Speculative_Risk",   "leverage": 3, "rr_threshold": 1.8, "min_signal_strength": 15.0},
}

LEVERAGE_TIERS = {
    "custom_leverage": {
        "coins": {},
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
ATR_WARMUP_SYMBOL_COUNT = 19
ATR_WARMUP_LIMIT = 1000
ATR_WARMUP_PAUSE_SEC = 0.4
TIME_STOP_MINUTES = 30

DEFAULT_SYMBOLS = [
    "SOLUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT",
    "LINKUSDT", "SUIUSDT", "INJUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
    "HYPEUSDT", "AAVEUSDT", "AVAXUSDT", "DOGEUSDT",
]
CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot_symbols.json")

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

MAX_POSITIONS = 3
COOLDOWN_SEC = 900

DAILY_LOSS_LIMIT_PCT = 0.10

MAIN_LOOP_INTERVAL_SEC = 25
PENDING_CONFIRM_SEC = 2
BAN_WINDOW = 3600
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 3
SL_ATR_MULTIPLIER = 2.5
TP_ATR_MULTIPLIER = 3.0
HARD_STOP_LOSS_PCT = 0.015

MIN_PROFIT_LOCK_THRESHOLD = 0.004
PROTECTED_PROFIT_FLOOR   = 0.0025
TREND_PERSISTENCE_WINDOW  = 300
PRICE_MOVEMENT_THRESHOLD  = 0.0015

TAKER_FEE_RATE = 0.0005
ROUND_TRIP_FEE_PCT = TAKER_FEE_RATE * 2
