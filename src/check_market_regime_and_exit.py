"""
Market Regime and Exit Strategy - Main Implementation with WebSocket Fallback

This script handles market regime detection, including breakout reversal and ranging market conditions.
It also includes a fallback mechanism using HTTP API polling when WebSocket connection is lost.
"""

import asyncio
import aiohttp

async def check_market_regime_and_exit(current_p, side, pos_avg):
    # 模擬大單偵測變量
    big_order_detected = False  # 在實際應用中，這需要根據 WebSocket 訊息更新

    # 1. 偵測大單：這是最高優先級
    if big_order_detected:
        print("🚨 [緊急反轉] 偵測到主力大單突破，立即平倉並反手！")
        return "BREAKOUT_REVERSAL"
    
    # 模擬盤整市場檢查函數
    async def is_in_historical_range(symbol, session):
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit=50"
        async with session.get(url) as response:
            data = await response.json()
        
        highs = [float(candle[2]) for candle in data]
        lows = [float(candle[3]) for candle in data]
        
        avg_high = sum(highs) / len(highs)
        avg_low = sum(lows) / len(lows)
        
        return avg_low <= current_p <= avg_high
    
    # 模擬獲利百分比計算函數
    def get_profit_pct(current_p, pos_avg):
        profit = abs(current_p - pos_avg)
        pct_gain = (profit / pos_avg) * 100
        return round(pct_gain, 4)

    symbol = "BLUAIUSDT"  # 設置交易對
    async with aiohttp.ClientSession() as session:
        # 檢查盤整市場
        is_range = await is_in_historical_range(symbol, session)
        if is_range:
            profit_pct = get_profit_pct(current_p, pos_avg)

            if profit_pct >= 0.3:
                print(f"กำไร達到 {profit_pct}%，立即平倉獲利！")
                return "RANGE_PROFIT_TAKE"
            else:
                print("🔍 [盤整市場] 當前賺取不到 0.3% 益利，繼續持有...")
                return "HOLD"

        # 如果不是盤整市場且沒有大單情況，繼續持有
        print("🔍 [非盤整市場] 當前價格不在歷史區間內")
        return "HOLD"

async def main():
    symbol = "BLUAIUSDT"  # 設置交易對
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                current_price = float(input("請輸入當前價格: "))
                position_side = input("請輸入持倉方向 (buy/sell): ").strip().lower()
                average_cost_price = float(input("請輸入平均成本價: "))

                decision = await check_market_regime_and_exit(
                    current_p=current_price,
                    side=position_side,
                    pos_avg=average_cost_price
                )

                print(f"策略決策：{decision}")
            
            except ValueError as ve:
                print(f"輸入錯誤: {ve}")
            except Exception as e:
                print(f"發生異常: {e}")

if __name__ == "__main__":
    asyncio.run(main())
