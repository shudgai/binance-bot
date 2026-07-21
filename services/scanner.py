import os
import sys
import time
import json
import ccxt
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Configuration
WHITELIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "NEARUSDT", "UNIUSDT", "AAVEUSDT", "HYPEUSDT", "WLDUSDT", "1000PEPEUSDT", "TRUMPUSDT", "SUIUSDT"]
MAX_SYMBOLS = 15  # Target count of active symbols
MIN_24H_QUOTE_VOLUME = 50_000_000  # 5,000萬 USDT 最低 24H 成交量要求
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "scanner_state.json")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "bot_symbols.json")
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

        logger.info("🔍 Loading markets and fetching 24h tickers...")
        tickers = exchange.fetch_tickers()

        candidates = []
        current_volumes = {}

        for symbol, ticker in tickers.items():
            # Check for USDT perpetual futures symbols
            if not symbol.endswith('USDT') and not symbol.endswith('USDT:USDT'):
                continue
            
            clean_sym = symbol.replace('/', '').split(':')[0]
            if not clean_sym.endswith('USDT'):
                continue

            last_price = ticker.get('last')
            quote_volume = ticker.get('quoteVolume')

            if last_price is None or quote_volume is None:
                continue

            vol_val = float(quote_volume)
            current_volumes[clean_sym] = vol_val

            # 嚴格成交量過濾：24H 成交量低於 5,000萬 USDT 直接剔除 (白名單豁免)
            if clean_sym not in WHITELIST:
                if vol_val < MIN_24H_QUOTE_VOLUME:
                    continue

            candidates.append((clean_sym, vol_val))

        # 按 24H 成交金額 (USDT) 降序排序，確保選出的全是大流動性主流幣
        candidates.sort(key=lambda x: x[1], reverse=True)

        selected_symbols = [item[0] for item in candidates[:MAX_SYMBOLS]]

        # Sort selected symbols alphabetically
        selected_symbols.sort()

        # Save to bot_symbols.json and preserve existing symbol profiles if any
        logger.info(f"🎯 Selected {len(selected_symbols)} symbols (sorted): {selected_symbols}")
        payload = {"symbols": selected_symbols}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                if isinstance(existing, dict) and isinstance(existing.get('profiles'), dict):
                    payload['profiles'] = existing['profiles']
            except (OSError, json.JSONDecodeError):
                logger.warning("⚠️ Existing symbol config could not be read; profiles were not preserved")
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)

        # Save current volumes as state for the next run
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(current_volumes, f, ensure_ascii=False)

        logger.info("✅ Volume growth scan completed successfully!")

    except Exception as e:
        logger.exception(f"❌ Scanner Error: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Check arguments
    once_mode = "--once" in sys.argv
    
    if once_mode:
        logger.info("🏃 Starting one-shot volume scan...")
        run_scan()
    else:
        logger.info(f"🌀 Starting daemon volume scanner (Interval: {SCAN_INTERVAL_SEC}s)...")
        while True:
            run_scan()
            logger.info(f"💤 Sleeping for {SCAN_INTERVAL_SEC} seconds...")
            time.sleep(SCAN_INTERVAL_SEC)
