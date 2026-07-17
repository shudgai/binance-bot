import unittest
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ctx import STATES, init_states
import core.ctx as ctx
from core.state_manager import reset_coin_state
from core.symbol_profile import apply_symbol_pool_change, save_symbol_pool


class SymbolPoolTests(unittest.TestCase):
    @patch("core.symbol_profile.save_symbol_pool")
    def test_pool_shrinks_to_top_fifteen_instead_of_preserving_old_high_water_mark(self, mock_save):
        old_symbols = [f"OLD{i}USDT" for i in range(23)]
        requested = [f"NEW{i}USDT" for i in range(15)]
        original_symbols = list(ctx.ALL_SYMBOLS)
        original_states = dict(ctx.STATES)
        try:
            ctx.ALL_SYMBOLS[:] = old_symbols
            for sym in old_symbols:
                ctx.STATES[sym] = {
                    "qty": 0.0, "entry_count": 0, "open_time": 0,
                    "status": "ACTIVE", "pending_side": None,
                }
            with patch("core.symbol_profile.filter_valid_symbols",
                       side_effect=lambda _, symbols: symbols):
                updated = apply_symbol_pool_change(requested)

            self.assertEqual(len(updated), 15)
            self.assertEqual(updated, requested[:15])
        finally:
            ctx.ALL_SYMBOLS[:] = original_symbols
            ctx.STATES.clear()
            ctx.STATES.update(original_states)

    @patch("core.symbol_profile.save_symbol_pool")
    def test_pending_order_symbol_is_kept_while_pool_shrinks(self, mock_save):
        original_symbols = list(ctx.ALL_SYMBOLS)
        original_states = dict(ctx.STATES)
        original_pending = dict(ctx.PENDING_LIMIT_ORDERS)
        try:
            ctx.ALL_SYMBOLS[:] = ["XRPUSDT", "DOGEUSDT"]
            for sym in ctx.ALL_SYMBOLS:
                ctx.STATES[sym] = {
                    "qty": 0.0, "entry_count": 0, "open_time": 0,
                    "status": "ACTIVE", "pending_side": None,
                }
            ctx.PENDING_LIMIT_ORDERS.clear()
            ctx.PENDING_LIMIT_ORDERS["order-1"] = {"sym": "XRPUSDT"}
            with patch("core.symbol_profile.filter_valid_symbols",
                       side_effect=lambda _, symbols: symbols):
                updated = apply_symbol_pool_change(["BTCUSDT"])
            self.assertIn("XRPUSDT", updated)
        finally:
            ctx.ALL_SYMBOLS[:] = original_symbols
            ctx.STATES.clear()
            ctx.STATES.update(original_states)
            ctx.PENDING_LIMIT_ORDERS.clear()
            ctx.PENDING_LIMIT_ORDERS.update(original_pending)

    @patch("core.symbol_profile.save_symbol_pool")
    def test_locked_symbol_stays_when_pool_is_replaced(self, mock_save):
        sym = "XRPUSDT"
        init_states([sym, "DOGEUSDT", "ADAUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"])
        reset_coin_state(sym)
        s = STATES[sym]
        s["qty"] = 0.01
        s["avg_price"] = 100.0
        s["open_time"] = 1.0

        original = ["XRPUSDT", "DOGEUSDT", "ADAUSDT"]
        ctx.ALL_SYMBOLS = list(original)

        updated = apply_symbol_pool_change(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

        self.assertIn("XRPUSDT", updated)
        self.assertNotIn("DOGEUSDT", updated)
        self.assertIn("BTCUSDT", updated)
        self.assertEqual(len(updated), 3)
        mock_save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
