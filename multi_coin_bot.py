import asyncio
import ccxt.pro as ccxtpro
import numpy as np
import json
import os
import time
import sys
import uuid
import fcntl
from dotenv import load_dotenv
from services.utils import paper_key
from update_paper_state import update_paper_state

load_dotenv()

exchange = ccxtpro.binance({
    'apiKey': os.getenv('BINANCE_API_KEY') or None,
    'secret': os.getenv('BINANCE_API_SECRET') or None,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',  # 強制使用 USDT-M 永續合約 (fapi)
    },
})
USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
PAPER_TRADING = True
TIMEFRAME = '1m'
LEVERAGE = 5

if USE_TESTNET:
    exchange.urls['api']['fapiPublic'] = 'https://testnet.binancefuture.com/fapi/v1'
    exchange.urls['api']['fapiPrivate'] = 'https://testnet.binancefuture.com/fapi/v1'

DEFAULT_SYMBOLS = [
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT",
    "DOTUSDT", "UNIUSDT", "NEARUSDT", "FETUSDT", "SUIUSDT"
]
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "bot_symbols.json")


def normalize_symbol(sym):
    if sym is None:
        return ""
    sym = str(sym).strip().upper()
    if not sym:
        return ""
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    return sym


def normalize_symbol_list(symbols, max_count=10):
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


def load_symbol_pool():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return normalize_symbol_list(data.get("symbols", []))
        return normalize_symbol_list(data)
    except FileNotFoundError:
        return list(DEFAULT_SYMBOLS)
    except Exception:
        return list(DEFAULT_SYMBOLS)


def save_symbol_pool(symbols):
    normalized = normalize_symbol_list(symbols)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"symbols": normalized}, f, ensure_ascii=False)
    return normalized


ALL_SYMBOLS = load_symbol_pool()

MAX_POSITIONS = 2
COOLDOWN_SEC = 300
MAIN_LOOP_INTERVAL_SEC = 6
PENDING_CONFIRM_SEC = 2
BAN_WINDOW = 3600
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 3
SL_ATR_MULTIPLIER = 4.0
TP_ATR_MULTIPLIER = 0.8
HARD_STOP_LOSS_PCT = 0.10

def build_symbol_state(sym):
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
        "vol_ma20": 0.0,
        "current_vol": 0.0,
        "trailing_highest": 0.0,
        "trailing_lowest": float('inf'),
        "highest_profit_pct": 0.0,
        "has_partial_closed": False,
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
        "entry_cooldown_sec": 90,
        "last_entry_time": 0.0,
    }

STATES = {sym: build_symbol_state(sym) for sym in ALL_SYMBOLS}
WATCH_TASKS = {}

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

# ── 狀態管理 ──────────────────────────────────────────────────

def get_active_count():
    return sum(1 for s in STATES.values() if s["status"] == "ACTIVE")

def get_open_position_count():
    return sum(1 for s in STATES.values() if abs(s["qty"]) > 0.000001)

def get_open_symbols():
    return [sym for sym in ALL_SYMBOLS if abs(STATES[sym]["qty"]) > 0.000001]


def is_symbol_locked(sym):
    s = STATES[sym]
    return abs(s["qty"]) > 0.000001 or s["entry_count"] > 0 or s["open_time"] > 0 or s["status"] in ("COOLDOWN", "BANNED")


def apply_symbol_pool_change(requested_symbols):
    global ALL_SYMBOLS
    desired = normalize_symbol_list(requested_symbols)
    locked_symbols = [sym for sym in ALL_SYMBOLS if is_symbol_locked(sym)]

    new_symbols = []
    used = set()
    target_count = min(10, max(len(desired), len(ALL_SYMBOLS)))

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


def get_balance():
    if not PAPER_TRADING:
        return 150.0
    try:
        with open("paper_state.json", "r") as f:
            state = json.load(f)
            return float(state.get("balance_usdt", 150.0))
    except:
        return 150.0

def compute_per_coin_margin():
    balance = get_balance()
    open_count = get_open_position_count()
    remaining_slots = MAX_POSITIONS - open_count
    if remaining_slots <= 0:
        return 0
    usable = balance * LEVERAGE * 0.95
    per_slot = usable / MAX_POSITIONS
    return per_slot

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

def mark_exit(sym, is_stop_loss=False):
    s = STATES[sym]
    now = time.time()
    s["status"] = "COOLDOWN"
    s["next_status_time"] = now + COOLDOWN_SEC
    s["status_reason"] = "冷卻中 (5分鐘)"
    print(f"⏳ [狀態] {sym} 平倉 → COOLDOWN 5分鐘")
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
    s["last_entry_time"] = 0.0

# ── 資料獲取 ──────────────────────────────────────────────────

async def fetch_all_klines():
    tasks = {}
    for sym in ALL_SYMBOLS:
        tasks[sym] = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=30)
    results = await asyncio.gather(*[tasks[sym] for sym in ALL_SYMBOLS], return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym]["ohlcv"] = results[i]
            STATES[sym]["close_price"] = results[i][-1][4]
        else:
            print(f"⚠️ [K線獲取失敗] {sym}: {results[i]}")

async def fetch_sma200_1h(sym):
    try:
        ohlcv = await exchange.fetch_ohlcv(sym, '1h', limit=200)
        closes = np.array([x[4] for x in ohlcv])
        return float(np.mean(closes))
    except Exception as e:
        print(f"⚠️ [SMA200獲取失敗] {sym}: {e}")
        return 0.0

async def fetch_all_sma200():
    tasks = {sym: fetch_sma200_1h(sym) for sym in ALL_SYMBOLS}
    results = await asyncio.gather(*[tasks[sym] for sym in tasks], return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym]["sma200_1h"] = results[i]

async def load_open_positions():
    if not PAPER_TRADING:
        return
    try:
        with open("paper_state.json", "r") as f:
            state = json.load(f)
        for sym in ALL_SYMBOLS:
            pk = paper_key(sym)
            pos = state.get("positions", {}).get(pk, {})
            qty = float(pos.get("qty", 0.0))
            if abs(qty) > 0.000001:
                STATES[sym]["qty"] = qty
                STATES[sym]["avg_price"] = float(pos.get("avg_price", 0.0))
    except:
        pass

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
        if len(s["atr_history"]) > 20:
            s["atr_history"] = s["atr_history"][-20:]
        s["atr_ma20"] = float(np.mean(s["atr_history"])) if len(s["atr_history"]) >= 20 else s["current_atr"]
    if len(closes) > 14:
        deltas = np.diff(closes[-15:])
        gains = deltas[deltas > 0].mean() if np.any(deltas > 0) else 1e-10
        losses = -deltas[deltas < 0].mean() if np.any(deltas < 0) else 1e-10
        rs = gains / losses
        s["current_rsi"] = 100.0 - (100.0 / (1.0 + rs))
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

def update_trailing_take_profit(sym, current_price, is_long):
    s = STATES[sym]
    avg_price = s["avg_price"]
    if avg_price <= 0:
        return False, 0.0

    if s.get("trail_tp_price", 0.0) <= 0:
        if is_long:
            s["trail_tp_price"] = avg_price * 1.003
        else:
            s["trail_tp_price"] = avg_price * 0.997

    if is_long:
        if current_price <= s["trail_tp_price"]:
            return True, s["trail_tp_price"]
        new_tp = current_price * 0.997
        s["trail_tp_price"] = min(s["trail_tp_price"], new_tp)
        return False, s["trail_tp_price"]

    if current_price >= s["trail_tp_price"]:
        return True, s["trail_tp_price"]
    new_tp = current_price * 1.003
    s["trail_tp_price"] = max(s["trail_tp_price"], new_tp)
    return False, s["trail_tp_price"]


def should_recover_from_reversal(sym, is_long):
    s = STATES[sym]
    if abs(s["qty"]) < 0.000001:
        return False

    macd_reversal = (is_long and s["prev_macd_line"] > s["prev_macd_signal"] and s["macd_line"] < s["macd_signal"]) or \
                    (not is_long and s["prev_macd_line"] < s["prev_macd_signal"] and s["macd_line"] > s["macd_signal"])

    breakout_confirmed = False
    if s["prev_close"] and len(s["ohlcv"]) >= 1:
        prev_bar_idx = -2 if len(s["ohlcv"]) >= 2 else -1
        prev_bar_high = s["ohlcv"][prev_bar_idx][2]
        prev_bar_low = s["ohlcv"][prev_bar_idx][3]
        break_high = s["close_price"] > s["prev_close"] and s["close_price"] >= prev_bar_high
        break_low = s["close_price"] < s["prev_close"] and s["close_price"] <= prev_bar_low
        breakout_confirmed = (is_long and break_low) or (not is_long and break_high)

    volume_confirmed = s["current_vol"] > s["vol_ma20"] * 2.0
    trade_signal = s.get("trade_signal_strength", 0.0)
    trade_confirmed = trade_signal >= 1.5

    if macd_reversal and breakout_confirmed and (volume_confirmed or trade_confirmed):
        return True

    return False


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

    # 1) 即時成交流監聽：大額成交且價格急速變動，優先判定為突破反轉
    trade_signal = s.get("trade_signal_strength", 0.0)
    if trade_signal >= 1.1:
        return "BREAKOUT_REVERSAL", f"即時大額成交異常 {s['trade_signal_reason']}"

    # 2) 簡化的大單/突發行情判斷：放量且價格急速變動
    volume_surge = s["current_vol"] > s["vol_ma20"] * 2.5
    price_jump = abs(current_price - s["prev_close"]) / max(s["prev_close"], 1e-8) > 0.01 if s["prev_close"] else False
    if volume_surge and price_jump:
        return "BREAKOUT_REVERSAL", "放量突發且價格急速變動"

    # 2) 盤整市場：價格被壓縮在狹窄區間內，且 ATR 也偏小
    is_ranging = range_width_pct < 0.025 and atr_pct < 0.015
    if is_ranging:
        profit_pct = (current_price - avg_price) / avg_price if is_long else (avg_price - current_price) / avg_price
        if profit_pct >= 0.003:
            return "RANGE_PROFIT_TAKE", f"盤整區間內已獲利 {profit_pct * 100:.2f}%"

    return "HOLD", "未達出場條件"

async def close_position(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = STATES[sym]
    pk = paper_key(sym)
    qty = min(abs(qty), abs(s["qty"]))
    if qty < 0.000001:
        return
    close_qty = qty if close_side == 'sell' else -qty
    if PAPER_TRADING:
        if s["qty"] > 0:
            pnl = (price - avg_price) * qty
        else:
            pnl = (avg_price - price) * qty
        update_paper_state(pk, close_side, price, qty, is_close=True, pnl=pnl)
    else:
        try:
            await exchange.create_order(sym, type='market', side=close_side, amount=qty,
                                        params={'reduceOnly': True, 'marginMode': 'isolated'})
        except Exception as e:
            print(f"🚨 [平倉錯誤] {sym}: {e}")
            return
    remaining = abs(s["qty"]) - qty
    if remaining < 0.01:
        if remaining > 0.000001:
            print(f"🧹 [塵埃清理] {sym} 剩餘 {remaining:.6f} 視為已清")
        mark_exit(sym, is_stop_loss=is_stop_loss)
        reset_coin_state(sym)
    else:
        s["qty"] = (abs(s["qty"]) - qty) * (1 if s["qty"] > 0 else -1)
        print(f"✅ [部分平] {sym} 平{qty} 剩{abs(s['qty']):.4f} {reason}")

async def check_exits(sym):
    s = STATES[sym]
    if abs(s["qty"]) < 0.000001 or s["avg_price"] <= 0:
        return
    hold_sec = time.time() - s["open_time"] if s["open_time"] > 0 else 9999
    if hold_sec < 10:
        return
    if hold_sec < 60 and profit_pct < -0.002:
        cs = 'sell' if is_long else 'buy'
        print(f"🛑 [初期止損] {sym} 剛進場即反向，縮到最小損失")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="初期止損", is_stop_loss=True)
        return
    sl_mult = SL_ATR_MULTIPLIER * 2 if hold_sec < 120 else SL_ATR_MULTIPLIER
    p = s["close_price"]
    avg = s["avg_price"]
    is_long = s["qty"] > 0
    atr_val = s["current_atr"] if s["current_atr"] > 0 else (p * 0.01)

    profit_pct = (p - avg) / avg if is_long else (avg - p) / avg
    if profit_pct > s["highest_profit_pct"]:
        s["highest_profit_pct"] = profit_pct
    if profit_pct < 0:
        s["has_been_negative"] = True

    regime_decision, regime_reason = detect_market_regime(sym, p, avg, is_long)
    if regime_decision == "BREAKOUT_REVERSAL":
        cs = 'sell' if is_long else 'buy'
        print(f"🚨 [市場 regime] {sym} {regime_reason}，立即平倉並反手")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="市場反轉/大單突破", is_stop_loss=True)
        s["highest_profit_pct"] = 0.0
        return

    if should_recover_from_reversal(sym, is_long):
        recovery_side = 'sell' if is_long else 'buy'
        print(f"🔄 [反向補救] {sym} 方向錯誤且出現反轉訊號，直接反手")
        await close_position(sym, recovery_side, abs(s["qty"]), p, avg, reason="反轉補救", is_stop_loss=True)
        s["highest_profit_pct"] = 0.0
        return
    if regime_decision == "RANGE_PROFIT_TAKE":
        cs = 'sell' if is_long else 'buy'
        print(f"📈 [盤整獲利] {sym} {regime_reason}，提前獲利了結")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="盤整獲利了結")
        s["highest_profit_pct"] = 0.0
        return

    # 動能衰減檢查：利潤溜滑梯
    s["pnl_history"].append(profit_pct * 100)
    if len(s["pnl_history"]) > 5:
        s["pnl_history"].pop(0)
    if len(s["pnl_history"]) == 5:
        is_decaying = all(s["pnl_history"][i] > s["pnl_history"][i+1] for i in range(4))
        if is_decaying and profit_pct * 100 > 0.5:
            cs = 'sell' if is_long else 'buy'
            print(f"📉 [動能衰減] {sym} 利潤連5次下滑 ({profit_pct*100:.2f}%)，即時出場")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="動能衰減")
            s["highest_profit_pct"] = 0.0
            return
    if p > s["trailing_highest"]:
        s["trailing_highest"] = p
    if p < s["trailing_lowest"]:
        s["trailing_lowest"] = p

    # ── 三叉決策樹出場邏輯 ─────────────────────────────────────
    # 1. 趨勢反轉：MACD 反向交叉 → 立即認賠出場 (無視盈虧)
    m_death = s["prev_macd_line"] > s["prev_macd_signal"] and s["macd_line"] < s["macd_signal"]
    m_golden = s["prev_macd_line"] < s["prev_macd_signal"] and s["macd_line"] > s["macd_signal"]
    if (is_long and m_death) or (not is_long and m_golden):
        cs = 'sell' if is_long else 'buy'
        print(f"📉 [反轉出場] {sym} MACD反向交叉，立即平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="趨勢反轉", is_stop_loss=True)
        return

    # 2. 判斷市場狀態：強勢 / 弱勢
    is_strong = (is_long and s["current_rsi"] > 50) or (not is_long and s["current_rsi"] <= 50)

    # ATR TP/SL 價格 (兩條路線共用)
    if is_long:
        tp = avg + max(atr_val * TP_ATR_MULTIPLIER, avg * 0.003)
        sl = avg - (atr_val * sl_mult)
    else:
        tp = avg - max(atr_val * TP_ATR_MULTIPLIER, avg * 0.003)
        sl = avg + (atr_val * sl_mult)

    # 見好就收：先以 0.3% 進行停利，若後續價格再創新高，就把停利線往上移動
    early_take_profit_pct = 0.003
    if s["highest_profit_pct"] >= early_take_profit_pct and profit_pct >= 0.001:
        should_exit, trail_tp = update_trailing_take_profit(sym, p, is_long)
        if should_exit:
            cs = 'sell' if is_long else 'buy'
            print(f"🎯 [移動停利] {sym} 已達到 0.3% 目標，按移動停利出場")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="移動停利")
            s["highest_profit_pct"] = 0.0
            return

    if not is_strong:
        # ── 盤整／弱勢路線 ────────────────────────────────
        # 僵局一階：時間到 + 利潤微薄 → 平50%
        stagnation_limit = get_dynamic_stagnation_limit(s["current_atr"], s["atr_ma20"])
        if not s["has_partial_closed"] and hold_sec > stagnation_limit and 0.002 <= profit_pct < 0.005:
            half = abs(s["qty"]) * 0.5
            cs = 'sell' if is_long else 'buy'
            print(f"⏳ [僵局一階] {sym} 持倉{stagnation_limit//60}分利潤{profit_pct*100:.2f}%，平50%")
            await close_position(sym, cs, half, p, avg, reason="僵局一階")
            s["has_partial_closed"] = True
            return
        # 僵局二階：平過50% + 8分仍未突破1% → 全平
        if s["has_partial_closed"] and hold_sec > 480 and profit_pct < 0.01:
            cs = 'sell' if is_long else 'buy'
            print(f"⏳ [僵局二階] {sym} 剩餘50%持倉8分仍未突破1%，全平")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="僵局二階")
            s["highest_profit_pct"] = 0.0
            s["has_partial_closed"] = False
            return
        # 弱勢快速停利：曾虧轉盈 → 0.3% 就走；否則 0.5%
        weak_tp = 0.003 if s["has_been_negative"] else 0.005
        if s["highest_profit_pct"] >= weak_tp:
            cs = 'sell' if is_long else 'buy'
            label = f"曾負轉正{weak_tp*100:.1f}%" if s["has_been_negative"] else f"弱勢{weak_tp*100:.1f}%"
            print(f"🎯 [{label}] {sym} 弱勢利潤達{weak_tp*100:.1f}%")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="快速停利")
            s["highest_profit_pct"] = 0.0
            return
        # 弱勢 ATR 停損直接觸發 (停利在弱勢下先抓快速停利)
        if (is_long and p <= sl) or (not is_long and p >= sl):
            cs = 'sell' if is_long else 'buy'
            sl_pct = abs(sl - avg) / avg * 100
            print(f"🛑 [ATR停損] {sym} -{sl_pct:.1f}%")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="ATR停損", is_stop_loss=True)
            return
    else:
        # ── 強勢路線 ────────────────────────────────────
        # 強勢動態停利：高點回撤 0.5%
        if s["highest_profit_pct"] >= 0.01:
            if (is_long and p <= s["trailing_highest"] * 0.995) or (not is_long and p >= s["trailing_lowest"] * 1.005):
                cs = 'sell' if is_long else 'buy'
                print(f"🏃 [動態停利] {sym} 強勢回撤0.5%")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="動態停利")
                s["highest_profit_pct"] = 0.0
                return
        # 強勢 ATR TP/SL
        if (is_long and p >= tp) or (not is_long and p <= tp):
            cs = 'sell' if is_long else 'buy'
            tp_pct = abs(tp - avg) / avg * 100
            print(f"🎯 [ATR停利] {sym} {tp_pct:.1f}%")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="ATR停利")
            return
        if (is_long and p <= sl) or (not is_long and p >= sl):
            cs = 'sell' if is_long else 'buy'
            sl_pct = abs(sl - avg) / avg * 100
            print(f"🛑 [ATR停損] {sym} -{sl_pct:.1f}%")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="ATR停損", is_stop_loss=True)
            return

async def check_position_exits(sym):
    s = STATES[sym]
    if abs(s["qty"]) < 0.000001:
        return
    p = s["close_price"]
    avg = s["avg_price"]
    is_long = s["qty"] > 0
    profit_pct = (p - avg) / avg if is_long else (avg - p) / avg
    hold_sec = time.time() - s["open_time"] if s["open_time"] > 0 else 9999

    if hold_sec < 120:
        return

    # 10% 硬停損
    hard_sl = avg * HARD_STOP_LOSS_PCT
    if (is_long and p <= avg - hard_sl) or (not is_long and p >= avg + hard_sl):
        cs = 'sell' if is_long else 'buy'
        print(f"⛔ [10%停損] {sym}")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="10%停損", is_stop_loss=True)
        return

    # 3x ATR 停利
    tp_dist = s["current_atr"] * 3.0
    if (is_long and p >= avg + tp_dist) or (not is_long and p <= avg - tp_dist):
        cs = 'sell' if is_long else 'buy'
        print(f"🎯 [ATR停利K線] {sym}")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="ATR停利K線")
        return

    # MACD 反轉搶救 (已在 check_exits 三叉樹中處理，此處保留 10% 硬停損與 3x ATR 停利)
        await close_position(sym, 'sell', abs(s["qty"]), p, avg, reason="反轉搶救", is_stop_loss=True)
        return

    # 15分鐘時間停損
    if hold_sec > 900 and profit_pct < -0.01:
        cs = 'sell' if is_long else 'buy'
        print(f"⏱️ [時間停損] {sym} {hold_sec/60:.1f}分仍虧損")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="時間停損", is_stop_loss=True)
        return

# ── 進場邏輯 ──────────────────────────────────────────────────

async def execute_order(sym, side, price):
    s = STATES[sym]
    pk = paper_key(sym)
    margin = compute_per_coin_margin()
    if margin <= 0:
        print(f"⚠️ [風控] {sym} 無可用保證金")
        return

    now = time.time()
    if side == 'buy' and s["entry_count"] > 0:
        if now - s["last_entry_time"] < s["entry_cooldown_sec"]:
            print(f"⏳ [加倉冷卻] {sym} 距離上次加倉不足 {s['entry_cooldown_sec']} 秒")
            return
        if s["entry_count"] >= s["max_additional_entries"]:
            print(f"⚠️ [加倉上限] {sym} 已達最大加倉次數")
            return
        if s["avg_price"] > 0 and s["close_price"] > 0:
            profit_pct = (s["close_price"] - s["avg_price"]) / s["avg_price"]
            if profit_pct < 0.001:
                print(f"🛑 [加倉風控] {sym} 目前尚未回到保本線以上，不加倉")
                return

    base_amt = margin / price
    if base_amt < 0.001:
        print(f"⚠️ [風控] {sym} 數量過小 {base_amt:.6f}")
        return

    if side == 'buy':
        base_amt *= 0.5 if s["entry_count"] == 0 else 0.25
    else:
        base_amt *= 0.5 if s["entry_count"] == 0 else 0.25

    if PAPER_TRADING:
        if side == 'buy':
            s["qty"] += base_amt
        else:
            s["qty"] -= base_amt
        if s["avg_price"] <= 0:
            s["avg_price"] = price
        else:
            s["avg_price"] = ((s["avg_price"] * abs(s["qty"] - base_amt)) + (price * base_amt)) / abs(s["qty"])
        s["open_time"] = now
        s["last_buy_time"] = now
        s["last_entry_time"] = now
        s["entry_count"] += 1
        update_paper_state(pk, side, price, base_amt)
        direction = "做多" if side == 'buy' else "做空"
        print(f"🟢 [{direction}] {sym} {base_amt:.4f} @ {price} (保證金:{margin:.2f} USDT)")
    else:
        try:
            order = await exchange.create_order(sym, type='market', side=side, amount=base_amt,
                                                params={'marginMode': 'isolated'})
            s["qty"] = base_amt if side == 'buy' else -base_amt
            s["avg_price"] = price
            s["open_time"] = now
            s["last_buy_time"] = now
            s["last_entry_time"] = now
            s["entry_count"] += 1
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

    if side == 'buy':
        if close_price <= open_price:
            return False
        if close_price <= prev_close:
            return False
        if upper_wick > body * 2.0 and close_price < prev_close:
            return False
        return True

    if close_price >= open_price:
        return False
    if close_price >= prev_close:
        return False
    if lower_wick > body * 2.0 and close_price > prev_close:
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
    if current_vol < vol_ma20 * 1.5:
        return False
    if side == 'buy':
        return s["close_price"] > s["prev_close"]
    return s["close_price"] < s["prev_close"]


def is_entry_allowed(sym, side):
    s = STATES[sym]
    cp = s["close_price"]
    if s.get("sma200_1h", 0) > 0:
        if side == 'buy' and cp <= s["sma200_1h"]:
            return False
        if side == 'sell' and cp >= s["sma200_1h"]:
            return False
    if len(s["ohlcv"]) < 20:
        return False
    if not is_entry_pin_safe(sym, side):
        print(f"⚠️ [進場濾波] {sym} 遇到反向針線，跳過")
        return False
    if not is_entry_volume_confirmed(sym, side):
        print(f"⚠️ [進場濾波] {sym} 量能未確認，跳過")
        return False
    highs = np.array([x[2] for x in s["ohlcv"]])
    lows = np.array([x[3] for x in s["ohlcv"]])
    closes = np.array([x[4] for x in s["ohlcv"]])
    adx_val = calculate_adx(highs, lows, closes)
    if adx_val < 25:
        return False

    # 目前市場量能普遍低於 20 週期均量，改為在紙面交易中放寬量能門檻，避免信號已生成卻因量能條件被卡住。
    if not PAPER_TRADING:
        min_volume = max(1000.0, s["vol_ma20"] * 0.1)
        if s["current_vol"] < min_volume:
            return False
    return True

def compute_signal_strength(sym):
    s = STATES[sym]
    if len(s["closes"]) < 20:
        return (None, 0)

    rsi = s["current_rsi"]
    close = s["close_price"]
    prev_close = s["prev_close"] if s["prev_close"] is not None else close
    ema20 = s.get("ema20", 0.0)
    ema50 = s.get("ema50", 0.0)

    trend_long = ema20 > 0 and ema50 > 0 and ema20 > ema50
    trend_short = ema20 > 0 and ema50 > 0 and ema20 < ema50

    last_candle_confirmed_long = len(s["ohlcv"]) >= 2 and s["ohlcv"][-1][4] > s["ohlcv"][-2][4]
    last_candle_confirmed_short = len(s["ohlcv"]) >= 2 and s["ohlcv"][-1][4] < s["ohlcv"][-2][4]

    long_cond_rsi = rsi < 35
    long_cond_macd = s["prev_macd_line"] <= s["prev_macd_signal"] and s["macd_line"] > s["macd_signal"]
    short_cond_rsi = rsi > 65
    short_cond_macd = s["prev_macd_line"] >= s["prev_macd_signal"] and s["macd_line"] < s["macd_signal"]

    if trend_long and last_candle_confirmed_long:
        if long_cond_rsi:
            return ("buy", max(10.0, 40 - rsi))
        if long_cond_macd:
            return ("buy", 8.0)

    if trend_short and last_candle_confirmed_short:
        if short_cond_rsi:
            return ("sell", max(10.0, rsi - 60))
        if short_cond_macd:
            return ("sell", 8.0)

    return (None, 0)

async def check_entries():
    open_count = get_open_position_count()
    if open_count >= MAX_POSITIONS:
        return
    remaining_slots = MAX_POSITIONS - open_count

    candidates = []
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s["status"] != "ACTIVE":
            continue
        if abs(s["qty"]) > 0.000001:
            continue
        side_strength = compute_signal_strength(sym)
        if side_strength[0] is None:
            continue
        side, strength = side_strength
        if not is_entry_allowed(sym, side):
            continue
        candidates.append((sym, side, strength))

    if not candidates:
        return

    candidates.sort(key=lambda x: -x[2])
    print(f"📊 [訊號排行] {' | '.join(f'{sym}:{side}({strength:.2f})' for sym, side, strength in candidates[:3])}")

    for i in range(min(remaining_slots, len(candidates))):
        sym, side, _ = candidates[i]
        s = STATES[sym]
        now = time.time()
        if s["pending_side"] != side:
            s["pending_side"] = side
            s["pending_time"] = now
            m_line, m_sig, _, _, _ = calculate_macd(s["closes"])
            macd_triggered = (s["prev_macd_line"] <= s["prev_macd_signal"] and m_line > m_sig) or \
                             (s["prev_macd_line"] >= s["prev_macd_signal"] and m_line < m_sig)
            if macd_triggered and len(s["ohlcv"]) >= 2:
                s["pending_confirm_high"] = s["ohlcv"][-2][2]
                s["pending_confirm_low"] = s["ohlcv"][-2][3]
            continue
        if now - s["pending_time"] >= PENDING_CONFIRM_SEC:
            if side == 'buy' and s["pending_confirm_high"] > 0 and s["close_price"] <= s["pending_confirm_high"]:
                continue
            if side == 'sell' and s["pending_confirm_low"] > 0 and s["close_price"] >= s["pending_confirm_low"]:
                continue
            await execute_order(sym, side, s["close_price"])
            s["pending_side"] = None
            s["pending_confirm_high"] = 0
            s["pending_confirm_low"] = 0

# ── 主循環 ──────────────────────────────────────────────────

async def watch_symbol_trades(sym):
    while True:
        try:
            trades = await exchange.watch_trades(sym)
            if isinstance(trades, list):
                for trade in trades:
                    update_trade_signal(sym, trade)
            elif trades:
                update_trade_signal(sym, trades)
        except Exception as e:
            print(f"⚠️ [成交流監聽異常] {sym}: {e}")
            await asyncio.sleep(3)


async def ensure_watch_tasks():
    global WATCH_TASKS
    desired_symbols = set(ALL_SYMBOLS)
    current_symbols = set(WATCH_TASKS.keys())

    for sym in current_symbols - desired_symbols:
        task = WATCH_TASKS.pop(sym, None)
        if task is not None:
            task.cancel()

    for sym in desired_symbols - current_symbols:
        WATCH_TASKS[sym] = asyncio.create_task(watch_symbol_trades(sym))


async def watch_all_trades():
    while True:
        try:
            await ensure_watch_tasks()
            await asyncio.sleep(5)
        except Exception as e:
            print(f"❌ [成交流監聽錯誤] {e}")
            await asyncio.sleep(3)


async def main_loop():
    print("🚀 [多幣輪動] 啟動多幣種輪動交易機器人")
    print(f"📋 監控幣種: {', '.join(ALL_SYMBOLS)}")
    print(f"📊 最大同時持倉: {MAX_POSITIONS}")
    print(f"📡 模式: {'模擬' if PAPER_TRADING else '實盤'}")
    print(f"@@LEVERAGE@@{LEVERAGE}")
    await load_open_positions()
    await fetch_all_sma200()

    while True:
        try:
            loop_start = time.time()
            if ALL_SYMBOLS != load_symbol_pool():
                apply_symbol_pool_change(load_symbol_pool())
            await ensure_watch_tasks()
            await fetch_all_klines()
            for sym in ALL_SYMBOLS:
                compute_indicators(sym)
            update_states()
            for sym in ALL_SYMBOLS:
                await check_exits(sym)
            for sym in ALL_SYMBOLS:
                await check_position_exits(sym)
            await check_entries()
            elapsed = time.time() - loop_start
            sleep_time = max(3, MAIN_LOOP_INTERVAL_SEC - elapsed)
            await asyncio.sleep(sleep_time)
        except Exception as e:
            import traceback
            print(f"❌ [主循環錯誤] {e}")
            traceback.print_exc()
            await asyncio.sleep(3)

async def periodic_sma200_update():
    while True:
        await asyncio.sleep(3600)
        await fetch_all_sma200()
        print("🔄 [SMA200] 已更新所有幣種1h SMA200")

async def periodic_status_log():
    while True:
        await asyncio.sleep(60)
        active = sum(1 for s in STATES.values() if s["status"] == "ACTIVE")
        cooldown = sum(1 for s in STATES.values() if s["status"] == "COOLDOWN")
        banned = sum(1 for s in STATES.values() if s["status"] == "BANNED")
        open_syms = get_open_symbols()
        open_str = ', '.join(f"{sym}({'多' if STATES[sym]['qty']>0 else '空'})" for sym in open_syms) if open_syms else "無"
        print(f"📊 [狀態] ACTIVE={active} COOLDOWN={cooldown} BANNED={banned} | 持倉({len(open_syms)}): {open_str}")

async def main():
    await asyncio.gather(
        main_loop(),
        periodic_sma200_update(),
        periodic_status_log(),
        watch_all_trades(),
    )

if __name__ == "__main__":
    asyncio.run(main())
