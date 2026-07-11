import os
import time
import threading

class BinanceDataCache:
    """全市場價格快取，降低重複查價的 API 權重消耗。

    傳入的 exchange_client 必須是同步的 python-binance client（例如
    services.binance_service.client），不能是 core.exchange_client.exchange_futures
    那個非同步的 ccxt 實例——原本直接呼叫 exchange_client.fetch_tickers()（ccxt 的
    非同步方法）卻沒有 await，等於每次都在存一個從未真正執行的 coroutine 物件，
    快取永遠是空的，被 fetch_and_cache 自己的 try/except 悄悄吞掉，功能形同虛設。

    資料來源用 futures_mark_price()（不帶 symbol，一次查全市場）而不是
    futures_ticker()：兩者都能拿到全市場即時價格，但實測 futures_ticker() 全市場
    權重高達 40，futures_mark_price() 全市場只要 10，便宜 4 倍。這個專案過去已經
    因為權重問題吃過好幾次苦頭（IP 被封鎖、-1003 錯誤），之前 get_all_prices() 也
    是為了同樣理由才從 futures_ticker() 改寫成只查監控池內幣種——選權重更低的全市場
    端點，等於同時保有「一次拿到全市場資料、不用逐檔查」的效率，又避開最貴的那個
    端點。"""

    def __init__(self, exchange_client):
        self.client = exchange_client
        self.ticker_cache = {}
        self.last_update_time = 0
        # 全市場端點單次權重 10；15 秒足夠給非關鍵價格 fallback 使用。
        self.update_interval = max(5.0, float(os.getenv("MARK_PRICE_CACHE_SEC", "15")))
        self.lock = threading.Lock()  # 確保多執行緒安全

    def fetch_and_cache(self):
        """從幣安獲取最新全市場標記價格並存入快取，key 統一用原始交易對代號
        （例如 'BTCUSDT'，不含斜線），跟這個專案其餘程式碼慣用的符號格式一致。"""
        try:
            tickers = self.client.futures_mark_price()
            with self.lock:
                self.ticker_cache = {
                    t["symbol"]: {**t, "price": float(t.get("markPrice", 0) or 0)}
                    for t in tickers
                    if t.get("symbol")
                }
                self.last_update_time = time.time()
        except Exception as e:
            print(f"❌ [快取錯誤] 無法更新數據: {e}")
            return False
        return True

    def get_ticker(self, symbol):
        """
        讓所有服務從快取中讀取數據
        :param symbol: 交易對 (例如 'BTCUSDT')
        :return: 字典格式的 ticker 數據，至少含 'price' 欄位（標記價格）
        """
        current_time = time.time()

        # 如果快取過期，則重新抓取
        if current_time - self.last_update_time > self.update_interval:
            self.fetch_and_cache()

        with self.lock:
            return self.ticker_cache.get(symbol)

    def get_all_tickers(self):
        """
        獲取所有已快取的價格數據
        :return: 字典格式的 tickers，key 為交易對代號
        """
        current_time = time.time()

        # 如果快取過期，則重新抓取
        if current_time - self.last_update_time > self.update_interval:
            self.fetch_and_cache()

        with self.lock:
            return self.ticker_cache
