import json
import os
from pathlib import Path

ENTRY_TIME_STORE_FILE = Path("data/position_entry_times.json")


def _norm_symbol(symbol):
    return str(symbol or "").replace(":", "").replace("/", "").upper()


def _read_store():
    try:
        if not ENTRY_TIME_STORE_FILE.exists():
            return {}
        with ENTRY_TIME_STORE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_store(data):
    ENTRY_TIME_STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENTRY_TIME_STORE_FILE.with_suffix(ENTRY_TIME_STORE_FILE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, ENTRY_TIME_STORE_FILE)


def load_entry_time(symbol):
    key = _norm_symbol(symbol)
    if not key:
        return 0.0
    try:
        return max(0.0, float(_read_store().get(key, 0.0) or 0.0))
    except Exception:
        return 0.0


def save_entry_time(symbol, ts):
    key = _norm_symbol(symbol)
    if not key:
        return
    try:
        ts = max(0.0, float(ts or 0.0))
    except Exception:
        return
    data = _read_store()
    data[key] = ts
    _write_store(data)


def clear_entry_time(symbol):
    key = _norm_symbol(symbol)
    if not key:
        return
    data = _read_store()
    if key in data:
        data.pop(key, None)
        _write_store(data)
