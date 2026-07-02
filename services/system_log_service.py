import collections
import datetime
import json
import logging
import os
import fcntl
import tempfile
import pytz

# 系統日誌儲存（改為檔案型，讓 API 與子流程共享）
_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "system_logs.json")
_LOCK_FILE = os.path.join(_LOG_DIR, "system_logs.lock")

system_logs = collections.deque(maxlen=100)
_tz_taipei = pytz.timezone('Asia/Taipei')


class FileBackedSystemLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            add_system_log(msg, level=self._level_to_name(record.levelno))
        except Exception:
            pass

    @staticmethod
    def _level_to_name(levelno):
        if levelno >= logging.ERROR:
            return "danger"
        if levelno >= logging.WARNING:
            return "warning"
        if levelno >= logging.INFO:
            return "info"
        return "info"


def _load_logs_from_disk():
    try:
        with open(_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        normalized = []
        for item in data:
            if isinstance(item, dict):
                text = item.get("text")
                level = item.get("level", "info")
                if isinstance(text, str) and isinstance(level, str):
                    normalized.append({"time": item.get("time", ""), "text": text, "level": level})
        return normalized[-100:]
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def _write_logs_to_disk(entries):
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix="system_logs_", suffix=".json", dir=_LOG_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)
        os.replace(temp_path, _LOG_FILE)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _locked_state():
    with open(_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            return _load_logs_from_disk()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _bootstrap_from_disk():
    disk_logs = _locked_state()
    if disk_logs:
        system_logs.clear()
        system_logs.extend(disk_logs)
    else:
        system_logs.clear()


_bootstrap_from_disk()


def attach_to_root_logger(root_logger: logging.Logger):
    handler = FileBackedSystemLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(handler)
    return handler


def add_system_log(text: str, level: str = "info"):
    now = datetime.datetime.now(_tz_taipei).strftime("%H:%M:%S")
    if isinstance(text, Exception):
        text = str(text)
    entry = {"time": now, "text": text, "level": level}

    with open(_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            entries = _load_logs_from_disk()
            if not entries:
                entries = list(system_logs)
            entries.append(entry)
            entries = entries[-100:]
            system_logs.clear()
            system_logs.extend(entries)
            _write_logs_to_disk(entries)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def get_system_logs():
    with open(_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            entries = _load_logs_from_disk()
            if entries:
                system_logs.clear()
                system_logs.extend(entries)
            elif not system_logs:
                system_logs.clear()
            return list(system_logs)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def clear_system_logs():
    with open(_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            system_logs.clear()
            _write_logs_to_disk([])
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
