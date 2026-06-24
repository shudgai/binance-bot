import json

with open('bot_symbols.json', 'r') as f:
    data = json.load(f)

for symbol, profile in data.get('profiles', {}).items():
    profile['risk_threshold_pct'] = 0.005

with open('bot_symbols.json', 'w') as f:
    json.dump(data, f, indent=2)
