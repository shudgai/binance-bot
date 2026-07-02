"""
core — Binance bot trading engine.

Public API (AI: start here):
  ctx            Global state (MARKET_WIND, STATES[sym], ALL_SYMBOLS)
  config         COIN_PROFILE_CONFIG, leverage/risk constants
  calc           Pure calculation functions (no ctx dependency)

Entry pipeline:
  check_entries  Entry signal orchestration
  entry_filter   Pre-entry gate checks (wick, volume, MACD, trend bias)
  signal_engine  Signal strength scoring, pyramiding, trend-bias gate
  trade_signal   Real-time trade-flow signal aggregation

Exit pipeline:
  exits          All exit path logic (SL, TP, trailing, time-stop, etc.)
  orders         Order execution + position management

Infrastructure:
  exchange_client  ccxt.pro Binance client instances
  market_data      OHLCV/ATR/SMA/EMA fetch + cache
  indicators       Technical indicator calculators
  balance          Balance tracking + daily loss circuit breaker
  state_manager    Symbol state lifecycle (ACTIVE/COOLDOWN/BANNED)
  symbol_profile   Per-coin personality config loading
"""
