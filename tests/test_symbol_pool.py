import unittest
import multi_coin_bot


class SymbolPoolTests(unittest.TestCase):
    def test_locked_symbol_stays_when_pool_is_replaced(self):
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


if __name__ == "__main__":
    unittest.main()
