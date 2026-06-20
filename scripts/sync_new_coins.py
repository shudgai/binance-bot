import json
import os
import sys

# 增加路徑支援
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from auto_cluster_coins import cluster_coins
except ImportError:
    # 備用路徑導入
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scripts.auto_cluster_coins import cluster_coins

def sync_configs():
    # 使用相對於專案根目錄的絕對路徑
    base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bot_symbols_path = os.path.join(base_path, "bot_symbols.json")
    profiles_path = os.path.join(base_path, "config", "coin_profiles.json")

    # 1. 讀取目標幣種清單
    if not os.path.exists(bot_symbols_path):
        print(f"❌ 找不到目標幣種清單檔案: {bot_symbols_path}")
        return
        
    with open(bot_symbols_path, 'r', encoding='utf-8') as f:
        bot_symbols = json.load(f)
    
    # 2. 讀取現有配置
    if os.path.exists(profiles_path):
        try:
            with open(profiles_path, 'r', encoding='utf-8') as f:
                current_profiles = json.load(f)
        except Exception:
            current_profiles = {}
    else:
        current_profiles = {}

    # 找出尚未在配置檔中的新幣種
    new_symbols = [sym for sym in bot_symbols if sym not in current_profiles]

    if not new_symbols:
        print("✅ 所有幣種配置已同步完成，無需更新。")
        return

    print(f"🔍 發現新幣種 {len(new_symbols)} 個: {new_symbols}，正在進行自動分析與參數初始化...")
    
    # 3. 呼叫 auto_cluster_coins 模組進行特徵分類
    try:
        clusters = cluster_coins(new_symbols)
        
        new_count = 0
        for sym, data in clusters.items():
            current_profiles[sym] = data["config"]
            new_count += 1
            print(f"✅ 已為 {sym} 自動產生建議配置 ({data['cluster']} 類別)")
            
        # 4. 寫回配置文件
        if new_count > 0:
            os.makedirs(os.path.dirname(profiles_path), exist_ok=True)
            with open(profiles_path, 'w', encoding='utf-8') as f:
                json.dump(current_profiles, f, indent=4, ensure_ascii=False)
            print(f"🚀 配置同步完成！共為 {new_count} 個新幣種產生預設參數。")
        else:
            print("⚠️ 未能成功分析產生任何新幣種配置。")
            
    except Exception as e:
        print(f"🚨 自動分類分析過程出錯: {e}")

if __name__ == "__main__":
    sync_configs()
