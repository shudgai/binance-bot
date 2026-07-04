import asyncio
from core.config import COIN_PROFILE_CONFIG

ALL_SYMBOLS = []
STATES = {}
MARKET_WIND = {
    "btc_trend": "NEUTRAL",
    "allow_long": True,
    "allow_short": True,
    "btc_change_15m": 0.0,
    "eth_change_15m": 0.0,
}
PENDING_LIMIT_ORDERS = {}
WATCH_TASKS = {}
CONSECUTIVE_ERRORS = 0
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
    sp.SYMBOL_PROFILES = profiles
    for sym in ALL_SYMBOLS:
        STATES[sym] = build_symbol_state(sym)
    apply_all_symbol_profiles()
    request_semaphore = asyncio.Semaphore(5)
