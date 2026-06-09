import unittest
import multi_coin_bot


class EntryFilterTests(unittest.TestCase):
    def test_bad_pinbar_rejects_long_entry(self):
        sym = "XRPUSDT"
        s = multi_coin_bot.STATES[sym]
        multi_coin_bot.reset_coin_state(sym)
        s["ohlcv"] = [
            [0, 100, 101, 99, 100, 1000],
            [0, 100, 105, 95, 97, 1000],
        ]
        self.assertFalse(multi_coin_bot.is_entry_pin_safe(sym, "buy"))


if __name__ == "__main__":
    unittest.main()
