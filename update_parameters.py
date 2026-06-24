import re
import os

def update_params():
    file_path = "multi_coin_bot.py"
    
    if not os.path.exists(file_path):
        print(f"❌ 錯誤：找不到 {file_path}")
        return

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    print(f"🔍 正在讀取 {file_path} 並進行參數微調...")

    # --- 修改目標 ---
    # 1. 將 min_profit_room 從 0.017 改為 0.015
    # 2. 將 pullback_rsi_long 從 (40.0, 50.0) 改為 (35.0, 55.0)
    
    # 修改 min_profit_room (使用正則表達式匹配整行，確保不影響其他變數)
    new_profit_room = "        self.min_profit_room = 0.015  # 已微調為 1.5%"
    content = re.sub(r'self\.min_profit_room\s*=\s*0\.\d+', new_profit_room, content)

    # 修改 pullback_rsi_long
    new_rsi_long = "        self.pullback_rsi_long = (35.0, 55.0) "
    content = re.sub(r'self\.pullback_rsi_long\s*=\s*\(\d+\.\d+,\s*\d+\.\d+\)', new_rsi_long, content)

    # 寫回檔案
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("\n✅ 參數更新成功！")
    print(f"🔹 min_profit_room -> 0.015 (1.5%)")
    print(f"🔹 pullback_rsi_long -> (35.0, 55.0)")
    print("\n💡 這些變更將放寬進場條件，預計會增加成交頻率。")

if __name__ == "__main__":
    update_params()
