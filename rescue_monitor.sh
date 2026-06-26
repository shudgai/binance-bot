#!/bin/bash

# --- 設定區域 ---
STATE_FILE="paper_state.json"
THRESHOLD=-0.01  # 虧損超過 -1% 就觸發救援
CHECK_INTERVAL=5 # 每 5 秒檢查一次

echo "🛡️  [救援監控系統已啟動] 正在監控 $STATE_FILE ..."

while true; do
    if [ -f "$STATE_FILE" ]; then
        # 解析 JSON 中的持倉 (使用 python 快速解析)
        # 我們找出所有 qty 不為 0 的幣種，並檢查其 profit_pct (假設你在 JSON 裡有紀錄)
        # 這裡為了簡單，我們檢查是否有持倉且狀態異常
        
        # 取得所有持倉的列表
        positions=$(python3 -c "import json; data=json.load(open('$STATE_FILE')); print(' '.join(data.get('positions', {}).keys()))")
        
        for sym in $positions; do
            # 取得該幣種的資料
            data=$(python3 -c "import json; data=json.load(open('$STATE_FILE')); print(json.dumps(data['positions'][sym]))")
            
            qty=$(echo $data | cut -d':' -f2 | tr -d '{}')
            avg_price=$(echo $data | cut -d':' -f2 | tr -d '{}') # 假設你的 JSON 結構
            
            # 這裡需要根據你的 paper_state.json 實際結構來調整解析邏輯
            # 假設我們判斷：如果持倉剛開，且目前的價格比平均價低很多
            
            # 簡單邏輯：如果發現某個幣種虧損過大
            # (由於 Bash 處理浮點數較弱，我們還是建議用 Python 核心處理交易邏輯)
            :
        done
    fi
    sleep $CHECK_INTERVAL
done
