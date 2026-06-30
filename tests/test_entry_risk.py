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
        s["macd_line"] = 2.0
        s["macd_signal"] = 1.0
        s["prev_macd_line"] = 1.0
        s["prev_macd_signal"] = 0.8
        s["ohlcv"] = [
            [0, 100, 105, 95, 100, 1000],
            [0, 100, 105, 95, 105, 1000],
            [0, 105, 110, 100, 110, 1000]
        ]

        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker.return_value = {"last": 110.0}
        mock_exchange.fetch_order_book.return_value = {"bids": [[110.0, 1000.0]], "asks": [[110.1, 100.0]]}
        mock_exchange.fetch_balance.return_value = {"USDT": {"total": 10000.0, "free": 10000.0}}
        mock_exchange.fetch_order.return_value = {"status": "closed", "filled": 4.5, "average": 110.0, "price": 110.0}
        mock_exchange.fetch_positions.return_value = []
        mock_exchange.create_order.return_value = {"id": "12345"}
        
        with patch("core.orders.compute_per_coin_margin", return_value=3000.0), \
             patch("core.orders.get_balance", return_value=10000.0), \
             patch("core.entry_filter.is_entry_allowed", return_value=True), \
             patch("core.entry_filter.is_entry_volume_confirmed", return_value=True), \
             patch("core.orders.sanitize_order_qty", side_effect=lambda sym, q: 4.5), \
             patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_futures", mock_exchange):
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

        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker.return_value = {"last": 95.0}
        mock_exchange.fetch_order_book.return_value = {"bids": [[95.0, 1000.0]], "asks": [[95.1, 100.0]]}
        with patch("core.orders.compute_per_coin_margin", return_value=3000.0), \
             patch("core.orders.get_balance", return_value=10000.0), \
             patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_futures", mock_exchange):
            asyncio.run(execute_order(sym, "buy", 95.0))

        self.assertEqual(s["entry_count"], 1)


if __name__ == "__main__":
    unittest.main()
