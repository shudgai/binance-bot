import json
import os
import time
from pathlib import Path

PEAK_STORE_FILE = Path("data/position_peaks.json")

# 峰值檔案本來只給「重啟後接回同一筆倉位」用，理論上只需要撐過一次重啟的
# 空窗。但平倉流程裡有好幾個地方（remaining>=0.01 判斷為未完全平倉、qty
# 對帳失敗等）會漏呼叫 clear_peak()，而 save_peak() 又是只增不減的棘輪
# （max(現有值, 新值)），一旦某次漏清，這個舊高點就會卡住不會歸零，悄悄
# 污染這個幣種之後每一筆完全無關的新交易（實測 LINKUSDT 卡在 1.5% 至少
# 橫跨三天、三筆不同交易）。與其繼續在每個平倉分支補「別忘記呼叫
# clear_peak()」，不如讓資料本身有效期：超過這個時間視同過期，自動當作
# 沒有紀錄。6 小時遠比任何正常持倉時間長（不影響重啟接回的原始用途），
# 但又能讓任何一次漏清最多在同一個交易日內自動復原，不會累積成永久污染。
PEAK_STALE_AFTER_SEC = 6 * 3600


def _norm_symbol(symbol):
    return str(symbol or "").replace(":", "").replace("/", "").upper()


def _read_store():
    try:
        if not PEAK_STORE_FILE.exists():
            return {}
        with PEAK_STORE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_store(data):
    PEAK_STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PEAK_STORE_FILE.with_suffix(PEAK_STORE_FILE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, PEAK_STORE_FILE)


def _entry_value(entry):
    """Accept both the new {value, ts} format and legacy bare-number entries.
    Legacy entries carry no timestamp, so their age is unknown; treat them as
    stale (0.0) rather than risk resurrecting old, possibly-corrupted data."""
    if isinstance(entry, dict):
        try:
            ts = float(entry.get("ts", 0.0) or 0.0)
            value = float(entry.get("value", 0.0) or 0.0)
        except Exception:
            return 0.0
        if ts <= 0 or (time.time() - ts) > PEAK_STALE_AFTER_SEC:
            return 0.0
        return max(0.0, value)
    return 0.0


def load_peak(symbol):
    key = _norm_symbol(symbol)
    if not key:
        return 0.0
    try:
        return _entry_value(_read_store().get(key))
    except Exception:
        return 0.0


def save_peak(symbol, peak):
    key = _norm_symbol(symbol)
    if not key:
        return 0.0
    try:
        peak = max(0.0, float(peak or 0.0))
    except Exception:
        peak = 0.0
    data = _read_store()
    current = _entry_value(data.get(key))
    new_peak = max(current, peak)
    data[key] = {"value": new_peak, "ts": time.time()}
    _write_store(data)
    return new_peak


def load_partial_take_profit(symbol):
    key = _norm_symbol(symbol)
    return bool(_read_store().get(f"{key}__PARTIAL_TP", False)) if key else False


def save_partial_take_profit(symbol):
    key = _norm_symbol(symbol)
    if not key:
        return
    data = _read_store()
    data[f"{key}__PARTIAL_TP"] = True
    _write_store(data)


def clear_peak(symbol):
    key = _norm_symbol(symbol)
    if not key:
        return
    data = _read_store()
    changed = False
    for store_key in (key, f"{key}__PARTIAL_TP"):
        if store_key in data:
            data.pop(store_key, None)
            changed = True
    if changed:
        _write_store(data)
