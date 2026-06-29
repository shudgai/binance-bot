import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.entry_filter import is_entry_pin_safe


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


if __name__ == "__main__":
    unittest.main()
