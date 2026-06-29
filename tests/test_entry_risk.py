import asyncio
import unittest
import sys
import os
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core import exchange_client
from core.orders import execute_order


class EntryRiskTests(unittest.TestCase):
    def test_additional_entry_updates_average_price_safely(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["last_entry_price"] = 100.0
        s["entry_count"] = 1
        s["last_entry_time"] = 0.0
        s["close_price"] = 110.0
        s["current_atr"] = 1.0
        s["current_vol"] = 1000.0
        s["vol_ma20"] = 100.0
        s["ohlcv"] = [
            [0, 100, 105, 95, 100, 1000],
            [0, 100, 105, 95, 105, 1000],
            [0, 105, 110, 100, 110, 1000]
        ]

        mock_ticker = AsyncMock(return_value={"last": 110.0})
        mock_orderbook = AsyncMock(return_value={"bids": [[110.0, 1000.0]], "asks": [[110.1, 100.0]]})
        with patch("core.orders.compute_per_coin_margin", return_value=3000.0), \
             patch("core.orders.get_balance", return_value=10000.0), \
             patch("core.entry_filter.is_entry_allowed", return_value=True), \
             patch.object(exchange_client.exchange_futures, "fetch_ticker", mock_ticker), \
             patch.object(exchange_client.exchange_futures, "fetch_order_book", mock_orderbook):
            asyncio.run(execute_order(sym, "buy", 110.0))

        self.assertGreater(s["entry_count"], 1)
        self.assertAlmostEqual(s["avg_price"], 108.18, places=2)

    def test_losing_position_skips_additional_entry(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 95.0
        s["entry_count"] = 1
        s["last_entry_time"] = 0.0

        mock_ticker = AsyncMock(return_value={"last": 95.0})
        with patch("core.orders.compute_per_coin_margin", return_value=3000.0), \
             patch("core.orders.get_balance", return_value=10000.0), \
             patch.object(exchange_client.exchange_futures, "fetch_ticker", mock_ticker):
            asyncio.run(execute_order(sym, "buy", 95.0))

        self.assertEqual(s["entry_count"], 1)


if __name__ == "__main__":
    unittest.main()
