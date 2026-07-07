import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.check_entries import (
    is_divergence_blocking,
    is_second_bar_adverse,
    is_entry_price_direction_aligned,
    is_pending_confirmation_valid,
    should_wait_for_entry_confirmation,
)


class PendingConfirmationTests(unittest.TestCase):
    def test_paper_relaxed_strong_signal_skips_pending_confirmation(self):
        self.assertFalse(should_wait_for_entry_confirmation(True, True, 12.0))
        self.assertTrue(should_wait_for_entry_confirmation(True, True, 11.9))
        self.assertTrue(should_wait_for_entry_confirmation(False, True, 30.0))

    def test_entry_price_direction_requires_alignment(self):
        self.assertTrue(is_entry_price_direction_aligned("buy", 0.01))
        self.assertTrue(is_entry_price_direction_aligned("buy", 0.0))
        self.assertFalse(is_entry_price_direction_aligned("buy", -0.01))
        self.assertTrue(is_entry_price_direction_aligned("sell", -0.01))
        self.assertTrue(is_entry_price_direction_aligned("sell", 0.0))
        self.assertFalse(is_entry_price_direction_aligned("sell", 0.01))

    def test_relaxed_strong_signal_can_override_divergence(self):
        self.assertFalse(is_divergence_blocking("buy", "bearish", 20.0, True))
        self.assertTrue(is_divergence_blocking("buy", "bearish", 19.9, True))
        self.assertTrue(is_divergence_blocking("sell", "bullish", 25.0, False))

    def test_allows_bullish_candle_with_modest_upper_shadow(self):
        candle = [0, 100, 103, 98, 102, 1000]
        self.assertTrue(is_pending_confirmation_valid("buy", candle))

    def test_rejects_bearish_candle_without_clear_body(self):
        candle = [0, 100, 102, 99, 99.5, 1000]
        self.assertFalse(is_pending_confirmation_valid("buy", candle))

    def test_allows_bullish_candle_with_wider_upper_shadow(self):
        candle = [0, 100, 107, 95, 103, 1000]
        self.assertTrue(is_pending_confirmation_valid("buy", candle))

    def test_second_bar_adverse_rejects_failed_buy_confirmation(self):
        self.assertTrue(is_second_bar_adverse("buy", 99.4, 100.0, atr=0.2))
        self.assertFalse(is_second_bar_adverse("buy", 99.8, 100.0, atr=0.2))

    def test_second_bar_adverse_rejects_failed_sell_confirmation(self):
        self.assertTrue(is_second_bar_adverse("sell", 100.6, 100.0, atr=0.2))
        self.assertFalse(is_second_bar_adverse("sell", 100.2, 100.0, atr=0.2))


if __name__ == "__main__":
    unittest.main()
