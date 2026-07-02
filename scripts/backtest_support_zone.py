#!/usr/bin/env python3
import json
import re
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

# Import config for per-coin overrides
import sys
sys.path.append(str(Path(__file__).resolve().parents[1]))
from core.config import COIN_PROFILE_CONFIG

LOG_FILE = Path(__file__).resolve().parents[1] / 'data' / 'system_logs.json'

PAT = re.compile(r"\[SUPPORT_ZONE\].*?([A-Z0-9]{2,8}USDT).*?買入價\s*([0-9\.]+).*?(?:下軌|遠離下軌)\s*([0-9\.]+).*?訊號強度\s*([0-9\.]+)")
PAT_EN = re.compile(r"\[RESISTANCE_ZONE\].*?([A-Z0-9]{2,8}USDT).*?賣出價\s*([0-9\.]+).*?(?:上軌|遠離上軌)\s*([0-9\.]+).*?訊號強度\s*([0-9\.]+)")


def analyze(hours=24):
    if not LOG_FILE.exists():
        print('no system_logs.json')
        return
    with LOG_FILE.open('r', encoding='utf-8') as f:
        logs = json.load(f)

    cutoff = datetime.now().strftime('%H:%M:%S')  # not used; logs have no dates beyond time

    total = 0
    would_allow = defaultdict(int)
    original_rej = defaultdict(int)
    details = []

    # iterate recent logs
    for e in logs[-2000:]:
        t = e.get('text','')
        m = PAT.search(t)
        if m:
            sym = m.group(1)
            cp = float(m.group(2))
            bb_low = float(m.group(3))
            strength = float(m.group(4))
            total += 1
            original_rej[sym] += 1
            # default global
            default_tol = 0.003
            default_strength = 24.0
            cfg = COIN_PROFILE_CONFIG.get(sym, {})
            tol = cfg.get('support_zone_tolerance_pct', default_tol)
            strength_thresh = cfg.get('support_zone_strength_threshold', default_strength)
            # evaluate under original global rules
            # original is tol=0.003 and strength_thresh=28
            original_allowed = (cp <= bb_low * (1 + default_tol)) or (strength >= default_strength)
            new_allowed = (cp <= bb_low * (1 + tol)) or (strength >= strength_thresh)
            if new_allowed and not original_allowed:
                would_allow[sym] += 1
                details.append({'sym':sym,'cp':cp,'bb_low':bb_low,'strength':strength,'orig_tol':default_tol,'new_tol':tol,'orig_strength':default_strength,'new_strength':strength_thresh})

    print(f'Total support_zone rejections scanned: {total}')
    print('\nPer-symbol summary of additional allowed after relax:')
    for s,count in sorted(would_allow.items(), key=lambda x:-x[1]):
        print(f'- {s}: {count} would be allowed')
    print('\nTop details (up to 20):')
    for d in details[:20]:
        print(d)

if __name__ == '__main__':
    analyze()
