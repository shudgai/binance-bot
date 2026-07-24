import os
from dotenv import load_dotenv

load_dotenv()

USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()

def _is_placeholder_key(key):
    if not key:
        return True
    k = str(key).lower()
    return "your_" in k or "api_key" in k or "placeholder" in k or k == "your_api_key_here"

def _detect_port():
    import sys
    env_port = os.getenv("PORT", "").strip()
    if env_port:
        return env_port
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            return sys.argv[i + 1].strip()
        elif arg.startswith("--port="):
            return arg.split("=", 1)[1].strip()
    return "8005"

PORT = _detect_port()
PORT_SUFFIX = f"_{PORT}" if PORT and PORT != "8005" else ""

def _detect_paper_trading():
    port = _detect_port()
    # Port 8005 為純紙上模擬交易 (Paper Trading)，Port 8007 為幣安測試網 (Binance Demo Trading)
    if port == "8005":
        return True
    key = os.getenv("BINANCE_API_KEY", "").strip()
    return _is_placeholder_key(key)

PAPER_TRADING = _detect_paper_trading()

# Demo Trading 帳戶實際餘額可能遠大於測試用的本金上限，倉位大小要用上限計算（僅在非紙上交易時生效）。
# 設為 0 或留空則不再限制真實交易帳戶的資金上限。
LIVE_CAPITAL_CAP = float(os.getenv("LIVE_CAPITAL_CAP", "150.0"))
MAX_RISK_PER_TRADE_PCT = 0.025
TIMEFRAME = '5m'
def _detect_port():
    import sys
    env_port = os.getenv("PORT", "").strip()
    if env_port:
        return env_port
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            return sys.argv[i + 1].strip()
        elif arg.startswith("--port="):
            return arg.split("=", 1)[1].strip()
    return "8005"

PORT = _detect_port()
PORT_SUFFIX = f"_{PORT}" if PORT and PORT != "8005" else ""

def get_data_file_path(filename: str) -> str:
    base, ext = os.path.splitext(filename)
    # bot_symbols.json 必須是全主機共享的動態幣池清單，確保所有 Port (8005 / 8007 等) 監控與下單幣種完全一致
    if base == "bot_symbols":
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot_symbols.json")
    suffix = PORT_SUFFIX if PORT_SUFFIX else ""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", f"{base}{suffix}{ext}")

TRADE_HISTORY_FILE = get_data_file_path("trade_history.json")
PAPER_STATE_FILE = get_data_file_path("paper_state.json")
MAX_GLOBAL_CONCURRENT_TRADES = 1
DEFAULT_LEVERAGE = 5
# DUAL_SHOT_MAX_SLOTS 以下維持當作「本金 <200 USDT」時的保守 fallback
# （也是找不到餘額資料時的保守 fallback）。本金 200~1000 USDT 區間改用
# CAPITAL_SLOT_TIERS 分階段動態決定槽位數，見 core/balance.py 的
# get_dynamic_max_slots()：本金越大，允許同時開的倉位越多，但刻意讓每槽金額
# 隨本金一起成長（不會因為槽位變多就把單筆金額稀釋回太小、被手續費/滑價吃掉）。
DUAL_SHOT_MAX_SLOTS = 3
DUAL_SHOT_LEVERAGE = 5
DUAL_SHOT_ORDER_TIMEOUT = 600
DUAL_SHOT_MIN_PROFIT_ROOM = 0.012
TRADE_POOL_SIZE = 15
DISABLE_MA_BREAKOUT = True
DISABLE_MA25_PULLBACK = False
DISABLE_MA_CROSS = True  # 禁用滯後性高的 MA_Cross 交叉開倉路線，完全改用 MA25_Pullback

# ─── 區間模式參數 (Range Mode) ────────────────────────────────────────────────
# 在 ADX 低、無明顯趨勢時，於確認支撐買多、確認壓力做空的獨立模式。
# 與 MA 趨勢策略共用同一組槽位，不新增倉位數量。
RANGE_MODE_ENABLED = False          # 總開關；False 則完全禁用區間模式
RANGE_ADX_THRESHOLD = 65.0          # ADX < 此值才視為區間行情（極度放寬至 65.0）
RANGE_LOOKBACK = 40                 # 辨識支撐/壓力用的回顧已收盤 K 棒數
RANGE_TOUCH_COUNT = 2               # 最少幾次觸碰才確認水平區（防止偽支撐）
RANGE_TOUCH_ATR_TOLERANCE = 0.3    # 觸碰誤差帶（ATR 倍數），允許小幅穿越
RANGE_MIN_NET_PROFIT_PCT = -0.0020  # 一般幣種淨空間門檻（極度放寬至 -0.2%）
STRICT_ENTRY_SYMBOLS = frozenset({"ETHUSDT", "XRPUSDT"})
STRICT_RANGE_MIN_NET_PROFIT_PCT = 0.0002 # ETH/XRP 窄區間防掃損門檻（放寬至 0.02%）
RANGE_MIN_RR = 0.4                 # 區間單最低盈虧比要求（極度放寬至 0.4）
RANGE_MAX_SLOTS = 3                 # 區間模式最多佔幾個槽位（與總槽位對齊）
RANGE_MIN_SIGNAL_STRENGTH = 8.0     # 最低信號強度（極度放寬至 8.0）
# ─────────────────────────────────────────────────────────────────────────────


# (本金上限[USDT], 該階段槽位數)，由小到大排序；本金落在哪一段的上限之內
# 就用那一段的槽位數，超過最後一段（1000）則沿用最後一段的槽位數。
CAPITAL_SLOT_TIERS = [
    (200, 3),
    (400, 5),
    (600, 5),
    (800, 5),
    (1000, 5),
]

COIN_PROFILE_CONFIG = {
    # 第一類：核心趨勢型 (Core_Trend) - 穩健獲利為主
    # 選幣標準：24h量 > 1億USDT、流動性佳、滑點低
    "BTCUSDT":  {"sl_atr_multiplier": 1.2, "tp_atr_multiplier": 8.0,  "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 13, "disable_rescue_dca": False},
    "ETHUSDT":  {"sl_atr_multiplier": 1.2, "tp_atr_multiplier": 12.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 12, "disable_rescue_dca": False},
    "SOLUSDT":  {"sl_atr_multiplier": 1.3, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 3600, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 13, "disable_rescue_dca": False, "hard_sl_pct": 0.015},
    "BNBUSDT":  {"sl_atr_multiplier": 1.2, "tp_atr_multiplier": 12.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 14, "disable_rescue_dca": False},
    "XRPUSDT":  {"sl_atr_multiplier": 1.3, "tp_atr_multiplier": 12.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 13, "disable_rescue_dca": False, "hard_sl_pct": 0.015},
    "NEARUSDT": {"sl_atr_multiplier": 1.3, "tp_atr_multiplier": 12.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Core_Trend",         "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 13, "disable_rescue_dca": False, "hard_sl_pct": 0.015, "stagnation_base_limit": 7200},

    # 第二類：高彈性動能型 (High_Beta_Momentum) - 高報酬彈性
    # 選幣標準：24h量 > 0.5億、波動率較高（24h ±2%以上）
    "DOGEUSDT": {"sl_atr_multiplier": 1.3, "tp_atr_multiplier": 22.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 11, "disable_rescue_dca": False, "hard_sl_pct": 0.015},
    "ADAUSDT":  {"sl_atr_multiplier": 1.2, "tp_atr_multiplier": 10.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 13, "disable_rescue_dca": False, "hard_sl_pct": 0.015},
    "HYPEUSDT": {"profile_type": "High_Beta_Momentum", "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7, "disable_entry": True},
    "WLDUSDT":  {"sl_atr_multiplier": 1.5, "tp_atr_multiplier": 18.0, "volume_threshold_factor": 1.0, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "High_Beta_Momentum", "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 14, "hard_sl_pct": 0.015, "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7},

    # 第三類：投機風險型 (Speculative_Risk) - DeFi + 中型幣
    # 選幣標準：24h量 > 0.5億、有明確應用場景
    "UNIUSDT":  {"sl_atr_multiplier": 1.2, "tp_atr_multiplier": 12.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 14, "hard_sl_pct": 0.015},
    "AAVEUSDT": {"sl_atr_multiplier": 1.3, "tp_atr_multiplier": 16.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 1.2, "min_flip_time": 1800, "mtf_filter": True,  "profile_type": "Speculative_Risk",   "leverage": 5, "rr_threshold": 2.5, "min_signal_strength": 14, "hard_sl_pct": 0.015},
    }

# 新幣（雷達選入但不在上方設定檔）自動套用此保守設定
# min_signal_strength 拉高到 20.0，跟主力幣的門檻拉近，讓新幣也要有夠強的訊號才進場。
DEFAULT_NEW_COIN_PROFILE = {
    "sl_atr_multiplier": 1.5, "tp_atr_multiplier": 16.0,
    "volume_threshold_factor": 1.3, "breakeven_trigger": 1.2,
    "min_flip_time": 1800, "mtf_filter": True,
    "profile_type": "Speculative_Risk",
    "leverage": 5, "rr_threshold": 2.5,
    "min_signal_strength": 12,
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
VOLUME_RATIO_THRESHOLD = 0.40  # 極度放寬量能門檻至 0.40x
ATR_WARMUP_BATCH_SIZE = 2
ATR_WARMUP_SYMBOL_COUNT = 19
ATR_WARMUP_LIMIT = 1000
ATR_WARMUP_PAUSE_SEC = 0.4
TIME_STOP_MINUTES = 30
 
# 固定交易池：大／中大型市值、合約成交量充足，並保留足夠波動空間。
# 雷達只負責判斷這 15 檔當下是否可交易，不再從全市場替換幣種。
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT",
    "NEARUSDT", "AAVEUSDT", "XLMUSDT", "HYPEUSDT", "ZECUSDT",
]
CONFIG_FILE = get_data_file_path("bot_symbols.json")

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
}

MAX_POSITIONS = 5
COOLDOWN_SEC = 300

DAILY_LOSS_LIMIT_PCT = 0.10

MAIN_LOOP_INTERVAL_SEC = 15
PENDING_CONFIRM_SEC = 2
BAN_WINDOW = 1800          # 縮短至 30 分鐘觀測窗口，更快偵測連續停損
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 2    # 30 分鐘內觸發 2 次停損就封禁（原 3 次）
SL_ATR_MULTIPLIER = 2.0
TP_ATR_MULTIPLIER = 10.0
HARD_STOP_LOSS_PCT = float(os.getenv("HARD_STOP_LOSS_PCT", "0.035"))
SCALP_MODE = os.getenv("SCALP_MODE", "false").lower() in ("true", "1", "yes")
SCALP_TP1_PCT = float(os.getenv("SCALP_TP1_PCT", "0.005"))
SCALP_TP2_PCT = float(os.getenv("SCALP_TP2_PCT", "0.010"))
MIN_TREND_ADX = float(os.getenv("MIN_TREND_ADX", "18.0"))
EXIT_RR_MULTIPLIER = 2.5

MIN_PROFIT_LOCK_THRESHOLD = 0.008
PROTECTED_PROFIT_FLOOR   = 0.0025
MOMENTUM_EXIT_ATR_THRESHOLD = float(os.getenv('MOMENTUM_EXIT_ATR_THRESHOLD', 10.0))
MOMENTUM_EXIT_MIN_PROFIT_PCT = float(os.getenv('MOMENTUM_EXIT_MIN_PROFIT_PCT', 0.01))
HIGH_POINT_STAGNATION_MIN_PROFIT = float(os.getenv('HIGH_POINT_STAGNATION_MIN_PROFIT', 0.0030))
HIGH_POINT_STAGNATION_TIME = int(os.getenv('HIGH_POINT_STAGNATION_TIME', 300))
MIN_STAGNATION_TIME = int(os.getenv('MIN_STAGNATION_TIME', 60))
TREND_PERSISTENCE_WINDOW  = 300
PRICE_MOVEMENT_THRESHOLD  = 0.0015

# Entry & Radar Thresholds
ENTRY_SURGE_THRESHOLD = float(os.getenv("ENTRY_SURGE_THRESHOLD", 0.2))  # 統一 0.20x（原 0.70x）
# MA7／MA25 交叉後至少要拉開 0.005%，避免均線仍黏合時把一次跳動誤認成方向成立。
MA_CROSS_MIN_GAP_PCT = float(os.getenv("MA_CROSS_MIN_GAP_PCT", 0.00005))
MIN_ATR_PCT_FOR_ENTRY = float(os.getenv("MIN_ATR_PCT_FOR_ENTRY", 0.3))   # 下修至 0.3%，放行 BTC/ETH/BNB 穩健藍籌資產
MAX_ATR_PCT_FOR_ENTRY = float(os.getenv("MAX_ATR_PCT_FOR_ENTRY", 5.0))
MIN_1H_VOL_PCT_FOR_ENTRY = float(os.getenv("MIN_1H_VOL_PCT_FOR_ENTRY", 0.30))
MAX_1H_VOL_PCT_FOR_ENTRY = float(os.getenv("MAX_1H_VOL_PCT_FOR_ENTRY", 2.8))
MAX_ATR_PCT_FOR_MA_ENTRY = float(os.getenv("MAX_ATR_PCT_FOR_MA_ENTRY", 8.0))
MAX_1H_VOL_PCT_FOR_MA_ENTRY = float(os.getenv("MAX_1H_VOL_PCT_FOR_MA_ENTRY", 3.5))
MAX_ATR_PCT_FOR_RANGE_ENTRY = float(os.getenv("MAX_ATR_PCT_FOR_RANGE_ENTRY", 15.0))
MAX_1H_VOL_PCT_FOR_RANGE_ENTRY = float(os.getenv("MAX_1H_VOL_PCT_FOR_RANGE_ENTRY", 3.5))
MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY = float(os.getenv("MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY", 14.0))

TAKER_FEE_RATE = 0.0005
ROUND_TRIP_FEE_PCT = TAKER_FEE_RATE * 2
MIN_5M_ATR_PCT_FOR_MA_ENTRY = float(os.getenv("MIN_5M_ATR_PCT_FOR_MA_ENTRY", 0.0005))
# 虧損出場後避免同一幣種立刻沿用已失效的同方向訊號再次進場；個別幣種仍可覆蓋。
DEFAULT_LOSS_REENTRY_COOLDOWN_SEC = int(os.getenv("DEFAULT_LOSS_REENTRY_COOLDOWN_SEC", 1800))

# 全域調整：進場方式改回 7dceb33 的 auto 模式，依訊號強度自動選 pullback/chase/market
# （原本被改成強制全部用 pullback，不管訊號多強都要等拉回才進場，實測 AVAXUSDT
# 因此連續 5 次守門失敗才等到拉回，等到的時候價格已經跑掉，追價滑價 0.44%。
# auto 模式讓強訊號直接市價成交、不用等，只有弱訊號才值得耐心等拉回。這是純粹的
# 執行風險管理邏輯，不是 demo 環境專屬，真正上線一樣適用，一併採用 7dceb33 的門檻）。
ENTRY_ORDER_MODE = os.getenv("ENTRY_ORDER_MODE", "auto").lower()
ENTRY_PULLBACK_ATR_MULT = float(os.getenv("ENTRY_PULLBACK_ATR_MULT", 0.16))
ENTRY_CHASE_OFFSET_PCT = float(os.getenv("ENTRY_CHASE_OFFSET_PCT", 0.0005))
ENTRY_ORDER_MODE_AUTO_STRONG = float(os.getenv("ENTRY_ORDER_MODE_AUTO_STRONG", 12.0))
ENTRY_ORDER_MODE_AUTO_MARKET = float(os.getenv("ENTRY_ORDER_MODE_AUTO_MARKET", 22.0))

ENTRY_STRICTNESS_MODE = os.getenv("ENTRY_STRICTNESS_MODE", "relaxed").lower()
ENTRY_STRICTNESS_PROFILES = {
    "relaxed": {
        "volume_ratio": 0.25,
        "pin_threshold": 3.2,
        "min_body_ratio": 0.08,
        "min_signal_strength": 5.0,
        "rsi_long_floor": 10.0,
        "rsi_short_floor": 10.0,
        "rsi_long_ceiling": 95.0,
        "rsi_short_ceiling": 90.0,
        "min_entry_strength": 3.0,
    },
    "balanced": {
        "volume_ratio": 0.40,
        "pin_threshold": 2.0,
        "min_body_ratio": 0.25,
        "min_signal_strength": 8.0,
        "rsi_long_floor": 20.0,
        "rsi_short_floor": 20.0,
        "rsi_long_ceiling": 88.0,
        "rsi_short_ceiling": 82.0,
        "min_entry_strength": 8.0,
    },
    "strict": {
        "volume_ratio": 0.50,
        "pin_threshold": 1.5,
        "min_body_ratio": 0.35,
        "min_signal_strength": 12.0,
        "rsi_long_floor": 28.0,
        "rsi_short_floor": 25.0,
        "rsi_long_ceiling": 80.0,
        "rsi_short_ceiling": 75.0,
        "min_entry_strength": 10.0,
    },
}

def get_entry_strictness_profile(mode=None):
    mode_name = (mode or ENTRY_STRICTNESS_MODE).lower()
    return ENTRY_STRICTNESS_PROFILES.get(mode_name, ENTRY_STRICTNESS_PROFILES["balanced"])

# 是否啟用 BTC 大盤過濾鎖定小幣開倉（True=啟用鎖定，False=小幣走自己獨立行情）
USE_BTC_MACRO_FILTER = os.getenv("USE_BTC_MACRO_FILTER", "true").lower() in ("1", "true", "yes", "on")

# 市場資料分批抓取：將所有監控幣種分成此數量的批次，fetch_all_klines 每輪抓一個批次
# （輪替），降低每輪瞬間送出請求量，避免衝高幣安 API 權重
MARKET_FETCH_BATCHES = int(os.getenv('MARKET_FETCH_BATCHES', '6'))
# 控制同時對交易所發出的併發請求數（Semaphore 大小）
REQUEST_SEMAPHORE_SIZE = int(os.getenv('REQUEST_SEMAPHORE_SIZE', '1'))
# 批次之間的額外延遲（秒），需要時可拉開批次間隔
KLINE_BATCH_PAUSE_SEC = float(os.getenv("KLINE_BATCH_PAUSE_SEC", "0.2"))
# REST 成交流只作輔助動能判斷，降低輪詢頻率並縮小回傳筆數可大幅減少 API 權重。
TRADE_POLL_INTERVAL_SEC = float(os.getenv("TRADE_POLL_INTERVAL_SEC", "60"))
# 減少每次獲取的成交明細數量以節省權重
TRADE_POLL_LIMIT = int(os.getenv("TRADE_POLL_LIMIT", "10"))
API_RATE_LIMIT_COOLDOWN_SEC = float(os.getenv("API_RATE_LIMIT_COOLDOWN_SEC", "60"))
