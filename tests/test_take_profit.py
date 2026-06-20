import unittest
import asyncio
import multi_coin_bot


class TakeProfitTests(unittest.TestCase):
    def test_trailing_take_profit_updates_target_with_price_rise(self):
        # 測試 Layer 2 極限追蹤 (Extreme Trailing) 的觸發平倉
        sym = "XRPUSDT"
        s = multi_coin_bot.STATES[sym]
        multi_coin_bot.reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 101.0
        s["open_time"] = 1.0
        s["current_atr"] = 0.5
        s["highest_profit_pct"] = 0.04
        s["trailing_highest"] = 104.0
        s["trade_status"] = "TRAILING"
        s["ohlcv"] = [[0, 100, 100, 99, 100, 1000]]

        async def run_check():
            await multi_coin_bot.check_exits(sym)

        asyncio.run(run_check())

        # 獲利從 4% 回撤到 1% (小於追蹤線 104 * 0.997 = 103.688)，應該平倉
        self.assertEqual(s["qty"], 0.0)

    def test_early_take_profit_triggers_on_small_profit(self):
        # 測試 Layer 0 硬損 (Hard Stop Loss) 的觸發平倉
        sym = "XRPUSDT"
        s = multi_coin_bot.STATES[sym]
        multi_coin_bot.reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 98.0  # 價格跌破硬損 (約 99.25)
        s["open_time"] = 1.0
        s["current_atr"] = 0.5
        s["ohlcv"] = [[0, 100, 100, 99, 100, 1000]]

        async def run_check():
            await multi_coin_bot.check_exits(sym)

        asyncio.run(run_check())

        # 跌破硬停損線，應該觸發平倉
        self.assertEqual(s["qty"], 0.0)


if __name__ == "__main__":
    unittest.main()
