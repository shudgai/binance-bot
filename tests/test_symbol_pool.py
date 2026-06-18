import unittest
from unittest.mock import patch
import multi_coin_bot


class SymbolPoolTests(unittest.TestCase):
    @patch("multi_coin_bot.save_symbol_pool")
    def test_locked_symbol_stays_when_pool_is_replaced(self, mock_save):
        sym = "XRPUSDT"
        multi_coin_bot.reset_coin_state(sym)
        s = multi_coin_bot.STATES[sym]
        s["qty"] = 0.01
        s["avg_price"] = 100.0
        s["open_time"] = 1.0

        original = ["XRPUSDT", "DOGEUSDT", "ADAUSDT"]
        multi_coin_bot.ALL_SYMBOLS = list(original)

        updated = multi_coin_bot.apply_symbol_pool_change(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

        self.assertIn("XRPUSDT", updated)
        self.assertNotIn("DOGEUSDT", updated)
        self.assertIn("BTCUSDT", updated)
        self.assertEqual(len(updated), 3)
        mock_save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
