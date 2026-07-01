import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.entry_filter import is_entry_pin_safe, get_entry_strictness_profile


class EntryFilterTests(unittest.TestCase):
    def test_bad_pinbar_rejects_long_entry(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["ohlcv"] = [
            [0, 100, 101, 99, 100, 1000],
            [0, 100, 108, 95, 97, 1000],
        ]
        self.assertFalse(is_entry_pin_safe(sym, "buy"))

    def test_entry_strictness_profile_switches_between_modes(self):
        relaxed = get_entry_strictness_profile("relaxed")
        balanced = get_entry_strictness_profile("balanced")
        strict = get_entry_strictness_profile("strict")

        self.assertLess(relaxed["volume_ratio"], balanced["volume_ratio"])
        self.assertLess(balanced["volume_ratio"], strict["volume_ratio"])
        self.assertGreater(relaxed["pin_threshold"], strict["pin_threshold"])


if __name__ == "__main__":
    unittest.main()
