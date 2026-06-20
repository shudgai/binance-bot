import datetime
import pytz
import time
tz = pytz.timezone('Asia/Taipei')
now = datetime.datetime.now(tz)
target = now.replace(hour=6, minute=0, second=0, microsecond=0)
if now >= target:
    target += datetime.timedelta(days=1)
wait_seconds = (target - now).total_seconds()
print("WAITING SECONDS:", wait_seconds)
