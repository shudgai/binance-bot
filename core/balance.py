import json
import time
from core.config import (
    PAPER_TRADING, DUAL_SHOT_MAX_SLOTS, DUAL_SHOT_LEVERAGE,
    DAILY_LOSS_LIMIT_PCT, TAKER_FEE_RATE, ROUND_TRIP_FEE_PCT,
)

REAL_BALANCE = 150.0

_DAILY_REALIZED_LOSS = 0.0
_DAILY_LOSS_DATE     = ""
_DAILY_LOSS_HALTED   = False


def _reset_daily_loss_if_new_day():
    global _DAILY_REALIZED_LOSS, _DAILY_LOSS_DATE, _DAILY_LOSS_HALTED
    today = time.strftime("%Y-%m-%d")
    if _DAILY_LOSS_DATE != today:
        if _DAILY_LOSS_DATE:
            print(f"[每日熔斷重置] 新的一天 ({today})，清空昨日虧損累計 ({_DAILY_REALIZED_LOSS:.4f})")
        _DAILY_REALIZED_LOSS = 0.0
        _DAILY_LOSS_DATE = today
        _DAILY_LOSS_HALTED = False


def accrue_daily_realized_pnl(profit_pct: float, position_value: float):
    global _DAILY_REALIZED_LOSS, _DAILY_LOSS_HALTED
    _reset_daily_loss_if_new_day()
    if profit_pct < 0:
        _DAILY_REALIZED_LOSS += profit_pct
        if not _DAILY_LOSS_HALTED and abs(_DAILY_REALIZED_LOSS) >= DAILY_LOSS_LIMIT_PCT:
            _DAILY_LOSS_HALTED = True
            print(f"[每日熔斷] 當日累計虧損已達 {_DAILY_REALIZED_LOSS*100:.2f}% (上限: {DAILY_LOSS_LIMIT_PCT*100:.1f}%)，今日封鎖所有新進場！")


def is_daily_loss_halted() -> bool:
    _reset_daily_loss_if_new_day()
    return _DAILY_LOSS_HALTED


async def fetch_real_balance():
    global REAL_BALANCE
    if PAPER_TRADING:
        return
    from core.exchange_client import exchange_futures
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
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "paper_state.json"), "r") as f:
            state = json.load(f)
            return float(state.get("balance_usdt", 150.0))
    except:
        return 150.0


def compute_per_coin_margin(sym=None, allocation_pct=None):
    balance = get_balance()
    if balance <= 0:
        return 0
    allocated_margin = balance / DUAL_SHOT_MAX_SLOTS
    return allocated_margin * 0.999


def get_fee_overhead(leverage: float = 5.0) -> float:
    return ROUND_TRIP_FEE_PCT * leverage


def get_total_wallet_balance():
    if PAPER_TRADING:
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "paper_state.json"), 'r') as f:
                st = json.load(f)
                return float(st.get("balance_usdt", 150.0))
        except:
            return 150.0
    else:
        return REAL_BALANCE if REAL_BALANCE > 0 else 150.0
