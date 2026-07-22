import asyncio
import logging
from typing import Dict
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import uvicorn

# --- 配置區 (您可以直接在這裡調整「膽量」) ---
class TradingConfig:
    MIN_PROFIT_SPACE = 0.4
    MIN_RR_RATIO = 1.1
    MIN_VOL_MULTIPLIER = 0.15
    MIN_ATR_PCT = 0.1
    CALLBACK_RATE = 0.005
    BREAKEVEN_THRESHOLD = 0.005


class TradeState:
    def __init__(self, symbol: str, entry_price: float):
        self.symbol = symbol
        self.entry_price = entry_price
        self.highest_price = entry_price
        self.is_breakeven_set = False
        self.is_active = True

    def update_and_check(self, current_price: float) -> str:
        if not self.is_active:
            return "No Position"

        if current_price > self.highest_price:
            self.highest_price = current_price
            print(f"📈 [{self.symbol}] 創新高: {self.highest_price:.4f}")

        if not self.is_breakeven_set:
            if current_price >= self.entry_price * (1 + TradingConfig.BREAKEVEN_THRESHOLD):
                self.is_breakeven_set = True
                print(f"🛡️  [{self.symbol}] 觸發保本！止損鎖定在成本價: {self.entry_price}")

        if self.is_breakeven_set:
            stop_loss_trigger = self.entry_price
        else:
            stop_loss_trigger = self.highest_price * (1 - TradingConfig.CALLBACK_RATE)

        if current_price <= stop_loss_trigger:
            self.is_active = False
            return (
                f"❌ [{self.symbol}] 觸發停利出場！"
                f"價格: {current_price:.4f} <= 觸發點: {stop_loss_trigger:.4f}"
            )

        return f"✅ [{self.symbol}] 持倉中 (最高點: {self.highest_price:.4f})"


app = FastAPI(title="Dynamic Trailing Stop Bot")
active_trades: Dict[str, TradeState] = {}


async def get_live_price(symbol: str) -> float:
    import random
    base = 100.0
    return base + random.uniform(-1.0, 2.0)


async def monitor_trade_loop(symbol: str):
    print(f"🚀 啟動 {symbol} 監控任務...")
    try:
        while True:
            if symbol in active_trades and active_trades[symbol].is_active:
                current_p = await get_live_price(symbol)
                result = active_trades[symbol].update_and_check(current_p)
                print(result)
                if "出場" in result:
                    break
            await asyncio.sleep(1)
    except Exception as e:
        print(f"⚠️ {symbol} 監控異常: {e}")


class TradeRequest(BaseModel):
    symbol: str
    entry_price: float


@app.post("/start_trade")
async def start_trade(req: TradeRequest, background_tasks: BackgroundTasks):
    active_trades[req.symbol] = TradeState(req.symbol, req.entry_price)
    background_tasks.add_task(monitor_trade_loop, req.symbol)
    return {
        "status": "success",
        "message": f"Started tracking {req.symbol} with dynamic trailing stop."
    }


@app.get("/status/{symbol}")
async def get_status(symbol: str):
    if symbol not in active_trades:
        return {"error": "No active trade"}
    trade = active_trades[symbol]
    return {
        "symbol": symbol,
        "is_active": trade.is_active,
        "entry_price": trade.entry_price,
        "highest_price": trade.highest_price,
        "is_breakeven_set": trade.is_breakeven_set,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
