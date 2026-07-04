import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import MAIN_LOOP_INTERVAL_SEC, PENDING_CONFIRM_SEC, COOLDOWN_SEC


class TradingSpeedTests(unittest.TestCase):
    def test_entry_timing_config(self):
        self.assertGreater(MAIN_LOOP_INTERVAL_SEC, 0)
        self.assertLessEqual(PENDING_CONFIRM_SEC, 2)
        self.assertGreater(COOLDOWN_SEC, 0)


if __name__ == "__main__":
    unittest.main()
