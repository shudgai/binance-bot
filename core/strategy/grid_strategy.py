import logging
import asyncio
import time
from core.strategy.base_strategy import BaseStrategy
from core.exchange_client import exchange_futures, convert_to_ccxt_symbol, get_contract_precision, round_step
from core import ctx
from core.config import PAPER_TRADING

logger = logging.getLogger(__name__)

class GridStrategy(BaseStrategy):
    def __init__(self, symbol):
        super().__init__(symbol)
        self.profile_type = "Grid_Trading"

    def get_profile_type(self) -> str:
        return self.profile_type

    async def init_grids(self):
        """初始化網格"""
        logger.info(f"🚀 [{self.symbol}] 正在初始化網格...")
        from core.symbol_profile import SYMBOL_PROFILES
        config = SYMBOL_PROFILES.get(self.symbol, {})
        upper = config.get("grid_upper", 0)
        lower = config.get("grid_lower", 0)
        count = config.get("grid_count", 10)
        
        if upper <= 0 or lower <= 0 or upper <= lower:
            logger.info(f"⚠️ [{self.symbol}] 網格上下限設定錯誤！ (Upper: {upper}, Lower: {lower})")
            return
            
        # 計算網格間距
        step_price = (upper - lower) / count
        
        # 取得目前價格
        try:
            ccxt_symbol = convert_to_ccxt_symbol(self.symbol)
            ticker = await exchange_futures.fetch_ticker(ccxt_symbol)
            current_price = ticker.get("last", 0)
        except Exception as e:
            logger.info(f"⚠️ [{self.symbol}] 無法取得目前價格，網格初始化失敗: {e}")
            return
            
        if current_price == 0:
            return

        # 每個網格的資金與數量計算
        capital_per_grid = config.get("capital_per_grid", 10.0) # 預設每格 10 USDT
        leverage = config.get("leverage", 5)
        
        grids = []
        prec = await get_contract_precision(self.symbol)
        
        for i in range(count + 1):
            price = lower + (step_price * i)
            price = round_step(price, prec["tick_size"])
            
            # 決定方向：高於現價掛賣(Sell)，低於現價掛買(Buy)
            side = "sell" if price > current_price else "buy"
            
            # 計算數量 (以名目價值 / 價格)
            notional = capital_per_grid * leverage
            qty = notional / price
            qty = round_step(qty, prec["step_size"])
            
            if qty < prec["min_qty"]:
                continue
                
            grids.append({
                "level": i,
                "price": price,
                "side": side,
                "qty": qty,
                "order_id": None,
                "status": "pending" # pending, filled
            })
            
        self.state["grids"] = grids
        self.state["grid_initialized"] = True
        logger.info(f"✅ [{self.symbol}] 網格初始化完成，共建立 {len(grids)} 個網格點。目前價格: {current_price}")
        
        # 開始掛單
        await self.place_grid_orders(grids)

    async def place_grid_orders(self, grids):
        """實際發送掛單請求"""
        ccxt_symbol = convert_to_ccxt_symbol(self.symbol)
        for grid in grids:
            if grid["order_id"] is not None or grid["status"] == "filled":
                continue
                
            try:
                if PAPER_TRADING:
                    # 模擬訂單
                    grid["order_id"] = f"paper_grid_{int(time.time()*1000)}_{grid['level']}"
                    logger.info(f"📝 [紙上網格] 虛擬掛單 {self.symbol} {grid['side'].upper()} 數量: {grid['qty']} 價格: {grid['price']}")
                else:
                    async with ctx.request_semaphore:
                        order = await exchange_futures.create_order(
                            ccxt_symbol, "limit", grid["side"], grid["qty"], grid["price"],
                            params={"timeInForce": "GTC"}
                        )
                        grid["order_id"] = order["id"]
                        logger.info(f"網格掛單成功 {self.symbol}: {grid['side']} @ {grid['price']}")
            except Exception as e:
                logger.info(f"⚠️ [{self.symbol}] 網格掛單失敗 (Level {grid['level']}): {e}")

    async def run(self, sym=None):
        """執行網格維護邏輯 (此方法由 runner 主循環定期呼叫)"""
        state = self.state
        if not state.get("grid_initialized"):
            await self.init_grids()
            return
            
        # 檢查訂單狀態並補單
        grids = state.get("grids", [])
        if not grids:
            return
            
        # 在這裡實作訂單成交後的反向補單邏輯
        # 因應 API 限制，實盤環境會需要 fetch_open_orders() 來比對
        if PAPER_TRADING:
            await self._simulate_paper_grid_fills(grids)
        else:
            await self._check_live_grid_fills(grids)

    async def _simulate_paper_grid_fills(self, grids):
        """紙上交易環境的網格撮合模擬"""
        try:
            ccxt_symbol = convert_to_ccxt_symbol(self.symbol)
            ticker = await exchange_futures.fetch_ticker(ccxt_symbol)
            current_price = ticker.get("last", 0)
            if current_price == 0:
                return
                
            for grid in grids:
                if grid["order_id"] and grid["status"] == "pending":
                    # 判斷是否穿越價格
                    is_filled = False
                    if grid["side"] == "buy" and current_price <= grid["price"]:
                        is_filled = True
                    elif grid["side"] == "sell" and current_price >= grid["price"]:
                        is_filled = True
                        
                    if is_filled:
                        logger.info(f"💸 [紙上網格成交] {self.symbol} {grid['side'].upper()} @ {grid['price']}")
                        grid["status"] = "filled"
                        grid["order_id"] = None
                        # 反向補單邏輯 (Buy 成交就掛 Sell，反之亦然)
                        await self._place_reverse_grid(grids, grid)
                        
        except Exception as e:
            pass

    async def _check_live_grid_fills(self, grids):
        """實盤環境比對未成交訂單，判斷網格是否成交"""
        try:
            ccxt_symbol = convert_to_ccxt_symbol(self.symbol)
            async with ctx.request_semaphore:
                open_orders = await exchange_futures.fetch_open_orders(ccxt_symbol)
                
            open_order_ids = {str(o["id"]) for o in open_orders}
            
            for grid in grids:
                if grid["order_id"] and grid["status"] == "pending":
                    if str(grid["order_id"]) not in open_order_ids:
                        # 訂單不在 Open 列表中，假設已成交 (此處實務上應進一步呼叫 fetch_order 確認 status == 'closed')
                        logger.info(f"💰 [實盤網格成交] {self.symbol} {grid['side'].upper()} @ {grid['price']} 已成交！")
                        grid["status"] = "filled"
                        grid["order_id"] = None
                        await self._place_reverse_grid(grids, grid)
                        
        except Exception as e:
            logger.info(f"⚠️ [{self.symbol}] 檢查實盤網格訂單失敗: {e}")

    async def _place_reverse_grid(self, grids, filled_grid):
        """網格補單：買單成交掛賣單，賣單成交掛買單"""
        prec = await get_contract_precision(self.symbol)
        # 尋找上下相鄰的網格位置
        from core.symbol_profile import SYMBOL_PROFILES
        config = SYMBOL_PROFILES.get(self.symbol, {})
        count = config.get("grid_count", 10)
        upper = config.get("grid_upper", 0)
        lower = config.get("grid_lower", 0)
        step_price = (upper - lower) / count
        
        new_side = "sell" if filled_grid["side"] == "buy" else "buy"
        price_offset = step_price if new_side == "sell" else -step_price
        
        new_price = filled_grid["price"] + price_offset
        new_price = round_step(new_price, prec["tick_size"])
        
        filled_grid["side"] = new_side
        filled_grid["price"] = new_price
        filled_grid["status"] = "pending"
        
        await self.place_grid_orders([filled_grid])

    async def check_exit(self, sym=None):
        # 網格不需要統一的停損停利，由 run() 維護
        pass
