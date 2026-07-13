import asyncio
import unittest
import sys
import os
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state, repair_invalid_states, get_open_position_count
from core import ctx
from core import exchange_client
from core.orders import execute_order, _enforce_bracket_rr, _pending_entry_setup_valid


class EntryRiskTests(unittest.TestCase):
    def test_float_symbol_state_is_repaired_before_position_count(self):
        sym = "BROKENUSDT"
        original_symbols = list(ctx.ALL_SYMBOLS)
        original_state = ctx.STATES.get(sym)
        try:
            if sym not in ctx.ALL_SYMBOLS:
                ctx.ALL_SYMBOLS.append(sym)
            ctx.STATES[sym] = 12.34

            self.assertEqual(repair_invalid_states(), [sym])
            self.assertIsInstance(ctx.STATES[sym], dict)
            self.assertEqual(ctx.STATES[sym]["qty"], 0.0)
            self.assertIsInstance(get_open_position_count(), int)
        finally:
            ctx.ALL_SYMBOLS[:] = original_symbols
            if original_state is None:
                ctx.STATES.pop(sym, None)
            else:
                ctx.STATES[sym] = original_state

    def test_pending_first_entry_revalidates_latest_signal(self):
        info = {
            "sym": "AAVEUSDT", "side": "sell", "entry_route": "b",
            "signal_strength": 30.6, "signal_price": 96.06,
            "is_rescue_dca": False,
        }
        revalidate = unittest.mock.Mock(return_value=(False, "bullish divergence"))
        self.assertEqual(
            _pending_entry_setup_valid(info, validator=revalidate),
            (False, "bullish divergence"),
        )
        revalidate.assert_called_once_with("AAVEUSDT", "sell", "b", 30.6, 96.06)

    def test_pending_rescue_order_keeps_separate_risk_path(self):
        self.assertEqual(
            _pending_entry_setup_valid({"is_rescue_dca": True}),
            (True, "rescue_dca"),
        )

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

        self.assertEqual(s["entry_count"], 1)
        self.assertEqual(s["avg_price"], 100.0)

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


    def test_long_bracket_enforces_minimum_reward_over_risk(self):
        stop, take_profit = _enforce_bracket_rr(100.0, 97.0, 102.0, True, 0.1, min_rr=1.5)
        self.assertEqual(stop, 97.0)
        self.assertGreaterEqual(take_profit - 100.0, (100.0 - stop) * 1.5)

    def test_short_bracket_enforces_minimum_reward_over_risk(self):
        stop, take_profit = _enforce_bracket_rr(100.0, 103.0, 98.0, False, 0.1, min_rr=1.5)
        self.assertEqual(stop, 103.0)
        self.assertGreaterEqual(100.0 - take_profit, (stop - 100.0) * 1.5)


if __name__ == "__main__":
    unittest.main()
