import os
import json
import sys
import time
import threading
import subprocess
from services.system_log_service import add_system_log
from core.config import TRADE_POOL_SIZE

# 模擬交易機器人狀態 (支援多幣種多進程)
bot_status = {
    "is_running": False,
    "strategy": "Top 15 Radar / 4 Slots",
    "balance_quote": 150.0,
    "active_orders": 0,
    "active_symbols": [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "NEARUSDT", "UNIUSDT", "AAVEUSDT", "DOGEUSDT", "1000PEPEUSDT"
    ],
    "watch_symbols": [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "NEARUSDT", "UNIUSDT", "AAVEUSDT", "DOGEUSDT", "1000PEPEUSDT"
    ],
    "regime": "多幣種監控中",
    "coin_regimes": {},    # { symbol: regime }
    "trade_amount": 150.0,
    "entry_diagnosis": "等待訊號",
    "entry_diagnoses": {},  # {symbol: {message, updated_at}}
}

bot_processes = {}  # {symbol: subprocess.Popen}
_intentional_stop_processes = set()
_web_log_throttle = {}
ROUTINE_WAIT_LOG_INTERVAL_SEC = 60.0
_restart_order_cache = {"checked_at": 0.0, "orders": None}
RESTART_ORDER_CACHE_SEC = 5.0
from core.config import get_data_file_path

def _get_symbol_config_path():
    return get_data_file_path("bot_symbols.json")

BOT_STATE_PATH = get_data_file_path("bot_running_state.json")
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT",
    "NEARUSDT", "AAVEUSDT", "XLMUSDT", "LTCUSDT", "ZECUSDT",
]


def _strategy_label(balance=None):
    """Keep the status-page slot count aligned with the trading engine."""
    from core.config import SCALP_MODE
    from core.balance import get_dynamic_max_slots

    if SCALP_MODE:
        return "Top 15 Radar / Scalp Micro-Trend (+0.3% TP / -1.5% SL)"

    slots = get_dynamic_max_slots(balance)
    if slots == 3:
        return "Top 15 Radar / 3 MA Trend Slots"
    return f"Top 15 Radar / {slots} Slots"


def _record_entry_diagnosis(message: str, now: float | None = None):
    """Keep the latest diagnosis per symbol instead of one global last-writer value."""
    message = str(message or "").strip()
    symbol, separator, _ = message.partition(":")
    if not separator or not symbol.endswith("USDT"):
        bot_status["entry_diagnosis"] = message or "等待訊號"
        return
    bot_status.setdefault("entry_diagnoses", {})[symbol] = {
        "message": message,
        "updated_at": float(time.time() if now is None else now),
    }


def _summarize_entry_diagnosis(trade_eligibility, now: float | None = None):
    """Prefer current actionable reasons over a later symbol's warm-up message."""
    now = float(time.time() if now is None else now)
    diagnoses = bot_status.get("entry_diagnoses", {})
    eligible_symbols = [
        sym for sym, info in (trade_eligibility or {}).items()
        if bool((info or {}).get("eligible"))
    ]
    recent = []
    actionable = []
    for sym in eligible_symbols:
        record = diagnoses.get(sym) or {}
        message = str(record.get("message") or "").strip()
        updated_at = float(record.get("updated_at", 0.0) or 0.0)
        if not message or now - updated_at > 180.0:
            continue
        recent.append(message)
        if "K 線資料不足" not in message and "指標載入中" not in message:
            actionable.append(message)

    selected = actionable or recent
    if selected:
        return " ｜ ".join(selected[:3])
    return bot_status.get("entry_diagnosis") or "等待訊號"


def _prune_entry_diagnoses(active_symbols):
    """Keep the status payload aligned with the current UI and trading pool."""
    allowed = set(active_symbols or [])
    diagnoses = bot_status.get("entry_diagnoses", {})
    bot_status["entry_diagnoses"] = {
        sym: record for sym, record in diagnoses.items() if sym in allowed
    }


def normalize_symbol(sym):
    if sym is None:
        return ""
    sym = str(sym).strip().upper()
    if not sym:
        return ""
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    return sym


def _should_emit_bot_web_log(text: str, now: float | None = None) -> bool:
    """Throttle identical routine waiting messages without hiding changed diagnostics."""
    text = str(text or "").strip()
    if not (text.startswith("⏳") and "[MA_Strategy]" in text):
        return True
    now = float(time.time() if now is None else now)
    last = float(_web_log_throttle.get(text, 0.0) or 0.0)
    if last > 0 and now - last < ROUTINE_WAIT_LOG_INTERVAL_SEC:
        return False
    _web_log_throttle[text] = now
    return True


def _mark_intentional_stop(pid: int) -> None:
    if not pid:
        return
    from core.sigterm_diagnostic import intentional_stop_marker_path
    marker = intentional_stop_marker_path(pid)
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("expected\n")
    except OSError:
        pass


def normalize_symbol_list(symbols, max_count=23):
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


def _filter_disabled_symbols(symbols):
    from core.config import COIN_PROFILE_CONFIG
    filtered = []
    for sym in symbols:
        if COIN_PROFILE_CONFIG.get(sym, {}).get("disable_entry", False):
            continue
        filtered.append(sym)
    return filtered


def _prioritize_trade_pool(symbols, profiles):
    """Put mature tradable markets before observing and watch-only candidates, preserving Tier 1 bluechips at the top."""
    original_order = {sym: idx for idx, sym in enumerate(symbols)}
    top_tier = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}

    def priority(sym):
        if sym in top_tier:
            return (-1, 0, 0, original_order.get(sym, 999))
        profile = profiles.get(sym) or {}
        return (
            0 if profile.get("_trade_eligible", False) else
            1 if profile.get("_radar_strict_eligible", False) else 2,
            -float(profile.get("_radar_entry_readiness", 0.0) or 0.0),
            float(profile.get("_radar_rank", 9999) or 9999),
            original_order.get(sym, 999),
        )

    return sorted(symbols, key=priority)


def load_symbol_config():
    try:
        with open(_get_symbol_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            symbols = normalize_symbol_list(data.get("symbols", []))
        else:
            symbols = normalize_symbol_list(data)
        symbols = _filter_disabled_symbols(symbols)
        for default_sym in DEFAULT_SYMBOLS:
            if default_sym not in symbols:
                symbols.append(default_sym)
        raw_profiles = data.get("profiles", {}) if isinstance(data, dict) else {}
        return _prioritize_trade_pool(symbols, raw_profiles)[:TRADE_POOL_SIZE]
    except Exception:
        return _filter_disabled_symbols(list(DEFAULT_SYMBOLS))


def load_symbol_profiles():
    try:
        with open(_get_symbol_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw_profiles = data.get("profiles", {})
            if isinstance(raw_profiles, dict):
                normalized_profiles = {}
                for sym, profile in raw_profiles.items():
                    normalized = normalize_symbol(sym)
                    if normalized and isinstance(profile, dict):
                        normalized_profiles[normalized] = profile
                return normalized_profiles
        return {}
    except Exception:
        return {}


def _restore_truncated_radar_pool(symbols):
    """雷達仔保存完整 profiles 時，避免短暫重啟狀態把正式監控池縮成少數幣。

    profiles 會保存雷達排名與交易資格； symbols 偶爾只剰冷卻候補/最後監控幣。
    啟動時只有 symbols 少於 8 檔才視為截斷，並由 profiles 恢復正式 Top 15；
    已有 8 檔以上視為正常自訂池，不擅自覆蓋。
    """
    symbols = normalize_symbol_list(symbols)
    profiles = load_symbol_profiles()
    ranked = [
        sym for sym, profile in sorted(
            profiles.items(),
            key=lambda item: float((item[1] or {}).get("_radar_rank", 9999) or 9999),
        )
        if isinstance(profile, dict)
        and float(profile.get("_radar_atr_pct", 0.0) or 0.0) > 0
    ]
    ranked = _filter_disabled_symbols(normalize_symbol_list(ranked, max_count=25))
    if len(symbols) < 8 and len(ranked) >= 8:
        restored = list(ranked[:TRADE_POOL_SIZE])
        add_system_log(
            f"♻️ [啟動幣池修復] symbols 僅 {len(symbols)} 檔，"
            f"由雷達 profiles 恢復為 {len(restored)} 檔",
            "warning",
        )
        return restored
    return symbols


def load_disabled_symbols():
    try:
        with open(_get_symbol_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return [normalize_symbol(s) for s in data.get("disabled", [])]
        return []
    except Exception:
        return []


def save_symbol_config(symbols):
    normalized = normalize_symbol_list(symbols)
    profiles = load_symbol_profiles()
    disabled = load_disabled_symbols()
    payload = {"symbols": normalized}
    if profiles:
        payload["profiles"] = profiles
    if disabled:
        payload["disabled"] = disabled
    with open(_get_symbol_config_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return normalized


def toggle_coin_disabled(symbol: str) -> dict:
    sym = normalize_symbol(symbol)
    try:
        with open(_get_symbol_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {"symbols": data}
    disabled = [normalize_symbol(s) for s in data.get("disabled", [])]
    if sym in disabled:
        disabled.remove(sym)
        is_disabled = False
    else:
        disabled.append(sym)
        is_disabled = True
    data["disabled"] = disabled
    with open(_get_symbol_config_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    bot_status["disabled_symbols"] = disabled
    action = "暫停" if is_disabled else "恢復"
    add_system_log(f"🔧 [{sym}] 已{action}交易", "info")
    return {"symbol": sym, "disabled": is_disabled, "all_disabled": disabled}


def _get_trade_history_realized_pnl():
    try:
        from core.config import TRADE_HISTORY_FILE
        if os.path.exists(TRADE_HISTORY_FILE):
            with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
            total = 0.0
            for t in history:
                if t.get("is_close") or "net_pnl" in t or "realized_pnl" in t:
                    net = float(t.get("net_pnl", 0.0) or t.get("realized_pnl", 0.0) or 0.0)
                    fee = float(t.get("fees", 0.0) or t.get("fee", 0.0) or 0.0)
                    if "net_pnl" not in t and fee > 0:
                        net -= fee
                    total += net
            return total
    except Exception:
        pass
    return 0.0


def get_bot_status():
    from services.paper_trade_service import get_paper_balance
    from core.config import PAPER_TRADING
    import os
    import json

    # 改用 core.config.PAPER_TRADING（跟實際下單邏輯同一個判斷依據），
    # 不要再看 TRADING_MODE 這個沒被設定過的環境變數，避免切了真實交易後面板還顯示紙上餘額。
    if PAPER_TRADING:
        bot_status["balance_quote"] = get_paper_balance()

        # Calculate total realized PNL from paper_state.json
        try:
            total_realized = 0.0
            total_fees = 0.0
            from core.config import PAPER_STATE_FILE
            state_path = PAPER_STATE_FILE
            if os.path.exists(state_path):
                with open(state_path, "r") as f:
                    state = json.load(f)
                for t in state.get("trades", []):
                    fee = t.get("fee", (t.get("price", 0) * abs(t.get("qty", 0))) * 0.0005)
                    total_fees += fee
                    if t.get("is_close"):
                        pnl = t.get("realized_pnl", 0.0)
                        total_realized += pnl
            bot_status["total_realized_pnl"] = total_realized - total_fees
        except Exception as e:
            bot_status["total_realized_pnl"] = 0.0

        # 單次自動交易金額跟著複利調整（紙上餘額本身就已經是本金+累計損益，
        # 直接拿來當交易金額即可），道理跟下面實盤那段一樣：虧損就縮水、獲利就變大。
        bot_status["trade_amount"] = max(bot_status["balance_quote"], 10.0)
    else:
        try:
            from services.binance_service import get_total_realized_pnl_usdt
            pnl = get_total_realized_pnl_usdt()
            if pnl == 0.0:
                pnl = _get_trade_history_realized_pnl()
            bot_status["total_realized_pnl"] = pnl
        except Exception:
            bot_status["total_realized_pnl"] = _get_trade_history_realized_pnl()
        try:
            from core.config import LIVE_CAPITAL_CAP
            base_amount = LIVE_CAPITAL_CAP if LIVE_CAPITAL_CAP else 150.0
            compounded_amount = max(base_amount + bot_status.get("total_realized_pnl", 0.0), 10.0)
            bot_status["balance_quote"] = compounded_amount
            bot_status["trade_amount"] = compounded_amount
        except Exception:
            pass

    # 槽位數由本金級距動態決定，狀態頁不可再使用寫死的舊值。
    bot_status["strategy"] = _strategy_label(bot_status.get("balance_quote"))

    # 每次都從 bot_symbols.json 讀取最新幣種清單，確保前端即時同步
    try:
        actual_symbols, _ = load_symbol_config()
        # 強制將藍籌巨頭 (BTC, ETH, BNB) 擺放在 active_symbols 清單的最前面
        ordered_symbols = []
        for maj in ("BTCUSDT", "ETHUSDT", "BNBUSDT"):
            if maj not in ordered_symbols:
                ordered_symbols.append(maj)
        for s in (actual_symbols or []):
            if s not in ordered_symbols:
                ordered_symbols.append(s)

        if ordered_symbols:
            bot_status["watch_symbols"] = ordered_symbols
            bot_status["active_symbols"] = ordered_symbols
            _prune_entry_diagnoses(ordered_symbols)
        bot_status["disabled_symbols"] = load_disabled_symbols()
        config_path = get_data_file_path("bot_symbols.json")
        with open(config_path, "r", encoding="utf-8") as f:
            raw_config = json.load(f)
        raw_profiles = raw_config.get("profiles", {}) if isinstance(raw_config, dict) else {}
        bot_status["trade_eligibility"] = {
            sym: {
                "eligible": bool((raw_profiles.get(sym) or {}).get("_trade_eligible", False)),
                "reason": (raw_profiles.get(sym) or {}).get("_trade_eligibility_reason", "尚無雷達確認"),
                "confirmations": int((raw_profiles.get(sym) or {}).get("_radar_confirmations", 0) or 0),
            }
            for sym in actual_symbols
        }
        bot_status["entry_diagnosis"] = _summarize_entry_diagnosis(
            bot_status["trade_eligibility"]
        )
    except Exception:
        pass

    # 跟隨模式下，介面顯示的幣池要跟來源部署完全一致；本地因「持倉保護」多
    # 加回的幣種（本地有真倉但來源清單沒選到）仍在背景由 ctx.ALL_SYMBOLS
    # 繼續做出場管理，只是不列在畫面上，避免看起來兩邊選幣邏輯跑掉了。
    try:
        follow_source = os.getenv("FOLLOW_SYMBOLS_FROM", "").strip()
        if follow_source and os.path.exists(follow_source):
            with open(follow_source, "r", encoding="utf-8") as f:
                source_data = json.load(f)
            source_symbols = source_data.get("symbols", []) if isinstance(source_data, dict) else source_data
            if source_symbols:
                bot_status["active_symbols"] = source_symbols
                bot_status["watch_symbols"] = source_symbols
    except Exception:
        pass

    return bot_status

def set_bot_balance_quote(balance: float):
    bot_status["balance_quote"] = balance

def update_bot_status(key, value):
    bot_status[key] = value

def set_entry_diagnosis(message: str):
    bot_status["entry_diagnosis"] = message
    # 交易邏輯在獨立子程序執行；只改該程序內的 dict，API 主程序看不到。
    # 透過既有 stdout 控制通道同步，讓狀態頁顯示真正的最新阻擋原因。
    print(f"@@ENTRY_DIAG@@{message}", flush=True)


def classify_bot_log_level(line: str) -> str:
    """Map meaningful bot events to the status-page severity."""
    if any(k in line for k in ("❌", "🛑", "⚠️", "停損", "REJECT", "Error", "error")):
        return "danger"
    if any(k in line for k in ("✅", "🚀", "⚡", "開倉", "平倉", "獲利")):
        return "success"
    # 🔄 means a routine refresh, not a warning. Keep actual defensive/cooldown
    # events highlighted without making every K-line update look unhealthy.
    if any(k in line for k in ("🛡️", "📊", "冷卻")):
        return "warning"
    return "info"


def read_bot_output(proc, sym):
    for line in iter(proc.stdout.readline, ''):
        line = line.strip()
        if line:
            if line.startswith("@@REGIME@@"):
                bot_status["regime"] = line.replace("@@REGIME@@", "").strip()
            elif line.startswith("@@ENTRY_DIAG@@"):
                _record_entry_diagnosis(
                    line.replace("@@ENTRY_DIAG@@", "", 1).strip()
                )
            elif line.startswith("@@COIN_REGIME@@"):
                parts = line.replace("@@COIN_REGIME@@", "").strip().split("@@")
                if len(parts) >= 2:
                    coin_sym = parts[0]
                    coin_reg = parts[1]
                    bot_status["coin_regimes"][coin_sym] = coin_reg
            elif line.startswith("@@AMOUNT@@"):
                try:
                    bot_status["trade_amount"] = float(line.replace("@@AMOUNT@@", "").strip())
                except:
                    pass
            elif line.startswith("@@LEVERAGE@@"):
                try:
                    bot_status["leverage"] = int(line.replace("@@LEVERAGE@@", "").strip())
                except:
                    pass
            elif line.startswith("@@SL_STATE@@"):
                try:
                    import json as _json
                    bot_status["sl_states"] = _json.loads(line.replace("@@SL_STATE@@", "").strip())
                except Exception:
                    pass
            elif line.startswith("@@TREND_BIAS@@"):
                try:
                    import json as _json
                    bot_status["trend_bias"] = _json.loads(line.replace("@@TREND_BIAS@@", "").strip())
                except Exception:
                    pass
            elif line.startswith("@@COIN_DEBUG@@"):
                web_line = line.replace("@@COIN_DEBUG@@", "").strip()
                if _should_emit_bot_web_log(web_line):
                    add_system_log(web_line, "info")
            else:
                _skip_prefixes = ("----", "[__multi__] ----")
                if any(line.startswith(p) for p in _skip_prefixes):
                    pass  # 靜默丟棄
                else:
                    level = classify_bot_log_level(line)
                    log_msg = line if sym == "__multi__" else f"[{sym}] {line}"
                    add_system_log(log_msg, level)
    proc.stdout.close()
    proc.wait()
    intentional_stop = id(proc) in _intentional_stop_processes
    _intentional_stop_processes.discard(id(proc))
    
    if intentional_stop:
        if os.getenv("BOT_DEBUG_LOGS") == "1":
            add_system_log(f"ℹ️ [系統守護] 機器人({sym})依管理指令正常停止", "info")
    elif proc.returncode == 4:
        # 單幣熔斷停牌 (Exit Code 4)
        from services.radar_service import replace_dead_coin, blacklist_coin
        blacklist_coin(sym, duration_sec=24*3600)
        threading.Thread(target=replace_dead_coin, args=(sym,), daemon=True).start()
    elif proc.returncode == 3:
        # 死水幣觸發淘汰 (Exit Code 3)
        from services.radar_service import replace_dead_coin
        threading.Thread(target=replace_dead_coin, args=(sym,), daemon=True).start()
    elif proc.returncode == 2:
        # 觸發全自動雷達換倉機制 (保留)
        from services.radar_service import auto_radar_switch
        threading.Thread(target=auto_radar_switch, daemon=True).start()
    elif proc.returncode == 99:
        # main.py 的單例鎖偵測到「已有核心在盯盤」，本行程只是多餘的重複啟動，正常讓路
        # 退出，不代表真正在跑的核心有任何異常，不需要重啟——重啟只會 spawn 出下一個
        # 一樣撞上同一個核心、一樣 exit 99 的行程，變成每 5 秒一次的無限迴圈，持續消耗
        # 資源卻永遠沒有真的多開出一個核心（實測發生過連續空轉超過 20 分鐘）。
        if os.getenv("BOT_DEBUG_LOGS") == "1":
            add_system_log(f"ℹ️ [防禦分流] {sym} 偵測到重複啟動，正常讓路退出，不重啟", "info")
    elif bot_status["is_running"] and sym in bot_processes and bot_processes[sym] == proc:
        # 無論退出碼為何，只要 bot_status["is_running"] 為 True，就必須重啟
        # (退出碼 0 可能是不可預期的 CancelledError 導致)
        if proc.returncode == 0:
            # 只在調試模式下記錄，避免頁面被重複重啟訊息刷爆。
            if os.getenv("BOT_DEBUG_LOGS") == "1":
                add_system_log(f"ℹ️ [防禦分流] {sym} 正常退出 (exit 0)，將在 5 秒後重試檢查...", "info")
        else:
            add_system_log(f"⚠️ [系統守護] 偵測到機器人({sym})意外停止 (exit {proc.returncode})，將在 5 秒後自動重啟...", "danger")
            
        def daemon_restart():
            time.sleep(5)
            if not bot_status["is_running"]:
                return
            if sym == "__multi__":
                _start_multi_coin_bot(bot_status["trade_amount"])
            else:
                _start_single_bot(sym, bot_status["trade_amount"])
        threading.Thread(target=daemon_restart, daemon=True).start()


def _start_single_bot(symbol: str, trade_amt: float):
    global bot_processes
    if symbol == "__multi__":
        _start_multi_coin_bot(trade_amt)
        return
    bot_status["active_symbols"] = [symbol]
    save_symbol_config(bot_status["active_symbols"])
    _start_multi_coin_bot(trade_amt)


def _start_multi_coin_bot(trade_amt: float):
    global bot_processes
    cmd = [sys.executable, "-u", "main.py"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
    bot_processes["__multi__"] = proc
    threading.Thread(target=read_bot_output, args=(proc, "__multi__"), daemon=True).start()
    count = len(bot_status.get("active_symbols", []))
    add_system_log(f"🚀 已啟動多幣輪動機器人 ({count}個幣種, 金額: {trade_amt})", "success")

def _get_open_position_symbols():
    try:
        from core.config import PAPER_STATE_FILE
        state_path = PAPER_STATE_FILE
        if not os.path.exists(state_path):
            return []
        with open(state_path, "r") as f:
            state = json.load(f)
        open_syms = []
        for key, pos in state.get("positions", {}).items():
            if abs(float(pos.get("qty", 0.0))) > 0.000001:
                sym = key.replace(":USDT", "USDT").replace(":", "")
                open_syms.append(sym)
        return open_syms
    except Exception:
        return []


def _restart_blocking_entry_orders(orders):
    """Keep only live non-reduce-only orders that could open/increase a position."""
    blocking = []
    for order in orders or []:
        info = order.get("info") or {}
        status = str(order.get("status") or info.get("status") or "NEW").upper()
        if status not in ("OPEN", "NEW", "PARTIALLY_FILLED"):
            continue
        reduce_only = order.get("reduceOnly", info.get("reduceOnly", False))
        close_position = order.get("closePosition", info.get("closePosition", False))
        if str(reduce_only).lower() in ("true", "1"):
            continue
        if str(close_position).lower() in ("true", "1"):
            continue
        blocking.append(order)
    return blocking


def _get_restart_blocking_entry_orders(force=False):
    """Query only when a restart is requested, avoiding continuous API weight."""
    now = time.time()
    cached_at = float(_restart_order_cache.get("checked_at", 0.0) or 0.0)
    if not force and now - cached_at < RESTART_ORDER_CACHE_SEC:
        return _restart_order_cache.get("orders")
    try:
        from services.binance_service import client
        if client is None:
            raise RuntimeError("Binance client unavailable")
        orders = client.futures_get_open_orders()
        blocking = _restart_blocking_entry_orders(orders)
        _restart_order_cache.update({"checked_at": now, "orders": blocking})
        bot_status["active_orders"] = len(blocking)
        return blocking
    except Exception as exc:
        _restart_order_cache.update({"checked_at": now, "orders": None})
        add_system_log(f"⚠️ [重啟安全檢查] 無法確認交易所進場掛單：{exc}", "warning")
        return None


def _restart_is_safe():
    orders = _get_restart_blocking_entry_orders()
    if orders is None:
        add_system_log("⏳ [重啟延後] 無法確認交易所掛單狀態，保留目前交易程序", "warning")
        return False
    if orders:
        labels = []
        for order in orders[:3]:
            info = order.get("info") or {}
            labels.append(str(order.get("symbol") or info.get("symbol") or "UNKNOWN"))
        add_system_log(
            f"⏳ [重啟延後] 仍有 {len(orders)} 張待成交進場單"
            f"（{', '.join(labels)}），待成交、撤單或訊號失效後再重啟",
            "warning",
        )
        return False
    return True


def start_bot(symbols=None, trade_amt: float = None):
    global bot_processes
    if bot_status.get("is_running") and bot_processes and not _restart_is_safe():
        return False
    # 確保啟動新 bot 前先清除舊的 bot 進程，避免系統中存在重複執行
    kill_bot()

    if symbols is None:
        symbols = load_symbol_config()
    elif isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        symbols = list(DEFAULT_SYMBOLS)

    symbols = normalize_symbol_list(symbols)
    symbols = _restore_truncated_radar_pool(symbols)
    symbols = _prioritize_trade_pool(symbols, load_symbol_profiles())[:TRADE_POOL_SIZE]
    # 保留有持倉的幣種，避免被換掉
    open_syms = _get_open_position_symbols()
    for s in reversed(open_syms):
        if s in symbols:
            symbols.remove(s)
        symbols.insert(0, s)
    save_symbol_config(symbols)

    if trade_amt is None:
        trade_amt = bot_status.get("trade_amount", 150.0)

    bot_status["is_running"] = True
    bot_status["active_symbols"] = symbols
    bot_status["trade_amount"] = trade_amt

    # 持久化：後端重啟後可自動恢復
    try:
        with open(BOT_STATE_PATH, "w") as f:
            json.dump({"is_running": True, "trade_amount": trade_amt}, f)
    except Exception:
        pass

    # 啟動單一多幣行程
    _start_multi_coin_bot(trade_amt)

    # 若這段程式碼是在 main.py 這個 bot 子行程「自己內部」被呼叫（例如死水汰換偵測到
    # 動能不足要換幣，走 replace_dead_coin() -> start_bot() 這條自我重啟路徑），代表舊
    # 行程即將被剛剛啟動的新行程取代。kill_bot() 已經修成不會殺掉呼叫者自己，所以舊行程
    # 這裡並不會結束，會跟新行程同時活著、各自獨立管理同一批倉位——這非常危險，實測發生
    # 過兩個 main.py 同時在跑、重複校準、互相干擾彼此的持倉時間與峰值記憶。這裡讓舊行程
    # 給新行程幾秒鐘完成初始化、拿到單例鎖檔後，自己再退出，避免兩邊同時搶單。
    try:
        import __main__ as _main_mod
        _entry_file = os.path.basename(getattr(_main_mod, "__file__", "") or "")
        if _entry_file == "main.py":
            def _self_exit_after_handoff():
                # 原本：time.sleep(8) 後不管新行程死活直接退出，
                # 已知風險：若新行程啟動失敗（例如卡在 load_markets_helper 又被
                # 其他清理邏輯誤殺），舊行程仍會照原計畫自殺，造成兩邊都死、
                # 整個機器人離線且無人接手（實測發生過 12 小時空窗）。
                # 改為：輪詢確認新行程已成功拿到單例鎖且存活，才安全退出；
                # 超時仍未確認則放棄退出，繼續運行舊行程頂著，避免真空期。
                max_wait = 30
                poll_interval = 1
                waited = 0
                handoff_confirmed = False
                my_pid = os.getpid()
                while waited < max_wait:
                    time.sleep(poll_interval)
                    waited += poll_interval
                    try:
                        with open("/tmp/binance_bot_32f2e2ed.lock", "r") as f:
                            locked_pid_text = f.read().strip()
                        if not locked_pid_text:
                            continue
                        locked_pid = int(locked_pid_text)
                        if locked_pid != my_pid:
                            os.kill(locked_pid, 0)  # 存活探測，失敗會拋 ProcessLookupError
                            handoff_confirmed = True
                            break
                    except (ValueError, ProcessLookupError, FileNotFoundError, PermissionError):
                        continue
                    except Exception:
                        continue
                if handoff_confirmed:
                    add_system_log("♻️ [自我重啟交接] 新行程已確認接手，本行程即將退出", "warning")
                    os._exit(0)
                else:
                    add_system_log(
                        f"⚠️ [自我重啟交接失敗] 等待 {max_wait} 秒仍未確認新行程接手，"
                        f"本行程繼續運行以避免離線空窗，請人工檢查",
                        "danger",
                    )
            threading.Thread(target=_self_exit_after_handoff, daemon=True).start()
    except Exception:
        pass

def _kill_single_bot(symbol: str):
    global bot_processes
    if symbol in bot_processes and bot_processes[symbol]:
        proc = bot_processes[symbol]
        _intentional_stop_processes.add(id(proc))
        _mark_intentional_stop(getattr(proc, "pid", 0))
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except:
            proc.kill()
        bot_processes[symbol] = None
        del bot_processes[symbol]
        add_system_log(f"🛑 已終止背景機器人 ({symbol})", "warning")

def kill_bot():
    global bot_processes
    bot_status["is_running"] = False

    # 清除持久化狀態，避免下次後端重啟誤以為要繼續運行
    try:
        with open(BOT_STATE_PATH, "w") as f:
            json.dump({"is_running": False}, f)
    except Exception:
        pass

    symbols = list(bot_processes.keys())
    for s in symbols:
        _kill_single_bot(s)
    
    # 確保所有遺留的 bot 行程都被清除（包含 manage_bot.sh 直接啟動的進程）。
    # 原本用 pkill -f 'main.py' 整批殺，沒有排除呼叫者自己的 PID——core/runner.py 的
    # periodic_momentum_swap()（換動能不足的幣）是在 main.py 這個 bot 子行程「自己內部」
    # 呼叫 replace_dead_coin() -> start_bot() -> kill_bot()，等於 main.py 呼叫這行時會把
    # 「正在執行這段程式碼的自己」也一起殺掉（exit -15），造成系統守護誤判成「意外停止」
    # 反覆重啟——實際發生過同一天內好幾次換幣就自己重啟一次。改成排除自己的 PID，
    # 只清掉其他遺留的 main.py 行程。
    try:
        my_pid = os.getpid()
        pgrep_out = os.popen("pgrep -f 'main\\.py'").read().split()
        for pid_str in pgrep_out:
            try:
                pid = int(pid_str)
                if pid != my_pid:
                    _mark_intentional_stop(pid)
                    os.kill(pid, 15)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass

    # 移除單例鎖定檔，避免已終止程序遺留鎖定導致新進程啟動失敗
    for _lf in ("/tmp/binance_bot_32f2e2ed.lock", "/tmp/binance_bot_single_instance.lock"):
        try:
            os.remove(_lf)
        except FileNotFoundError:
            pass
        except Exception:
            pass

def restart_bot():
    return start_bot()


def auto_restore_bot_on_startup():
    """後端重啟後，若之前機器人在運行中，自動重新啟動"""
    try:
        if not os.path.exists(BOT_STATE_PATH):
            return
        with open(BOT_STATE_PATH, "r") as f:
            state = json.load(f)
        if not state.get("is_running", False):
            return
        trade_amt = state.get("trade_amount", bot_status.get("trade_amount", 150.0))
        add_system_log("♻️ [自動恢復] 偵測到後端重啟，正在自動重新啟動機器人...", "warning")

        def _delayed_restore():
            time.sleep(3)  # 等待 API 完全就緒
            start_bot(trade_amt=trade_amt)

        threading.Thread(target=_delayed_restore, daemon=True).start()
    except Exception as e:
        add_system_log(f"⚠️ [自動恢復] 讀取狀態失敗: {e}", "warning")

def toggle_bot():
    is_running = not bot_status["is_running"]
    status_str = "啟動" if is_running else "停止"
    add_system_log(f"手動{status_str}機器人群組", "info")
    
    if is_running:
        start_bot()
    else:
        if not _restart_is_safe():
            add_system_log(
                "ℹ️ 若確定要停止，請先取消待成交進場單；目前機器人維持運行以繼續管理掛單",
                "warning",
            )
            return True
        kill_bot()
    return bot_status["is_running"]

def set_bot_symbol(symbols):
    from core.config import COIN_PROFILE_CONFIG

    if isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        symbols = list(DEFAULT_SYMBOLS)

    symbols = normalize_symbol_list(symbols)
    symbols = [s for s in symbols if not COIN_PROFILE_CONFIG.get(s, {}).get("disable_entry", False)][:TRADE_POOL_SIZE]
    save_symbol_config(symbols)
    bot_status["active_symbols"] = symbols

    amt = bot_status.get("trade_amount", 150.0)
    bot_status["strategy"] = _strategy_label(bot_status.get("balance_quote"))
    add_system_log(f"🎯 自動交易監聽目標切換為: {', '.join(symbols)}", "info")

    return symbols

def set_bot_watch_symbols(symbols):
    if not isinstance(symbols, list):
        symbols = [symbols]
    symbols = [s.upper() for s in symbols][:10]
    bot_status["watch_symbols"] = symbols
    add_system_log(f"📋 使用者更新自選關注清單: {', '.join(symbols)}", "info")
    return symbols

def set_bot_amount(amount: float):
    if amount < 0 or amount > 1000:
        raise ValueError("單次交易數量必須限制在 0 至 1000 之間")
    bot_status["trade_amount"] = amount
    bot_status["strategy"] = _strategy_label(bot_status.get("balance_quote"))
    add_system_log(f"⚙️ 自動交易單次數量設定為: {amount}", "info")
    
    if bot_status.get("is_running"):
        add_system_log("♻️ 已重新啟動所有機器人以套用新的下單金額", "warning")
        restart_bot()
        
    return bot_status["trade_amount"]
