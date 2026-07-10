import time
import threading

class BinanceDataCache:
    """全市場 ticker 快取，降低重複查價的 API 權重消耗。

    傳入的 exchange_client 必須是同步的 python-binance client（例如
    services.binance_service.client），不能是 core.exchange_client.exchange_futures
    那個非同步的 ccxt 實例——原本直接呼叫 exchange_client.fetch_tickers()（ccxt 的
    非同步方法）卻沒有 await，等於每次都在存一個從未真正執行的 coroutine 物件，
    快取永遠是空的，被 fetch_and_cache 自己的 try/except 悄悄吞掉，功能形同虛設。
    改用同步 client 的 futures_ticker()（不帶 symbol，一次查全市場）。"""

    def __init__(self, exchange_client):
        self.client = exchange_client
        self.ticker_cache = {}
        self.last_update_time = 0
        # 全市場 futures_ticker() 權重高達 40（見 get_all_prices() 的說明），
        # 原本設 1 秒更新一次等於每秒打一次權重 40 的重量級請求，會把 API 權重
        # 衝到很誇張的程度（40 * 60 = 2400/分鐘），正是這個專案過去多次因為
        # API 權重/IP 封鎖吃過苦頭的那種模式。拉長到 5 秒，對交易判斷用的即時性
        # 仍然足夠，但大幅降低權重消耗。
        self.update_interval = 5.0
        self.lock = threading.Lock()  # 確保多執行緒安全

    def fetch_and_cache(self):
        """從幣安獲取最新全市場數據並存入快取，key 統一用原始交易對代號
        （例如 'BTCUSDT'，不含斜線），跟這個專案其餘程式碼慣用的符號格式一致。"""
        try:
            tickers = self.client.futures_ticker()
            with self.lock:
                self.ticker_cache = {
                    t["symbol"]: {**t, "price": float(t.get("lastPrice", 0) or 0)}
                    for t in tickers
                    if t.get("symbol")
                }
                self.last_update_time = time.time()
        except Exception as e:
            print(f"❌ [快取錯誤] 無法更新數據: {e}")

    def get_ticker(self, symbol):
        """
        讓所有服務從快取中讀取數據
        :param symbol: 交易對 (例如 'BTCUSDT')
        :return: 字典格式的 ticker 數據，至少含 'price' 欄位
        """
        current_time = time.time()

        # 如果快取過期，則重新抓取
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
        :return: 字典格式的 tickers，key 為交易對代號
        """
        current_time = time.time()

        # 如果快取過期，則重新抓取
        if current_time - self.last_update_time > self.update_interval:
            self.fetch_and_cache()
            # 如果抓取失敗或還沒抓到，稍微等一下再試一次
            if not self.ticker_cache:
                time.sleep(0.1)
                return {}

        with self.lock:
            return self.ticker_cache
