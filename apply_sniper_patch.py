import re
import os

def apply_patch():
    file_path = "multi_coin_bot.py"
    
    if not os.path.exists(file_path):
        print(f"❌ 錯誤：找不到 {file_path}")
        return

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    print(f"🔍 已讀取 {file_path}，開始執行狙擊手模式補丁...")

    # --- 1. 注入 __init__ 參數 ---
    # 尋找 __init__ 並在其中插入狙擊手參數
    # 我們尋找 self.min_entry_score 或類似的變數位置進行插入
    init_params = """
        self.min_entry_score = 13.0
        self.min_profit_room = 0.017  # 已更新為 1.7%
        
        # --- 新增：狙擊手專屬參數 ---
        # 做多時，RSI 落在 40-50 之間視為回調區 (趨勢向上但價格稍微放緩)
        self.pullback_rsi_long = (40.0, 50.0) 
        # 做空時，RSI 落在 50-60 之間視為反彈區 (趨勢向下但價格稍微回彈)
        self.pullback_rsi_short = (50.0, 60.0)
        # 價格折扣比例 (例如 0.1% = 0.001)
        self.price_offset = 0.001 
        # ----------------------------
        
        self.max_slots = 2
        self.leverage = 5
        self.order_timeout = 45
        self.max_hold_time = 4 * 3600
"""
    
    if "self.pullback_rsi_long" not in content:
        # 在 __init__ 的定義後插入
        content = re.sub(r'(def __init__(self):)', r'\1' + init_params, content)
        print("✅ 參數注入完成。")
    else:
        print("⚠️ 參數已存在，跳過注入。")

    # --- 2. 更新 check_hard_gates 函數 ---
    check_gate_new = """
    async def check_hard_gates(self, symbol, side, current_price, target_price, rsi_value):
        \"\"\"
        【第二層升級】硬門檻 + 狙擊手回調過濾
        \"\"\"
        # 1. 獲利空間門檻 (原本已有的)
        expected_profit = abs(target_price - current_price) / current_price
        if expected_profit < self.min_profit_room:
            return False, f"獲利空間不足 ({expected_profit:.2%})"

        # 2. 【新增】狙擊手回調過濾 (Pullback Filter)
        # 目的：確保我們不是在追高或殺跌，而是買在回調/賣在反彈
        if side == 'buy':
            # 做多時，RSI 必須在 40-50 之間 (回調區)
            if not (self.pullback_rsi_long[0] <= rsi_value <= self.pullback_rsi_long[1]):
                return False, f"Wait for Pullback (RSI: {rsi_value:.1f} outside {self.pullback_rsi_long})"
        else:
            # 做空時，RSI 必須在 50-60 之間 (反彈區)
            if not (self.pullback_rsi_short[0] <= rsi_value <= self.pullback_rsi_short[1]):
                return False, f"Wait for Rebound (RSI: {rsi_value:.1f} outside {self.pullback_rsi_short})"

        return True, "Passed"
"""
    # 使用正則表達式匹配整個 check_hard_gates 函數塊
    # 匹配從 'async def check_hard_gates' 開始到下一個 'async def' 或 'def' 為止的內容
    pattern_check = r'async def check_hard_gates\(.*?\):.*?(\n\s*(async\s+def|def)|$)'
    content = re.sub(pattern_check, check_gate_new, content, flags=re.DOTALL)
    print("✅ 回調過濾邏輯更新完成。")

    # --- 3. 更新 place_right_side_limit_order 函數 ---
    place_order_new = """
    async def place_right_side_limit_order(self, symbol, side, amount, entry_price, ema20_15m):
        \"\"\"
        【第四層升級】狙擊手對價掛單 (Discount Limit Order)
        \"\"\"
        ticker = await self.exchange.fetch_ticker(symbol)
        
        # 判定是否為 15m EMA 壓制下的逆勢做多
        is_counter_trend = side == 'buy' and entry_price < ema20_15m
        
        if is_counter_trend:
            # 逆勢做多：掛在 Ask1 的上方一點點 (等待反彈確認)
            # 因為是逆勢，我們稍微放寬一點，確保能成交
            best_price = ticker['ask'] * (1 + self.price_offset * 0.5) 
            route_log = f"Ask1+ 逆勢反彈迎擊 (Offset: {self.price_offset*100}%)"
        else:
            # 順勢單：掛在 Bid1 的下方一點點 (確保拿低點)
            if side == 'buy':
                best_price = ticker['bid'] * (1 - self.price_offset)
                route_log = f"Bid1- 順勢回調狙擊 (Offset: {self.price_offset*100}%)"
            else:
                best_price = ticker['ask'] * (1 + self.price_offset)
                route_log = f"Ask1+ 順勢反彈狙擊 (Offset: {self.price_offset*100}%)"
            
        print(f"⚡ [狙擊手掛單] {symbol} {side} 價格: {best_price:.4f} | {route_log}")
        
        # 實戰下單邏輯 (與原代碼一致)
        order_id = f"sniper_id_{int(time.time())}"
        self.pending_limit_orders[symbol] = {
            "order_id": order_id,
            "timestamp": int(time.time() * 1000),
            "side": side,
            "amount": amount
        }
        self.coin_states[symbol] = "PENDING"
"""
    pattern_place = r'async def place_right_side_limit_order\(.*?\):.*?(\n\s*(async\s+def|def)|$)'
    content = re.sub(pattern_place, place_order_new, content, flags=re.DOTALL)
    print("✅ 折扣掛單邏輯更新完成。")

    # 寫回檔案
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("\n🎉 所有補丁已成功套用！")
    print("💡 請檢查日誌，確認 [Wait for Pullback] 是否如預期運作。")

if __name__ == "__main__":
    apply_patch()
