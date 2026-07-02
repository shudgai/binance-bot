#!/usr/bin/env python3
import re
import json
from pathlib import Path
from datetime import datetime, timedelta

LOG_FILE = Path(__file__).resolve().parents[1] / 'logs' / 'bot.log'
SYSLOG = Path(__file__).resolve().parents[1] / 'data' / 'system_logs.json'

PATTERNS = [
    (re.compile(r"Rescue_DCA_Triggered"), 'Rescue DCA triggered'),
    (re.compile(r"DCA_Failure_Exit"), 'DCA failure exit'),
    (re.compile(r"AI API 調用失敗"), 'AI API failures'),
    (re.compile(r"AI API 回傳"), 'AI API raw response issues'),
]


def scan_bot_log(minutes=60):
    results = {p[1]: [] for p in PATTERNS}
    if not LOG_FILE.exists():
        return results
    cutoff = datetime.now() - timedelta(minutes=minutes)
    with LOG_FILE.open('r', encoding='utf-8', errors='ignore') as fh:
        for line in fh:
            # try to parse timestamp at start
            ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+", line)
            if ts_match:
                try:
                    ts = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S')
                except:
                    ts = None
            else:
                ts = None

            if ts and ts < cutoff:
                continue
            for pat, name in PATTERNS:
                if pat.search(line):
                    results[name].append(line.strip())
    return results


def scan_system_logs():
    out = {p[1]: [] for p in PATTERNS}
    if not SYSLOG.exists():
        return out
    try:
        data = json.loads(SYSLOG.read_text(encoding='utf-8'))
    except Exception as e:
        return {'error': [f'failed to read system_logs.json: {e}']}
    for entry in data:
        text = entry.get('text','')
        for pat, name in PATTERNS:
            if pat.search(text):
                out[name].append({'time': entry.get('time'), 'text': text})
    return out


if __name__ == '__main__':
    print('Scanning last 60 minutes in logs/bot.log for key events...')
    bot_results = scan_bot_log(60)
    for k,v in bot_results.items():
        print(f"- {k}: {len(v)} occurrences")
        if v:
            for line in v[-3:]:
                print('   ', line[:300])
    print('\nScanning data/system_logs.json for matching entries...')
    sys_results = scan_system_logs()
    for k,v in sys_results.items():
        print(f"- {k}: {len(v)} occurrences in system_logs.json")
        if v:
            for e in v[-3:]:
                print('   ', e.get('time'), e.get('text')[:200])
    print('\nDone.')
