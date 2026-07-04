#!/usr/bin/env python3
import asyncio
import time
import os
import sys
from statistics import mean
from pathlib import Path

# ensure project root is on sys.path so we can import core/ modules
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

async def monitor(duration_sec=1800, interval_sec=15):
    from core import ctx
    from core.exchange_client import exchange_market_data, check_binance_weight, _send_tele_alert

    samples = []
    end_at = time.time() + duration_sec
    while time.time() < end_at:
        ts = time.time()
        # do a lightweight public request to update last_response_headers
        try:
            async with ctx.request_semaphore:
                await exchange_market_data.fetch_time()
        except Exception:
            pass

        headers = getattr(exchange_market_data, 'last_response_headers', {}) or {}
        weight = None
        for k, v in headers.items():
            if k.lower() == 'x-mbx-used-weight-1m':
                try:
                    weight = int(v)
                except Exception:
                    weight = None
                break

        cooldown = check_binance_weight()
        samples.append((ts, weight if weight is not None else -1, float(cooldown)))
        print(f"[monitor] {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))} weight={weight} cooldown={cooldown}")
        await asyncio.sleep(interval_sec)

    # summary
    weights = [w for _, w, _ in samples if w >= 0]
    summary_lines = []
    summary_lines.append(f"Weight monitor finished: samples={len(samples)} duration={duration_sec}s interval={interval_sec}s")
    if weights:
        summary_lines.append(f"min={min(weights)} max={max(weights)} avg={mean(weights):.1f}")
        over_1800 = sum(1 for w in weights if w > 1800)
        over_2400 = sum(1 for w in weights if w >= 2400)
        summary_lines.append(f"count>1800={over_1800} count>=2400={over_2400}")
    else:
        summary_lines.append("no weight values observed")

    summary = "\n".join(summary_lines)
    logfile = f"/tmp/weight_monitor_{int(time.time())}.log"
    try:
        with open(logfile, 'w') as f:
            for t, w, c in samples:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))}\t{w}\t{c}\n")
            f.write('\n')
            f.write(summary + '\n')
    except Exception:
        pass

    print(summary)
    try:
        _send_tele_alert(f"[機器人] {summary}")
    except Exception:
        pass


if __name__ == '__main__':
    dur = int(os.getenv('WEIGHT_MONITOR_DURATION', '1800'))
    interval = int(os.getenv('WEIGHT_MONITOR_INTERVAL', '15'))
    asyncio.run(monitor(duration_sec=dur, interval_sec=interval))
