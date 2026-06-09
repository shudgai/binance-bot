import asyncio
import os
from dotenv import load_dotenv
from binance import AsyncClient, BinanceSocketManager

async def main():
    # 載入環境變數
    load_dotenv()
    
    # 建立非同步客戶端 (不需要 API Key 即可讀取公開 WebSocket)
    client = await AsyncClient.create()
    bm = BinanceSocketManager(client)
    
    symbol = "BTCUSDT"
    print(f"=== 開始即時監聽 {symbol} 價格變動 (WebSocket) ===")
    print("提示: 程式會連續讀取 10 次價格更新後自動停止，或可按 Ctrl+C 中斷。")
    print("-" * 50)
    
    # 建立 K 線 (kline) 或交易 (trade) 的監聽器
    # 這裡我們監聽 trade 頻道，每次有交易發生就會推送最新價格
    ts = bm.trade_socket(symbol)
    
    count = 0
    max_updates = 10
    
    try:
        async with ts as tscm:
            while count < max_updates:
                msg = await tscm.recv()
                
                # 幣安推送的資料格式中，'p' 代表成交價格 (Price)，'q' 代表成交量 (Quantity)
                if 'p' in msg:
                    price = float(msg['p'])
                    quantity = float(msg['q'])
                    count += 1
                    print(f"[{count:02d}] 🔔 即時價格: {price:,.2f} USDT | 成交量: {quantity:.4f}")
                    
    except KeyboardInterrupt:
        print("\n⏹️ 使用者中斷監聽。")
    except Exception as e:
        print(f"❌ 發生錯誤: {str(e)}")
    finally:
        # 關閉連線
        await client.close_connection()
        print("-" * 50)
        print("=== 監聽結束 ===")

if __name__ == "__main__":
    # 執行非同步主程式
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️ 程式已停止。")
