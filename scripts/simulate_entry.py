import asyncio
import sys
import os
import logging

# 將 repo root 加入 sys.path
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)

from core.ctx import init_states, STATES
from core import ctx
from core.orders import execute_order

logging.basicConfig(level=logging.INFO)


async def main():
    # 初始化狀態（只初始化目標幣種以加速）
    init_states(symbols=["NEARUSDT"]) 
    s = ctx.STATES.get("NEARUSDT")
    # 填入必要的市場快照，避免價格/參考價檢查被拒
    s["last_trade_price"] = 1.86
    s["current_atr"] = 0.01
    s["vol_ma20"] = 1000
    s["current_vol"] = 1200
    s["ohlcv"] = [[0, 0, 0, 0, 0, 0]] * 10

    # 模擬一次紙上交易進場（is_rescue_dca=True 跳過 orderbook 檢查，降低外部依賴）
    await execute_order("NEARUSDT", "sell", price=1.861, allocation_pct=0.05,
                        is_rescue_dca=True, signal_strength=22.0, entry_route="SimTest")


if __name__ == '__main__':
    asyncio.run(main())
