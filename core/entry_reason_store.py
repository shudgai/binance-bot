import json
import os
from pathlib import Path

ENTRY_REASON_STORE_FILE = Path("data/position_entry_reasons.json")


def _norm_symbol(symbol):
    return str(symbol or "").replace(":", "").replace("/", "").upper()


def _read_store():
    try:
        if not ENTRY_REASON_STORE_FILE.exists():
            return {}
        with ENTRY_REASON_STORE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_store(data):
    ENTRY_REASON_STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENTRY_REASON_STORE_FILE.with_suffix(ENTRY_REASON_STORE_FILE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, ENTRY_REASON_STORE_FILE)


def load_entry_reason(symbol):
    key = _norm_symbol(symbol)
    if not key:
        return None
    return _read_store().get(key)


def save_entry_reason(symbol, reason):
    key = _norm_symbol(symbol)
    if not key or not reason:
        return
    data = _read_store()
    data[key] = reason
    _write_store(data)


def clear_entry_reason(symbol):
    key = _norm_symbol(symbol)
    if not key:
        return
    data = _read_store()
    if key in data:
        data.pop(key, None)
        _write_store(data)
