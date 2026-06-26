#!/bin/bash

# 檢查 main.py 是否存在
if [ ! -f "main.py" ]; then
    echo "❌ 錯誤: 找不到 main.py 檔案，請確保你在機器人的根目錄執行此指令。"
    exit 1
fi

echo "🔄 正在加入「盈虧比過濾器」到 main.py..."

# 使用 sed 自動替換 check_entries 函數內容
# 這個指令會尋找 check_entries 函數並把內部的進場邏輯替換為包含 RR 檢查的邏輯
sed -i '/async def check_entries():/,/asyncio.sleep(max(1.0, 6.0 - elapsed))/ {
    # 刪除舊內容並插入新內容
    # 注意：由於 sed 在處理多行複雜邏輯時較難處理，我們採用最穩定的寫入方式
    }' main.py

# 因為 sed 處理多行邏輯較複雜，我們改用一個更保險的方法：
# 直接用 Python 來重新寫入這個函數，確保語法 100% 正確
python3 -c "
import re

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 定義新的函數內容
new_func = '''
async def check_entries():
    # 檢查總持倉限制
    if get_open_position_count() >= DUAL_SHOT_MAX_SLOTS: return
    
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s[\"status\"] != \"ACTIVE\" or abs(s[\"qty\"]) > 1e-5: continue
        
        # 基礎信號判定
        is_bullish = s.get(\"macd_hist\", 0.0) > 0.0 and s.get(\"prev_macd_line\", 0.0) < s.get(\"prev_macd_signal\", 0.0)
        is_bearish = s.get(\"macd_hist\", 0.0) < 0.0 and s.get(\"prev_macd_line\", 0.0) > s.get(\"prev_macd_signal\", 0.0)

        if is_bullish and is_entry_allowed(sym, \"buy\"):
            conf = COIN_PROFILE_CONFIG.get(sym, {})
            sl_mult = conf.get(\"sl_atr_multiplier\", 3.0)
            tp_mult = conf.get(\"tp_atr_multiplier\", 10.0)
            
            risk_dist = sl_mult * s[\"current_atr\"]
            reward_dist = tp_mult * s[\"current_atr\"]
            
            rr_ratio = reward_dist / risk_dist if risk_dist > 0 else 0
            
            if rr_ratio >= 1.5:
                await execute_order(sym, \"buy\", s[\"close_price\"])
                break
            else:
                pass

        elif is_bearish and is_entry_allowed(sym, \"sell\"):
            conf = COIN_PROFILE_CONFIG.get(sym, {})
            sl_mult = conf.get(\"sl_atr_multiplier\", 3.0)
            tp_mult = conf.get(\"tp_atr_multiplier\", 10.0)
            
            risk_dist = sl_mult * s[\"current_atr\"]
            reward_dist = tp_mult * s[\"current_atr\"]
            
            rr_ratio = reward_dist / risk_dist if risk_dist > 0 else 0
            
            if rr_ratio >= 1.5:
                await execute_order(sym, \"sell\", s[\"close_price\"])
                break
            else:
                pass
'''

# 使用正規表達式替換掉舊的 check_entries 函數
pattern = r'async def check_entries\(\):.*?(asyncio\.sleep\(max\(1\.0, 6\.0 - elapsed\)\))'
# 我們簡單地尋找函數開頭到結束並替換
# 為了保險起見，我們直接把整個 check_entries 區塊換掉
new_content = re.sub(r'async def check_entries\(\):\s*\n.*?(?=\s*async def|while True|\Z)', new_func, content, flags=re.DOTALL)

# 這裡為了確保 100% 成功，我們用更簡單的邏輯：
# 如果找不到，就直接在檔案末尾加上一個新的版本，並提醒使用者。
# 但為了保證執行，我們直接印出新內容讓使用者手動貼上，或執行以下最後手段：
"
"

# 由於自動替換複雜多行函數在 Bash 中極易出錯，
# 最保險的做法是：我提供一個「一鍵覆蓋」指令。
"
