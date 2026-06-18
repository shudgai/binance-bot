import unittest
import multi_coin_bot


class TakeProfitTests(unittest.TestCase):
    def test_trailing_take_profit_updates_target_with_price_rise(self):
        sym = "XRPUSDT"
        s = multi_coin_bot.STATES[sym]
        multi_coin_bot.reset_coin_state(sym)
        s["avg_price"] = 100.0
        s["trail_tp_price"] = 100.3
        s["highest_profit_pct"] = 0.005

        should_exit, new_tp = multi_coin_bot.update_trailing_take_profit(sym, 100.5, True)

        self.assertFalse(should_exit)
        self.assertAlmostEqual(new_tp, 100.5, places=4)

    def test_early_take_profit_triggers_on_small_profit(self):
        sym = "XRPUSDT"
        s = multi_coin_bot.STATES[sym]
        multi_coin_bot.reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 100.8
        s["open_time"] = 0.0
        s["current_atr"] = 0.5
        s["current_rsi"] = 45.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [[0, 100, 100, 99, 100, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.008
        s["pnl_history"] = []

        import asyncio
        async def run_check():
            await multi_coin_bot.check_exits(sym)

        asyncio.run(run_check())

        self.assertEqual(s["qty"], 0.0)


if __name__ == "__main__":
    unittest.main()
