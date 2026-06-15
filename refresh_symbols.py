import os
import asyncio
import ccxt.async_support as ccxt
import json
from dotenv import load_dotenv

load_dotenv()

from services.symbol_manager import fetch_top_volume_symbols, write_bot_symbols, read_bot_symbols

async def main():
    print("🔄 開始進行幣種池定期洗牌 (Refresh Symbols)...")
    
    exchange = ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY') or None,
        'secret': os.getenv('BINANCE_API_SECRET') or None,
        'options': {
            'defaultType': 'swap',
        },
    })
    
    USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
    if USE_TESTNET:
        exchange.urls['api']['fapiPublic'] = 'https://testnet.binancefuture.com/fapi/v1'
        exchange.urls['api']['fapiPrivate'] = 'https://testnet.binancefuture.com/fapi/v1'
        
    try:
        old_symbols = read_bot_symbols()
        print(f"📄 舊幣種清單 ({len(old_symbols)}): {old_symbols}")
        
        # 抓取前 20 名最高成交額且符合條件的幣種
        new_symbols = await fetch_top_volume_symbols(exchange, limit=20)
        
        if new_symbols:
            write_bot_symbols(new_symbols)
            
            added = [s for s in new_symbols if s not in old_symbols]
            removed = [s for s in old_symbols if s not in new_symbols]
            
            print(f"✅ 更新完成！新幣種清單 ({len(new_symbols)}): {new_symbols}")
            if added:
                print(f"📈 新增幣種: {added}")
            if removed:
                print(f"📉 淘汰幣種: {removed}")
            print("\n請注意：正在運行的 multi_coin_bot_v2.py 會自動偵測 bot_symbols.json 並熱更新幣種池。")
        else:
            print("⚠️ 獲取新幣種失敗，請檢查網路或 API。")
            
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(main())
