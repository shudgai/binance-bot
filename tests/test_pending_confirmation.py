import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.check_entries import is_pending_confirmation_valid, is_entry_candidate_still_valid
from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from unittest.mock import patch


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


    def test_delayed_buy_is_rejected_after_half_atr_adverse_move(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["close_price"] = 99.0
        s["current_atr"] = 1.0

        ok, reason = is_entry_candidate_still_valid(sym, "buy", "a", 18.0, 100.0)

        self.assertFalse(ok)
        self.assertIn("price moved adverse", reason)

    def test_delayed_entry_is_rejected_by_new_opposite_divergence(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["close_price"] = 100.0
        s["current_atr"] = 1.0
        s["divergence"] = "bearish"

        ok, reason = is_entry_candidate_still_valid(sym, "buy", "a", 18.0, 100.0)

        self.assertFalse(ok)
        self.assertEqual(reason, "bearish divergence")

    def test_delayed_entry_requires_latest_signal_and_filters(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["close_price"] = 100.0
        s["current_atr"] = 1.0
        s["divergence"] = "none"

        with patch("core.check_entries.compute_signal_strength", return_value=("buy", 19.0, "a")), \
             patch("core.check_entries.is_entry_allowed", return_value=True):
            ok, reason = is_entry_candidate_still_valid(sym, "buy", "a", 18.0, 100.0)

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")


if __name__ == "__main__":
    unittest.main()
