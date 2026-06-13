import urllib.request
import json

url = "http://127.0.0.1:8888/v1/chat/completions"

system_prompt = """你是加密貨幣短線交易分析助手。
根據提供的技術指標數據,針對每個幣種判斷:
1. 短線方向偏好 (bias): "long" / "short" / "neutral"
2. 信心程度 (confidence): 0.0 ~ 1.0
3. 簡短理由 (reason): 20字以內

注意:
- 只根據提供的數值判斷,不要假設你有即時新聞或市場資訊
- 如果數據矛盾或不明確,優先回答 neutral 並給低信心分數
- 嚴格只回傳 JSON,不要有任何其他文字或markdown格式

回傳格式範例:
{"BTCUSDT": {"bias": "long", "confidence": 0.6, "reason": "MACD轉強且站上EMA20"}}"""

mock_data = {
  "BTCUSDT": {
    "symbol": "BTCUSDT",
    "price": 65000.5,
    "rsi": 62.5,
    "ema20": 64500.0,
    "ema50": 63000.0,
    "macd_hist": 150.5,
    "atr": 500.0,
    "atr_ma20": 480.0,
    "bb_up": 66000.0,
    "bb_low": 63000.0,
    "htf_trend": "bullish",
    "sma200_15m": 62000.0,
    "recent_close_changes_pct": [0.1, -0.05, 0.2, 0.15, -0.1],
    "btc_trend": "bullish",
    "btc_change_15m": 0.5
  },
  "ETHUSDT": {
    "symbol": "ETHUSDT",
    "price": 3500.0,
    "rsi": 45.0,
    "ema20": 3520.0,
    "ema50": 3550.0,
    "macd_hist": -10.5,
    "atr": 50.0,
    "atr_ma20": 55.0,
    "bb_up": 3600.0,
    "bb_low": 3400.0,
    "htf_trend": "bearish",
    "sma200_15m": 3600.0,
    "recent_close_changes_pct": [-0.2, -0.1, 0.05, -0.15, 0.1],
    "btc_trend": "bullish",
    "btc_change_15m": 0.5
  }
}

payload = {
    "model": "llama",
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(mock_data, ensure_ascii=False)}
    ],
    "temperature": 0.2
}
# Note: we are not adding 'response_format': {'type': 'json_object'} here 
# just to see how the raw text comes back, but standard ai_signal.py uses it.
# Let's add it to perfectly simulate the real code.
payload["response_format"] = {"type": "json_object"}

req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode('utf-8'),
    headers={'Content-Type': 'application/json'},
    method='POST'
)

print("正在發送模擬資料到本地 AI...")
try:
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode('utf-8'))
        content = result['choices'][0]['message'].get('content', '')
        reasoning = result['choices'][0]['message'].get('reasoning_content', '')
        
        if reasoning:
            print("\n--- 🧠 AI 思考過程 (Reasoning) ---")
            print(reasoning)
        
        print("\n--- 🤖 AI 回傳結果 (JSON) ---")
        print(content)
except Exception as e:
    print(f"Error: {e}")
