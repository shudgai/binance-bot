import json
import os
from pathlib import Path

COOLDOWN_STORE_FILE = Path("data/position_cooldowns.json")


def _norm_symbol(symbol):
    return str(symbol or "").replace(":", "").replace("/", "").upper()


def _read_store():
    try:
        if not COOLDOWN_STORE_FILE.exists():
            return {}
        with COOLDOWN_STORE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_store(data):
    COOLDOWN_STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOLDOWN_STORE_FILE.with_suffix(COOLDOWN_STORE_FILE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, COOLDOWN_STORE_FILE)


def save_cooldown(
    symbol, status, next_status_time, status_reason="", stop_count=0,
    first_stop_time=0.0, last_exit_direction="",
):
    """記錄冷卻/封禁狀態到磁碟，讓機器人重啟後還能還原剩餘冷卻時間，
    不會因為重啟就把 mark_exit() 剛設好的冷卻清空、提早放行重新進場。"""
    key = _norm_symbol(symbol)
    if not key:
        return
    data = _read_store()
    data[key] = {
        "status": status,
        "next_status_time": float(next_status_time or 0.0),
        "status_reason": status_reason,
        "stop_count": int(stop_count or 0),
        "first_stop_time": float(first_stop_time or 0.0),
        "last_exit_direction": str(last_exit_direction or "").lower(),
    }
    _write_store(data)


def load_cooldown(symbol):
    key = _norm_symbol(symbol)
    if not key:
        return None
    try:
        entry = _read_store().get(key)
        return entry if isinstance(entry, dict) else None
    except Exception:
        return None


def clear_cooldown(symbol):
    key = _norm_symbol(symbol)
    if not key:
        return
    data = _read_store()
    if key in data:
        data.pop(key, None)
        _write_store(data)
