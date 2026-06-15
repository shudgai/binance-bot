import os
import json
import logging

logger = logging.getLogger("multi_coin_bot")

BANNED_TOKENS = [
    'CROSS', 'HANA', 'COAI', 'PHA', 'BAN', 'FOGO', 
    'ESPORTS', 'PLAY', 'HOME', 'VELVET', 'AIO', 'ALLO',
    'H', 'CL', 'BZ', 'SIREN', 'BEAT', 'OPG', 'EVAA',
    'XAU', 'XAG',
    'ZEC',
    'NOT', 'BOME', 'PEOPLE',
    'SHIB', 'FLOKI', 'WIF', 'SEI', 'STRK', 'CRV',
    'ARB', 'OP'
]
SLOW_OR_LOW_QUALITY_SYMBOLS = {
    "AERO", "ADA", "DOT", "UNI", "FET",
    "STG", "SEI",
    "NOT",     # ATR = 0，精度問題
    "BOME",    # ATR = 0，精度問題
    "PEOPLE",  # ATR 幾乎為 0
    "1000PEPEUSDT", "1000BONKUSDT", "1000FLOKIUSDT", "WIFUSDT", "ARKMUSDT",
    "USDCUSDT", "SPACEUSDT"
}

ANCHOR_COINS = ["SOLUSDT"]

async def fetch_top_volume_symbols(exchange, limit=30):
    """
    獲取 Binance Futures 24 小時內成交額最高的前 N 名幣種。
    排除黑名單與異常幣種，並始終保留 BTC, ETH, SOL。
    """
    try:
        tickers = await exchange.fetch_tickers()
        candidates = []
        
        for sym, t in tickers.items():
            raw_sym = t.get('info', {}).get('symbol', '')
            if not raw_sym:
                raw_sym = sym.replace('/', '').replace(':USDT', '')
                
            if not raw_sym.endswith("USDT") or "_" in raw_sym:
                continue
                
            base_asset = raw_sym.replace("USDT", "")
            if base_asset in BANNED_TOKENS or raw_sym in SLOW_OR_LOW_QUALITY_SYMBOLS or base_asset in SLOW_OR_LOW_QUALITY_SYMBOLS:
                continue
                
            quote_volume = float(t.get('quoteVolume', 0))
            if quote_volume > 0:
                candidates.append((raw_sym, quote_volume))
                
        # 依據成交額排序
        candidates.sort(key=lambda x: x[1], reverse=True)
        
        # 挑選 Top N
        top_symbols = [sym for sym, vol in candidates[:limit*2]]
        
        # 組合最終名單
        final_pool = []
        for anchor in ANCHOR_COINS:
            if anchor not in final_pool:
                final_pool.append(anchor)
                
        for sym in top_symbols:
            if sym not in final_pool:
                final_pool.append(sym)
            if len(final_pool) >= limit:
                break
                
        return final_pool
    except Exception as e:
        logger.error(f"⚠️ [幣種管理] 獲取高成交量幣種失敗: {e}")
        return None

def read_bot_symbols():
    config_file = os.path.join(os.path.dirname(__file__), "..", "bot_symbols.json")
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("symbols", [])
    except Exception:
        return []

def write_bot_symbols(symbols):
    config_file = os.path.join(os.path.dirname(__file__), "..", "bot_symbols.json")
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump({"symbols": symbols}, f, ensure_ascii=False)

async def replace_underperforming_symbol(exchange, old_symbol):
    """
    緊急替換單一表現不佳的幣種（例如停損 3 次或暴跌 50%）。
    將舊幣種從 bot_symbols.json 中移除，並從 Top 30 中挑選一個全新幣種加入。
    """
    if old_symbol in ANCHOR_COINS:
        logger.warning(f"⚠️ [幣種管理] {old_symbol} 是錨定幣種，不可替換。")
        return None

    current_symbols = read_bot_symbols()
    if old_symbol in current_symbols:
        current_symbols.remove(old_symbol)
        
    top_symbols = await fetch_top_volume_symbols(exchange, limit=40)
    if not top_symbols:
        return None
        
    new_symbol = None
    for sym in top_symbols:
        if sym not in current_symbols and sym != old_symbol:
            new_symbol = sym
            break
            
    if new_symbol:
        current_symbols.append(new_symbol)
        write_bot_symbols(current_symbols)
        logger.info(f"🔄 [幣種管理] 已將表現不佳的 {old_symbol} 替換為 {new_symbol}")
        return new_symbol
    else:
        logger.warning(f"⚠️ [幣種管理] 找不到適合替換 {old_symbol} 的新幣種")
        return None
