import ccxt
import time
import threading

class BinanceDataCache:
    def __init__(self, exchange_client):
        self.client = exchange_client
        self.ticker_cache = {}
        self.last_update_time = 0
        self.update_interval = 1.0  # 每 1 秒更新一次全市場數據
        self.lock = threading.Lock()  # 確保多執行緒安全

    def fetch_and_cache(self):
        """從幣安獲取最新數據並存入快取"""
        try:
            # 只抓取基礎的 ticker 數據（包含當前價、成交量、變動等）
            tickers = self.client.fetch_tickers()
            with self.lock:
                self.ticker_cache = tickers
                self.last_update_time = time.time()
            print(f"✅ [快取更新] 已成功更新全市場數據 (時間: {time.strftime('%H:%M:%S')})")
        except Exception as e:
            print(f"❌ [快取錯誤] 無法更新數據: {e}")

    def get_ticker(self, symbol):
        """
        讓所有服務從快取中讀取數據
        :param symbol: 交易對 (例如 'BTC/USDT')
        :return: 字典格式的 ticker 數據
        """
        current_time = time.time()
        
        # 如果快取過期（超過 1 秒），則重新抓取
        if current_time - self.last_update_time > self.update_interval:
            self.fetch_and_cache()
            # 如果抓取失敗或還沒抓到，稍微等一下再試一次
            if not self.ticker_cache:
                time.sleep(0.1)
                return self.get_ticker(symbol)

        with self.lock:
            return self.ticker_cache.get(symbol)

    def get_all_tickers(self):
        """
        獲取所有已快取的 ticker 數據
        :return: 字典格式的 tickers
        """
        current_time = time.time()
        
        # 如果快取過期（超過 1 秒），則重新抓取
        if current_time - self.last_update_time > self.update_interval:
            self.fetch_and_cache()
            # 如果抓取失敗或還沒抓到，稍微等一下再試一次
            if not self.ticker_cache:
                time.sleep(0.1)
                return {}

        with self.lock:
            return self.ticker_cache