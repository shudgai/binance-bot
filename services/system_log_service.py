import collections
import datetime
import pytz

# 系統日誌儲存
system_logs = collections.deque(maxlen=100)
_tz_taipei = pytz.timezone('Asia/Taipei')
LOG_FILE = "/home/shudgai999/project/binance-bot/bot_v2.log"

def add_system_log(text: str, level: str = "info"):
    now = datetime.datetime.now(_tz_taipei).strftime("%H:%M:%S")
    system_logs.append({"time": now, "text": text, "level": level})
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{now}] [{level.upper()}] {text}\n")

def get_system_logs():
    return list(system_logs)

def clear_system_logs():
    system_logs.clear()
