import json
import os
from pathlib import Path

PEAK_STORE_FILE = Path("data/position_peaks.json")


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


def load_peak(symbol):
    key = _norm_symbol(symbol)
    if not key:
        return 0.0
    try:
        return max(0.0, float(_read_store().get(key, 0.0) or 0.0))
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
    current = 0.0
    try:
        current = max(0.0, float(data.get(key, 0.0) or 0.0))
    except Exception:
        current = 0.0
    new_peak = max(current, peak)
    data[key] = new_peak
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
