import asyncio
import unittest
from unittest.mock import patch, AsyncMock
import multi_coin_bot


class EntryRiskTests(unittest.TestCase):
    def test_additional_entry_updates_average_price_safely(self):
        sym = "XRPUSDT"
        s = multi_coin_bot.STATES[sym]
        multi_coin_bot.reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["entry_count"] = 1
        s["last_entry_time"] = 0.0
        s["close_price"] = 110.0

        mock_ticker = AsyncMock(return_value={"last": 110.0})
        with patch("multi_coin_bot.compute_per_coin_margin", return_value=3000.0), \
             patch("multi_coin_bot.get_balance", return_value=10000.0), \
             patch.object(multi_coin_bot.exchange_futures, "fetch_ticker", mock_ticker):
            asyncio.run(multi_coin_bot.execute_order(sym, "buy", 110.0))

        self.assertGreater(s["entry_count"], 1)
        self.assertAlmostEqual(s["avg_price"], 108.18, places=2)

    def test_losing_position_skips_additional_entry(self):
        sym = "XRPUSDT"
        s = multi_coin_bot.STATES[sym]
        multi_coin_bot.reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 95.0
        s["entry_count"] = 1
        s["last_entry_time"] = 0.0

        mock_ticker = AsyncMock(return_value={"last": 95.0})
        with patch("multi_coin_bot.compute_per_coin_margin", return_value=3000.0), \
             patch("multi_coin_bot.get_balance", return_value=10000.0), \
             patch.object(multi_coin_bot.exchange_futures, "fetch_ticker", mock_ticker):
            asyncio.run(multi_coin_bot.execute_order(sym, "buy", 95.0))

        self.assertEqual(s["entry_count"], 1)


if __name__ == "__main__":
    unittest.main()
