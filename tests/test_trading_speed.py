import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import (
    MAIN_LOOP_INTERVAL_SEC,
    PENDING_CONFIRM_SEC,
    COOLDOWN_SEC,
    ENTRY_PULLBACK_ATR_MULT,
    ENTRY_CHASE_OFFSET_PCT,
    ENTRY_ORDER_MODE_AUTO_STRONG,
    ENTRY_ORDER_MODE_AUTO_MARKET,
)


class TradingSpeedTests(unittest.TestCase):
    def test_entry_timing_config(self):
        self.assertGreater(MAIN_LOOP_INTERVAL_SEC, 0)
        self.assertLessEqual(PENDING_CONFIRM_SEC, 2)
        self.assertGreater(COOLDOWN_SEC, 0)

    def test_entry_price_tuning_config(self):
        self.assertGreater(ENTRY_PULLBACK_ATR_MULT, 0.15)
        self.assertLess(ENTRY_CHASE_OFFSET_PCT, 0.0005)
        self.assertGreater(ENTRY_ORDER_MODE_AUTO_STRONG, 18.0)
        self.assertGreater(ENTRY_ORDER_MODE_AUTO_MARKET, 28.0)


if __name__ == "__main__":
    unittest.main()
