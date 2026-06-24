import json

with open('bot_symbols.json', 'r') as f:
    data = json.load(f)

for symbol, profile in data.get('profiles', {}).items():
    profile['rescue_tp_floor_pct'] = 0.002
    profile['rescue_trailing_atr'] = 0.75
    profile['rescue_timeout_min'] = 30

with open('bot_symbols.json', 'w') as f:
    json.dump(data, f, indent=2)
