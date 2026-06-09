import unittest
import multi_coin_bot


class TradingSpeedTests(unittest.TestCase):
    def test_entry_timing_is_more_aggressive(self):
        self.assertLessEqual(multi_coin_bot.MAIN_LOOP_INTERVAL_SEC, 6)
        self.assertLessEqual(multi_coin_bot.PENDING_CONFIRM_SEC, 2)
        self.assertLessEqual(multi_coin_bot.COOLDOWN_SEC, 300)


if __name__ == "__main__":
    unittest.main()
