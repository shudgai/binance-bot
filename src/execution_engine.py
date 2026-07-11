import logging
import time
import os
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

MAX_ORDER_VALUE_USDT = 50.0

class ExecutionEngine:
    """
    核心執行引擎，負責處理交易邏輯、訂單管理與風險控制。
    """
    def __init__(self, exchange, config: Dict[str, Any] = None):
        self.exchange = exchange
        self.config = config or {}
        self.active_trades = {}
        logger.info("ExecutionEngine 初始化成功。")

    async def execute_trade(self, symbol: str, side: str, amount: float, price: float = None):
        """
        執行交易指令。符合工業標準：嚴格精度控制、最小金額檢查與動態精度獲取。
        """
        try:
            # --- 防護層 1: 緊急停止開關 (Kill Switch) ---
            if os.getenv('BOT_STOP_SIGNAL') == 'True':
                logger.critical("🛑 [KILL SWITCH ACTIVATED] 偵測到 BOT_STOP_SIGNAL 為 True。立即停止所有下單動作並進入休眠。")
                # 在實際應用中，這裡可能需要通知外部系統或進入更長久的休眠
                return {"status": "stopped", "reason": "kill_switch_active"}

            logger.info(f"正在處理 {side} 交易請求: {symbol}, 數量: {amount}")

            # 1. 確保獲取最新的交易所市場規則（包含精度等資訊）
            await self.exchange.fetch_markets()

            # 2. 最小金額檢查 (10 USDT 門檻)
            if price is not None:
                notional_value = amount * price
                if notional_value < 10:
                    logger.warning(
                        f"⚠️ 交易被跳過: {symbol} {side} 預估價值 {notional_value:.2f} USDT "
                        f"低於最小門檻 10 USDT (數量: {amount}, 價格: {price})"
                    )
                    return {"status": "skipped", "reason": "notional_value_too_low", "value": notional_value}

            # --- 防護層 2: 最大單筆限額 (Max Order Cap) ---
            if price is not None:
                notional_value = amount * price
                if notional_value > MAX_ORDER_VALUE_USDT:
                    logger.critical(
                        f"🚨 [MAX_ORDER_CAP_EXCEEDED] 交易請求被攔截！"
                        f"預估價值 {notional_value:.2f} USDT 超過最大限額 {MAX_ORDER_VALUE_USDT} USDT "
                        f"({symbol} {side})"
                    )
                    return {"status": "skipped", "reason": "max_order_value_exceeded", "value": notional_value}

            # 3. 使用交易所提供的 amount_to_precision 進行嚴格精度處理
            # CCXT 標準方法，確保量化符合交易所規範
            precision_amount = self.exchange.amount_to_precision(symbol, amount)
            
            logger.info(f"已校正精度: {amount} -> {precision_amount}")

            # 4. 執行實際訂單
            # 這裡假設使用的是 market 訂單，若為 limit 訂單可根據需求擴充參數
            order = await self.exchange.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=precision_amount
            )

            # --- 防護層 3: 滑點預警 (Slippage Alert) ---
            if price is not None and 'average' in order:
                actual_price = float(order['average'])
                slippage = abs(actual_price - price) / price
                if slippage > 0.01:
                    logger.warning(f"⚠️ [高滑點警告] {symbol} {side} | 預期價格: {price} | 實際成交價: {actual_price} | 滑點: {slippage:.2%}")

            logger.info(f"✅ 交易執行成功: {symbol} {side} | 訂單ID: {order.get('id')}")
            return order

        except Exception as e:
            logger.error(f"執行交易時發生錯誤: {e}")
            raise e

    def check_risk(self, symbol: str, side: str, price: float) -> bool:
        """
        預先檢查風險。
        """
        from core.ctx import CACHE
        
        # 使用快取獲取最新數據，確保 risk check 基於最新的市場價格
        ticker_data = CACHE.get_ticker(symbol)
        if ticker_data:
            # 如果快取有值，可以使用 ticker_data['last'] 作為參考價格
            # 此處保留傳入的 price 作為主要檢查基準，但可與快取數據比對以防異常
            pass

        # 這裡可以加入更多風險檢查邏輯
        return True

    async def manage_active_trades(self):
        """
        持續管理正在進行中的交易（如止損、止盈）。
        """
        while True:
            # 這裡將實作定時檢查邏輯
            await asyncio.sleep(1)
