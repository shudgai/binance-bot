import os
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException

def main():
    # 載入環境變數
    load_dotenv()
    
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    use_testnet = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")

    print("=== 初始化 Binance 交易機器人 ===")
    print(f"模式: {'[測試網 Testnet]' if use_testnet else '[實盤 Production]'}")
    
    # 建立 Binance Client 連線
    # 註：如果沒有填寫真正的 API Key，只做公開資訊查詢（如獲取價格）也是可行的
    try:
        if api_key == "your_api_key_here" or not api_key:
            print("提示: 未偵測到有效的 API Key，將以『唯讀模式』獲取市場公開數據。")
            client = Client(testnet=use_testnet)
        else:
            client = Client(api_key, api_secret, testnet=use_testnet)
            # 測試帳戶連線狀態
            account_info = client.get_account()
            print("✅ 成功連線帳戶！")
            
        # 測試獲取 BTC/USDT 的當前價格
        symbol = "BTCUSDT"
        ticker = client.get_symbol_ticker(symbol=symbol)
        print(f"📊 當前 {symbol} 價格: {ticker['price']} USD")
        
        # 測試系統狀態
        system_status = client.get_system_status()
        status_msg = "正常" if system_status.get("status") == 0 else "維護中"
        print(f"⚙️ 幣安系統狀態: {status_msg}")

    except BinanceAPIException as e:
        print(f"❌ 幣安 API 錯誤: {e.message} (代碼: {e.status_code})")
    except Exception as e:
        print(f"❌ 連線發生其他錯誤: {str(e)}")
        print("請確認網路連線是否正常，或是否需要設定代理 (Proxy)。")

if __name__ == "__main__":
    main()
