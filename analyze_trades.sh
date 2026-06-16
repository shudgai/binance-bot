#!/bin/bash

# 檢查檔案是否存在
FILE="trade_log.txt"
if [ ! -f "$FILE" ]; then
    echo "錯誤: 找不到 $FILE 檔案，請確保資料已存入該檔案。"
    exit 1
fi

echo "=============================================================="
echo "                交易數據分析報告 (含勝率統計)                  "
echo "=============================================================="
printf "%-18s | %-8s | %-12s | %-8s\n" "時間" "幣種" "實質損益" "狀態"
echo "--------------------------------------------------------------"

# 使用 awk 處理數據
# 1. 使用 Tab 作為分隔符 (-F'\t')
# 2. 跳過第一行 (NR > 1)
# 3. 僅處理含有 "實:" 字樣的行 (避免處理未結算的行)
# 4. 提取 "實: " 後面的數字
awk -F'\t' '
NR > 1 && $7 ~ /實:/ {
    # 使用 match 函數提取 實: 後面的數字 (包含正負號與小數點)
    match($7, /實: ([-+]?[0-9.]+)/, arr);
    val = arr[1];
    
    # 判斷狀態
    if (val > 0) {
        status = "WIN";
        wins++;
    } else if (val < 0) {
        status = "LOSS";
        losses++;
        loss_list = loss_list $1 " ("$2") "; # 紀錄虧損的交易時間與幣種
    } else {
        status = "BREAK";
    }

    # 累加統計
    total_trades++;
    total_pnl += val;

    # 輸出每一行的結果
    # 這裡將 LOSS 用特殊方式視覺化，方便你在終端機一眼看出
    if (status == "LOSS") {
        printf "\033[0;31m%-18s | %-8s | %-12.4f | %-8s\033[0m\n", $1, $2, val, status;
    } else {
        printf "%-18s | %-8s | %-12.4f | %-8s\n", $1, $2, val, status;
    }
}

END {
    if (total_trades > 0) {
        win_rate = (wins / total_trades) * 100;
        print "--------------------------------------------------------------";
        printf "📊 統計結果:\n";
        printf "   總交易筆數: %d\n", total_trades;
        printf "   獲利筆數 (WIN): %d\n", wins;
        printf "   虧損筆數 (LOSS): %d\n", losses;
        printf "   目前勝率: %.2f%%\n", win_rate;
        printf "   總計實質盈虧: %.4f\n", total_pnl;
        
        if (total_pnl < 0) {
            print "\n⚠️ 警告: 目前總盈虧為負！請檢視交易策略。";
        } else {
            print "\n✅ 恭喜: 目前總盈虧為正！";
        }
    } else {
        print "未偵測到有效的交易結算數據。";
    }
}' "$FILE"
echo "=============================================================="
