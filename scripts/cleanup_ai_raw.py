#!/usr/bin/env python3
from pathlib import Path
from datetime import datetime, timedelta

RAW_DIR = Path(__file__).resolve().parents[1] / 'data' / 'ai_raw_responses'
KEEP_DAYS = 7

if __name__ == '__main__':
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.utcnow() - timedelta(days=KEEP_DAYS)
    removed = 0
    for f in RAW_DIR.glob('response_*.txt'):
        try:
            if datetime.utcfromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                print('removed', f)
                removed += 1
        except Exception as e:
            print('error removing', f, e)
    print('done. removed', removed)
