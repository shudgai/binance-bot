KNOWN_QUOTES = ["USDT", "BUSD", "BNB", "BTC", "ETH", "USDC"]

def parse_symbol(symbol: str):
    s = symbol.upper()
    for q in KNOWN_QUOTES:
        if s.endswith(q):
            return s[:-len(q)], q
    return s[:-4] if len(s) > 4 else s, s[-4:] if len(s) > 4 else s

def paper_key(symbol: str) -> str:
    base, quote = parse_symbol(symbol.upper())
    return f"{base}:{quote}"
