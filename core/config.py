import os
from dotenv import load_dotenv

load_dotenv()

USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
PAPER_TRADING = True
TIMEFRAME = '5m'
TRADE_HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trade_history.json")
MAX_GLOBAL_CONCURRENT_TRADES = 6
DEFAULT_LEVERAGE = 5
DUAL_SHOT_MAX_SLOTS = 3
DUAL_SHOT_LEVERAGE = 5
DUAL_SHOT_ORDER_TIMEOUT = 600
DUAL_SHOT_MIN_PROFIT_ROOM = 0.012

COIN_PROFILE_CONFIG = {
    # 第一類：核心趨勢型
    "ETHUSDT":  {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 3, "rr_threshold": 1.6, "min_signal_strength": 17.0, "disable_rescue_dca": False},
    "SOLUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 9.0,  "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 3600, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 20.0, "disable_rescue_dca": False, "hard_sl_pct": 0.025},
    "AVAXUSDT": {"sl_atr_multiplier": 1.8, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 3, "rr_threshold": 1.8, "min_signal_strength": 17.0, "disable_rescue_dca": False},
    "NEARUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 3, "rr_threshold": 1.8, "min_signal_strength": 18.0, "disable_rescue_dca": False, "hard_sl_pct": 0.025},
    "ADAUSDT":  {"sl_atr_multiplier": 1.8, "tp_atr_multiplier": 7.0,  "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 2, "rr_threshold": 1.5, "min_signal_strength": 16.0, "hard_sl_pct": 0.025, "disable_rescue_dca": False},
    "AAVEUSDT": {"sl_atr_multiplier": 1.8, "tp_atr_multiplier": 12.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 3, "rr_threshold": 1.8, "min_signal_strength": 20.0, "disable_rescue_dca": False, "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.8},
    "BNBUSDT":  {"sl_atr_multiplier": 1.8, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 18.0, "disable_rescue_dca": False},
    "XRPUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 2.0, "min_signal_strength": 20.0, "disable_rescue_dca": False, "hard_sl_pct": 0.025},
    "DOTUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 17.0, "hard_sl_pct": 0.02},
    "LTCUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 1.8, "min_signal_strength": 17.0, "hard_sl_pct": 0.02},
    "LINKUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 4, "rr_threshold": 2.0, "min_signal_strength": 20.0, "disable_rescue_dca": False, "hard_sl_pct": 0.025},
    # 新幣/特殊調整
    "BTWUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 8.0,  "volume_threshold_factor": 1.2, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 2, "rr_threshold": 2.2, "min_signal_strength": 22.0, "hard_sl_pct": 0.020, "disable_rescue_dca": True, "disable_entry": True},
    # 第二類：高彈性動能型
    "ORDIUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 3, "rr_threshold": 2.0, "min_signal_strength": 20.0, "hard_sl_pct": 0.030, "disable_rescue_dca": False, "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7},
    "INJUSDT":  {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.2, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 2.0, "min_signal_strength": 19.0, "hard_sl_pct": 0.030, "disable_rescue_dca": False, "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7},
    "APTUSDT":  {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 18.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 3, "rr_threshold": 2.0, "min_signal_strength": 18.0, "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7},
    "SUIUSDT":  {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 20.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "High_Beta_Momentum", "leverage": 4, "rr_threshold": 2.0, "min_signal_strength": 19.0, "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7},
    "HYPEUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 12.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 3, "rr_threshold": 2.0, "min_signal_strength": 19.0},
    # 第三類：低價投機型
    "ARBUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 16.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 2, "rr_threshold": 2.0, "min_signal_strength": 17.0, "hard_sl_pct": 0.030},
    "OPUSDT":   {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 2, "rr_threshold": 2.0, "min_signal_strength": 17.0, "hard_sl_pct": 0.025},
    "DOGEUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 20.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": False, "profile_type": "Speculative_Risk",   "leverage": 3, "rr_threshold": 1.8, "min_signal_strength": 17.0},
    # 第四類：新/小幣投機型（高風險，低槓桿保護）
    "GUAUSDT":   {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 2, "rr_threshold": 2.0, "min_signal_strength": 20.0, "disable_rescue_dca": False, "hard_sl_pct": 0.030},
    "SIRENUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 2, "rr_threshold": 2.0, "min_signal_strength": 20.0, "disable_rescue_dca": False, "hard_sl_pct": 0.030},
}

# 新幣（雷達選入但不在上方設定檔）自動套用此保守設定
DEFAULT_NEW_COIN_PROFILE = {
    "sl_atr_multiplier": 3.5, "tp_atr_multiplier": 12.0,
    "volume_threshold_factor": 1.3, "breakeven_trigger": 1.2,
    "min_flip_time": 1800, "mtf_filter": True,
    "profile_type": "Speculative_Risk",
    "leverage": 2, "rr_threshold": 2.0,
    "min_signal_strength": 20.0,
    "disable_rescue_dca": False, "hard_sl_pct": 0.015,
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
CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot_symbols.json")

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
        "hard_stop_loss_pct": 0.025,
    },
    "balanced": {
        "personality": "balanced",
        "risk_multiplier": 1.0,
        "volume_multiplier": 1.0,
        "entry_cooldown_sec": 45,
        "max_additional_entries": 3,
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "sl_atr_multiplier": 1.2,
        "tp_atr_multiplier": 2.4,
        "hard_stop_loss_pct": 0.025,
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
        "hard_stop_loss_pct": 0.025,
    },
    "adaptive": {
        "personality": "adaptive",
        "risk_multiplier": 1.0,
        "volume_multiplier": 1.0,
        "entry_cooldown_sec": 45,
        "max_additional_entries": 3,
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "sl_atr_multiplier": 1.2,
        "tp_atr_multiplier": 2.4,
        "hard_stop_loss_pct": 0.025,
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

MAIN_LOOP_INTERVAL_SEC = 10
PENDING_CONFIRM_SEC = 2
BAN_WINDOW = 3600
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 3
SL_ATR_MULTIPLIER = 1.5
TP_ATR_MULTIPLIER = 3.0
HARD_STOP_LOSS_PCT = 0.025

MIN_PROFIT_LOCK_THRESHOLD = 0.004
PROTECTED_PROFIT_FLOOR   = 0.0025
TREND_PERSISTENCE_WINDOW  = 300
PRICE_MOVEMENT_THRESHOLD  = 0.0015

TAKER_FEE_RATE = 0.0005
ROUND_TRIP_FEE_PCT = TAKER_FEE_RATE * 2

# 進場掛單模式配置：'market' (市價), 'passive' (被動掛買一賣一), 'pullback' (回踩掛單), 'chase' (對手價追價)
ENTRY_ORDER_MODE = "chase"
ENTRY_PULLBACK_ATR_MULT = 0.12
ENTRY_CHASE_OFFSET_PCT = 0.0005

ENTRY_STRICTNESS_MODE = os.getenv("ENTRY_STRICTNESS_MODE", "relaxed").lower()
ENTRY_STRICTNESS_PROFILES = {
    "relaxed": {
        "volume_ratio": 0.35,
        "pin_threshold": 3.2,
        "min_body_ratio": 0.15,
        "min_signal_strength": 8.0,
        "rsi_long_floor": 15.0,
        "rsi_short_floor": 15.0,
        "rsi_long_ceiling": 82.0,
        "rsi_short_ceiling": 78.0,
        "min_entry_strength": 5.0,
    },
    "balanced": {
        "volume_ratio": 0.70,
        "pin_threshold": 2.0,
        "min_body_ratio": 0.35,
        "min_signal_strength": 12.0,
        "rsi_long_floor": 25.0,
        "rsi_short_floor": 25.0,
        "rsi_long_ceiling": 75.0,
        "rsi_short_ceiling": 68.0,
        "min_entry_strength": 10.0,
    },
    "strict": {
        "volume_ratio": 0.85,
        "pin_threshold": 1.5,
        "min_body_ratio": 0.45,
        "min_signal_strength": 15.0,
        "rsi_long_floor": 32.0,
        "rsi_short_floor": 30.0,
        "rsi_long_ceiling": 75.0,
        "rsi_short_ceiling": 68.0,
        "min_entry_strength": 12.0,
    },
}


def get_entry_strictness_profile(mode=None):
    mode_name = (mode or ENTRY_STRICTNESS_MODE).lower()
    return ENTRY_STRICTNESS_PROFILES.get(mode_name, ENTRY_STRICTNESS_PROFILES["relaxed"])

# 是否啟用 BTC 大盤過濾鎖定小幣開倉（True=啟用鎖定，False=小幣走自己獨立行情）
USE_BTC_MACRO_FILTER = False

