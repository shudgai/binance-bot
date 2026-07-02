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
