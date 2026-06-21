import os
import ccxt
import time
import numpy as np

# 初始化 Binance 交易所 (不需 API Key 即可讀取公開 K 線)
exchange = ccxt.binance({
    'enableRateLimit': True,
})

def fetch_historical_data(symbol, timeframe='1d', days=30):
    """
    獲取過去 N 天的 K 線數據
    """
    try:
        since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since)
        return ohlcv
    except Exception as e:
        print(f"Error fetching data for {symbol}: {e}")
        return []

def calculate_scores(ohlcv):
    """
    計算 Volatility_Score 和 Liquidity_Score
    """
    if not ohlcv or len(ohlcv) < 2:
        return 0, 0
        
    prices = [candle[4] for candle in ohlcv] # Close prices
    volumes = [candle[5] for candle in ohlcv] # Volumes
    
    # 計算 ATR (真實波動幅度) 的簡化版
    trues_ranges = []
    for i in range(1, len(ohlcv)):
        high = ohlcv[i][2]
        low = ohlcv[i][3]
        prev_close = ohlcv[i-1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trues_ranges.append(tr)
        
    avg_atr = np.mean(trues_ranges)
    avg_price = np.mean(prices)
    avg_volume = np.mean(volumes)
    
    # 波動係數 (ATR / Price)
    volatility_score = avg_atr / avg_price if avg_price > 0 else 0
    
    # 流動性得分 (Volume * Price, 即成交額)
    liquidity_score = avg_volume * avg_price
    
    return volatility_score, liquidity_score

def cluster_coins(symbols):
    """
    根據分數對幣種進行分類並生成配置
    """
    results = {}
    print("開始分析幣種數據...")
    
    for sym in symbols:
        ohlcv = fetch_historical_data(sym)
        vol_score, liq_score = calculate_scores(ohlcv)
        
        if vol_score == 0:
            continue
            
        print(f"[{sym}] 波動率: {vol_score*100:.2f}%, 日均成交額: {liq_score:,.0f}")
        
        # 簡易的分類邏輯 (閾值可根據實際市場情況微調)
        # 假設波動率 > 8% 為 Wild，> 4% 為 Momentum，否則為 Stable
        # 假設流動性 < 1000萬 為低流動性 (更傾向 Wild)
        
        if vol_score > 0.08 or (vol_score > 0.06 and liq_score < 10000000):
            cluster = "Wild"
            config = {
                "sl_atr_multiplier": 4.0, 
                "tp_atr_multiplier": 8.0, 
                "volume_threshold_factor": 2.5, 
                "breakeven_trigger": 0.8,
                "profile_type": "Wild", 
                "leverage": 2, 
                "mmp": 0.01, 
                "volatility_circuit_breaker": True
            }
        elif vol_score > 0.04:
            cluster = "Momentum"
            config = {
                "sl_atr_multiplier": 2.5, 
                "tp_atr_multiplier": 5.0, 
                "volume_threshold_factor": 1.5, 
                "breakeven_trigger": 0.6,
                "profile_type": "High_Beta_Momentum", 
                "leverage": 4, 
                "mmp": 0.006,
                "volatility_circuit_breaker": False
            }
        else:
            cluster = "Stable"
            config = {
                "sl_atr_multiplier": 2.0, 
                "tp_atr_multiplier": 4.0, 
                "volume_threshold_factor": 1.2, 
                "breakeven_trigger": 0.5,
                "profile_type": "Core_Trend", 
                "leverage": 8, 
                "mmp": 0.003,
                "volatility_circuit_breaker": False
            }
            
        results[sym] = {"cluster": cluster, "config": config}
        # 避免觸發 API 頻率限制
        time.sleep(0.5)
        
    return results

if __name__ == "__main__":
    import json
    
    # 測試用的幣種清單 (未來可改為讀取 bot_symbols.json)
    try:
        with open('bot_symbols.json', 'r') as f:
            data = json.load(f)
            if isinstance(data, dict):
                test_symbols = data.get("symbols", [])
            else:
                test_symbols = data
    except FileNotFoundError:
        test_symbols = ["BTCUSDT", "SOLUSDT", "SUIUSDT", "HUSDT", "PEPEUSDT"]
        
    clusters = cluster_coins(test_symbols)
    
    print("\n=== 分類結果 ===")
    
    # 提取 config 用於儲存
    final_configs = {}
    for sym, data in clusters.items():
        print(f"{sym} -> {data['cluster']}")
        print(f"建議配置: {data['config']}\n")
        final_configs[sym] = data['config']
    
    # 儲存為 JSON 檔案
    os.makedirs('config', exist_ok=True)
    with open('config/coin_profiles.json', 'w') as f:
        json.dump(final_configs, f, indent=4)
        
    print("✅ 幣種配置已自動分析並更新至 config/coin_profiles.json")
