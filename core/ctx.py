import asyncio
from core.config import COIN_PROFILE_CONFIG

ALL_SYMBOLS = []
CACHE = None
STATES = {}
MARKET_WIND = {
    "btc_trend": "NEUTRAL",
    "allow_long": True,
    "allow_short": True,
    "btc_change_15m": 0.0,
    "eth_change_15m": 0.0,
    "btc_adx_15m": 0.0,
    "is_ranging": False,
    "btc_trend_1h": "NEUTRAL",
    "btc_trend_4h": "NEUTRAL",
    "btc_macro_updated_at": 0.0,
}
PENDING_LIMIT_ORDERS = {}
WATCH_TASKS = {}
UNSUPPORTED_SYMBOLS = set()
CONSECUTIVE_ERRORS = 0
LAST_KLINES_UPDATE = 0.0
LAST_SYMBOL_POOL_SYNC = 0.0
api_cooldown_until = 0.0
request_semaphore = None
# 冷卻期補位：{原幣種: 暫時補入的候補幣種}，讓監控池在冷卻期間維持原本數量
COOLDOWN_SUBSTITUTES = {}


def init_states(symbols=None):
    global ALL_SYMBOLS, request_semaphore
    from core.state_manager import build_symbol_state
    from core.symbol_profile import apply_all_symbol_profiles, load_symbol_config
    from core.config import DEFAULT_SYMBOLS
    if symbols is None:
        try:
            from core.symbol_profile import load_symbol_pool
            symbols = load_symbol_pool()
        except Exception:
            symbols = list(DEFAULT_SYMBOLS)
    ALL_SYMBOLS.extend(symbols)
    _, profiles = load_symbol_config()
    import core.symbol_profile as sp
    # 保留同一個 dict 物件，讓已用 ``from ... import SYMBOL_PROFILES`` 的模組
    # 也能立即看到最新雷達資格；重新賦值會讓那些模組永遠握著舊空表。
    sp.SYMBOL_PROFILES.clear()
    sp.SYMBOL_PROFILES.update(profiles)
    for sym in ALL_SYMBOLS:
        STATES[sym] = build_symbol_state(sym)
    apply_all_symbol_profiles()
    from core.config import REQUEST_SEMAPHORE_SIZE
    request_semaphore = asyncio.Semaphore(max(1, int(REQUEST_SEMAPHORE_SIZE)))
