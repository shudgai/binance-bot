import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.check_entries import is_pending_confirmation_valid


class PendingConfirmationTests(unittest.TestCase):
    def test_allows_bullish_candle_with_modest_upper_shadow(self):
        candle = [0, 100, 103, 98, 102, 1000]
        self.assertTrue(is_pending_confirmation_valid("buy", candle))

    def test_rejects_bearish_candle_without_clear_body(self):
        candle = [0, 100, 102, 99, 99.5, 1000]
        self.assertFalse(is_pending_confirmation_valid("buy", candle))

    def test_allows_bullish_candle_with_wider_upper_shadow(self):
        candle = [0, 100, 107, 95, 103, 1000]
        self.assertTrue(is_pending_confirmation_valid("buy", candle))


if __name__ == "__main__":
    unittest.main()
