import os
from dotenv import load_dotenv

load_dotenv()

USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
PAPER_TRADING = not BINANCE_API_KEY or BINANCE_API_KEY == "your_api_key_here"

# Demo Trading 帳戶實際餘額可能遠大於測試用的本金上限，倉位大小要用上限計算（僅在非紙上交易時生效）。
# 設為 0 或留空則不再限制真實交易帳戶的資金上限。
LIVE_CAPITAL_CAP = float(os.getenv("LIVE_CAPITAL_CAP", "150.0"))
TIMEFRAME = '5m'
TRADE_HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trade_history.json")
MAX_GLOBAL_CONCURRENT_TRADES = 3
DEFAULT_LEVERAGE = 5
# DUAL_SHOT_MAX_SLOTS / MAX_POSITIONS 以下維持當作「本金 <200 USDT」時的預設值
# （也是找不到餘額資料時的保守 fallback）。本金 200~1000 USDT 區間改用
# CAPITAL_SLOT_TIERS 分階段動態決定槽位數，見 core/balance.py 的
# get_dynamic_max_slots()：本金越大，允許同時開的倉位越多，但刻意讓每槽金額
# 隨本金一起成長（不會因為槽位變多就把單筆金額稀釋回太小、被手續費/滑價吃掉）。
DUAL_SHOT_MAX_SLOTS = 3
DUAL_SHOT_LEVERAGE = 5
DUAL_SHOT_ORDER_TIMEOUT = 600
DUAL_SHOT_MIN_PROFIT_ROOM = 0.012

# (本金上限[USDT], 該階段槽位數)，由小到大排序；本金落在哪一段的上限之內
# 就用那一段的槽位數，超過最後一段（1000）則沿用最後一段的槽位數。
CAPITAL_SLOT_TIERS = [
    (200, 3),
    (400, 4),
    (600, 5),
    (800, 6),
    (1000, 7),
]

COIN_PROFILE_CONFIG = {
    # 第一類：核心趨勢型 (Core_Trend) - 穩健獲利為主
    # sl_atr_multiplier 放寬：避免 5m K 線雜訊洗出；tp_atr 降低：設定可實際觸及的目標
    "BTCUSDT":  {"sl_atr_multiplier": 2.8, "tp_atr_multiplier": 6.0,  "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 1.6, "min_signal_strength": 15, "disable_rescue_dca": False},
    "ETHUSDT":  {"sl_atr_multiplier": 2.8, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 1.6, "min_signal_strength": 14, "disable_rescue_dca": False},
    "SOLUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 9.0,  "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 3600, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 1.8, "min_signal_strength": 15, "disable_rescue_dca": False, "hard_sl_pct": 0.030},
    "BNBUSDT":  {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 1.8, "min_signal_strength": 16, "disable_rescue_dca": False},
    "XRPUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 1.6, "min_signal_strength": 15, "disable_rescue_dca": False, "hard_sl_pct": 0.030},
    "LINKUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 15, "disable_rescue_dca": False, "hard_sl_pct": 0.030},
    "ADAUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 7.0,  "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 15, "disable_rescue_dca": False, "hard_sl_pct": 0.030},
    "AVAXUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 15, "disable_rescue_dca": False, "hard_sl_pct": 0.030},
    "DOTUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 15, "disable_rescue_dca": False, "hard_sl_pct": 0.030},
    "NEARUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 15, "disable_rescue_dca": False, "hard_sl_pct": 0.030, "stagnation_base_limit": 7200},
    "LTCUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 15, "disable_rescue_dca": False, "hard_sl_pct": 0.030},

    # 第二類：高彈性動能型 (High_Beta_Momentum) - 高報酬彈性
    # sl_atr 從 2.0 放寬到 2.8~3.0，避免高波動幣種被小幅回調就停損
    # tp_atr_multiplier 依使用者要求改回 7dceb33 的寬停利目標，配合今天放寬過的
    # sl_tiers/PeakLock 階梯，讓有機會的單子有更大空間長成大賺，不要結構性地
    # 把目標設得比 7dceb33 窄很多。
    "SUIUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 20.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 18, "loss_reentry_cooldown_sec": 7200, "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7},
    "INJUSDT":  {"sl_atr_multiplier": 2.8, "tp_atr_multiplier": 14.0, "volume_threshold_factor": 1.2, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 16, "hard_sl_pct": 0.030, "disable_rescue_dca": False, "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7},
    "APTUSDT":  {"sl_atr_multiplier": 2.8, "tp_atr_multiplier": 18.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 15, "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7},

    # 第三類：投機風險型 (Speculative_Risk) - 高波動/高收益點綴
    # UNI/HBAR/DOGE 在 7dceb33 沒有完全對應設定（DOGE 有、UNI/HBAR 沒有），
    # DOGE 直接採用 7dceb33 的值；UNI/HBAR 維持今天已決定的設定不動。
    "UNIUSDT":  {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0,  "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 16, "hard_sl_pct": 0.030},
    "HBARUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0,  "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 17, "disable_rescue_dca": False, "hard_sl_pct": 0.030},
    "DOGEUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 20.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 5, "rr_threshold": 2.0, "min_signal_strength": 13, "disable_rescue_dca": False, "hard_sl_pct": 0.030},
    }

# 新幣（雷達選入但不在上方設定檔）自動套用此保守設定
# tp_atr_multiplier 依使用者要求改回 7dceb33 的 12x（原本降到 7x）
# min_signal_strength 原本 15.0 是全系統最低門檻，比主力幣（21~24）低很多——動態選幣
# 選進來的新幣本來就流動性較差、波動雜訊較多，訊號門檻卻最寬鬆，等於「品質最沒把握
# 的幣種反而最容易進場」，是 US/THE/POWER/EDGE 這類一進場就套牢案例的成因之一。
# 拉高到 20.0，跟主力幣的門檻拉近，讓新幣也要有夠強的訊號才進場。
DEFAULT_NEW_COIN_PROFILE = {
    "sl_atr_multiplier": 3.5, "tp_atr_multiplier": 12.0,
    "volume_threshold_factor": 1.3, "breakeven_trigger": 1.2,
    "min_flip_time": 1800, "mtf_filter": True,
    "profile_type": "Speculative_Risk",
    "leverage": 5, "rr_threshold": 2.0,
    "min_signal_strength": 14,
    "disable_rescue_dca": False, "hard_sl_pct": 0.030,
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
 
# 使用者要求把小幣加回來：實測今天大幣（BTC/ETH/BNB等15檔）平均每筆淨損益 -0.47U，
# 波動太悶、峰值平均只有0.13%；小幣（AAVE/HBAR/INJ/UNI/XLM等）平均每筆淨損益 -0.35U，
# 峰值平均0.32%（2.5倍），明顯波動度更夠、行情比較走得動。維持原本 15 檔大幣當主力
# （流動性好），再加回 ATR_ELIGIBLE_SYMBOLS 裡原本就篩過的中小型幣種補足波動度。
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT",
    "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "SUIUSDT", "DOTUSDT", "NEARUSDT", "APTUSDT",
    "LTCUSDT", "BCHUSDT",
    "UNIUSDT", "ETCUSDT", "AAVEUSDT", "ATOMUSDT", "HBARUSDT",
    "XLMUSDT", "INJUSDT", "RENDERUSDT",
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
BAN_WINDOW = 1800          # 縮短至 30 分鐘觀測窗口，更快偵測連續停損
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 2    # 30 分鐘內觸發 2 次停損就封禁（原 3 次）
SL_ATR_MULTIPLIER = 1.8
TP_ATR_MULTIPLIER = 6.0
HARD_STOP_LOSS_PCT = 0.030
EXIT_RR_MULTIPLIER = 1.5

MIN_PROFIT_LOCK_THRESHOLD = 0.008
PROTECTED_PROFIT_FLOOR   = 0.0025
MOMENTUM_EXIT_ATR_THRESHOLD = float(os.getenv('MOMENTUM_EXIT_ATR_THRESHOLD', 10.0))
MOMENTUM_EXIT_MIN_PROFIT_PCT = float(os.getenv('MOMENTUM_EXIT_MIN_PROFIT_PCT', 0.01))
HIGH_POINT_STAGNATION_MIN_PROFIT = float(os.getenv('HIGH_POINT_STAGNATION_MIN_PROFIT', 0.0030))
HIGH_POINT_STAGNATION_TIME = int(os.getenv('HIGH_POINT_STAGNATION_TIME', 300))
MIN_STAGNATION_TIME = int(os.getenv('MIN_STAGNATION_TIME', 60))
TREND_PERSISTENCE_WINDOW  = 300
PRICE_MOVEMENT_THRESHOLD  = 0.0015

# Radar Selection Thresholds
MIN_ATR_PCT_FOR_ENTRY = float(os.getenv("MIN_ATR_PCT_FOR_ENTRY", 2.5))
MAX_ATR_PCT_FOR_ENTRY = float(os.getenv("MAX_ATR_PCT_FOR_ENTRY", 7.2))
MIN_1H_VOL_PCT_FOR_ENTRY = float(os.getenv("MIN_1H_VOL_PCT_FOR_ENTRY", 0.42))
MAX_1H_VOL_PCT_FOR_ENTRY = float(os.getenv("MAX_1H_VOL_PCT_FOR_ENTRY", 2.8))
MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY = float(os.getenv("MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY", 14.0))

TAKER_FEE_RATE = 0.0005
ROUND_TRIP_FEE_PCT = TAKER_FEE_RATE * 2
# 虧損出場後避免同一幣種立刻沿用已失效的同方向訊號再次進場；個別幣種仍可覆蓋。
DEFAULT_LOSS_REENTRY_COOLDOWN_SEC = int(os.getenv("DEFAULT_LOSS_REENTRY_COOLDOWN_SEC", 3600))

# 全域調整：進場方式改回 7dceb33 的 auto 模式，依訊號強度自動選 pullback/chase/market
# （原本被改成強制全部用 pullback，不管訊號多強都要等拉回才進場，實測 AVAXUSDT
# 因此連續 5 次守門失敗才等到拉回，等到的時候價格已經跑掉，追價滑價 0.44%。
# auto 模式讓強訊號直接市價成交、不用等，只有弱訊號才值得耐心等拉回。這是純粹的
# 執行風險管理邏輯，不是 demo 環境專屬，真正上線一樣適用，一併採用 7dceb33 的門檻）。
ENTRY_ORDER_MODE = os.getenv("ENTRY_ORDER_MODE", "auto").lower()
ENTRY_PULLBACK_ATR_MULT = float(os.getenv("ENTRY_PULLBACK_ATR_MULT", 0.16))
ENTRY_CHASE_OFFSET_PCT = float(os.getenv("ENTRY_CHASE_OFFSET_PCT", 0.0005))
ENTRY_ORDER_MODE_AUTO_STRONG = float(os.getenv("ENTRY_ORDER_MODE_AUTO_STRONG", 24.0))
ENTRY_ORDER_MODE_AUTO_MARKET = float(os.getenv("ENTRY_ORDER_MODE_AUTO_MARKET", 34.0))

ENTRY_STRICTNESS_MODE = os.getenv("ENTRY_STRICTNESS_MODE", "strict").lower()
ENTRY_STRICTNESS_PROFILES = {
    "relaxed": {
        "volume_ratio": 0.35,
        "pin_threshold": 3.2,
        "min_body_ratio": 0.10,
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
    return ENTRY_STRICTNESS_PROFILES.get(mode_name, ENTRY_STRICTNESS_PROFILES["balanced"])

# 是否啟用 BTC 大盤過濾鎖定小幣開倉（True=啟用鎖定，False=小幣走自己獨立行情）
USE_BTC_MACRO_FILTER = os.getenv("USE_BTC_MACRO_FILTER", "true").lower() in ("1", "true", "yes", "on")

# 市場資料分批抓取：將所有監控幣種分成此數量的批次，fetch_all_klines 每輪抓一個批次
# （輪替），降低每輪瞬間送出請求量，避免衝高幣安 API 權重
MARKET_FETCH_BATCHES = int(os.getenv('MARKET_FETCH_BATCHES', '4'))
# 控制同時對交易所發出的併發請求數（Semaphore 大小）
REQUEST_SEMAPHORE_SIZE = int(os.getenv('REQUEST_SEMAPHORE_SIZE', '2'))
# 批次之間的額外延遲（秒），需要時可拉開批次間隔
KLINE_BATCH_PAUSE_SEC = float(os.getenv("KLINE_BATCH_PAUSE_SEC", "0.0"))
# REST 成交流只作輔助動能判斷，降低輪詢頻率並縮小回傳筆數可大幅減少 API 權重。
TRADE_POLL_INTERVAL_SEC = float(os.getenv("TRADE_POLL_INTERVAL_SEC", "30"))
TRADE_POLL_LIMIT = int(os.getenv("TRADE_POLL_LIMIT", "20"))
API_RATE_LIMIT_COOLDOWN_SEC = float(os.getenv("API_RATE_LIMIT_COOLDOWN_SEC", "60"))
