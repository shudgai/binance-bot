import os
import sys
import time
import json
import ccxt
from dotenv import load_dotenv

load_dotenv()

# Configuration
WHITELIST = ["BTCUSDT", "ETHUSDT"]
MAX_SYMBOLS = 20  # Target count of active symbols
STATE_FILE = os.path.join(os.path.dirname(__file__), "scanner_state.json")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "bot_symbols.json")
SCAN_INTERVAL_SEC = 3600

def run_scan():
    try:
        # Initialize exchange
        exchange = ccxt.binance({
            'options': {'defaultType': 'future'},
            'enableRateLimit': True,
        })
        use_testnet = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
        if use_testnet:
            exchange.urls['api']['fapiPublic'] = 'https://testnet.binancefuture.com/fapi/v1'
            exchange.urls['api']['fapiPrivate'] = 'https://testnet.binancefuture.com/fapi/v1'

        print("🔍 Loading markets and fetching 24h tickers...")
        tickers = exchange.fetch_tickers()

        candidates = []
        current_volumes = {}

        for symbol, ticker in tickers.items():
            # Check for USDT perpetual futures symbols
            if not symbol.endswith('USDT') and not symbol.endswith('USDT:USDT'):
                continue
            
            # Standardize symbol name (e.g. BTC/USDT:USDT -> BTCUSDT)
            clean_sym = symbol.replace('/', '').split(':')[0]
            if not clean_sym.endswith('USDT'):
                continue

            last_price = ticker.get('last')
            quote_volume = ticker.get('quoteVolume')

            if last_price is None or quote_volume is None:
                continue

            current_volumes[clean_sym] = float(quote_volume)

            # Filter candidates: price under $5.0 OR in whitelist
            if clean_sym not in WHITELIST:
                if last_price > 5.0 or last_price == 0:
                    continue

            candidates.append(clean_sym)

        # Load previous volumes for change rate calculation
        prev_volumes = {}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    prev_volumes = json.load(f)
            except Exception as e:
                print(f"⚠️ Error loading state file: {e}")

        # Compute volume growth rates
        growth_rates = []
        for sym in candidates:
            if sym in WHITELIST:
                continue
            curr_vol = current_volumes.get(sym, 0.0)
            prev_vol = prev_volumes.get(sym, 0.0)

            # Growth rate calculation: (current - previous) / previous
            if prev_vol > 0.0:
                rate = (curr_vol - prev_vol) / prev_vol
            else:
                rate = 0.0  # Default to 0 growth if no prior data
            
            growth_rates.append((sym, rate, curr_vol))

        # Sort by growth rate descending (and absolute volume as secondary sort key)
        growth_rates.sort(key=lambda x: (x[1], x[2]), reverse=True)

        # Build new symbols list starting with whitelist
        selected_symbols = list(WHITELIST)
        for item in growth_rates:
            if len(selected_symbols) >= MAX_SYMBOLS:
                break
            sym = item[0]
            if sym not in selected_symbols:
                selected_symbols.append(sym)

        # Sort selected symbols alphabetically
        selected_symbols.sort()

        # Save to bot_symbols.json and preserve existing symbol profiles if any
        print(f"🎯 Selected {len(selected_symbols)} symbols (sorted): {selected_symbols}")
        payload = {"symbols": selected_symbols}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                if isinstance(existing, dict) and isinstance(existing.get('profiles'), dict):
                    payload['profiles'] = existing['profiles']
            except Exception:
                pass
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)

        # Save current volumes as state for the next run
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(current_volumes, f, ensure_ascii=False)

        print("✅ Volume growth scan completed successfully!")

    except Exception as e:
        print(f"❌ Scanner Error: {e}")

if __name__ == "__main__":
    # Check arguments
    once_mode = "--once" in sys.argv
    
    if once_mode:
        print("🏃 Starting one-shot volume scan...")
        run_scan()
    else:
        print(f"🌀 Starting daemon volume scanner (Interval: {SCAN_INTERVAL_SEC}s)...")
        while True:
            run_scan()
            print(f"💤 Sleeping for {SCAN_INTERVAL_SEC} seconds...")
            time.sleep(SCAN_INTERVAL_SEC)
