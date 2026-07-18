import unittest
from unittest.mock import patch
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.exits as exits_mod

class TestExitsPeakLock(unittest.TestCase):
    def _make_state(self):
        return {
            "avg_price": 100.0,
            "current_atr": 2.0,  # large ATR: 2.0 per unit
            "highest_profit_pct": 0.0,
            "ma_peak_saved_pct": 0.0,
            "ma_peak_lock_price": 0.0,
            "ma_peak_lock_armed": False,
            "realtime_peak_candidate_profit": 0.0,
            "realtime_peak_candidate_price": 0.0,
            "realtime_peak_candidate_time": 0.0,
        }

    def test_peak_lock_long_large_atr_respects_60pct_floor(self):
        """
        avg=100, price=100.8 → peak=0.8%.
        ATR=2.0 → atr*0.5 gap = 1.0 → without floor proposed = 100.8-1.0 = 99.8 (below entry).
        60% floor: avg*(1+0.008*0.6) = 100.48.
        Fee floor: avg*(1+fee_floor) ≈ 100.25 (below 100.48).
        Expect lock_price ≈ 100.48.
        """
        state = self._make_state()
        states = {"TESTUSDT": state}
        with patch.object(exits_mod.ctx, "STATES", states):
            crossed, lock_price = exits_mod.update_ma_peak_lock("TESTUSDT", 100.8, is_long=True)
        self.assertAlmostEqual(lock_price, 100.48, places=4,
            msg=f"Expected lock_price ≈ 100.48 but got {lock_price}")
        self.assertFalse(crossed)

    def test_peak_lock_long_crossed_at_60pct_floor(self):
        """After setting the peak, falling below 60% floor should trigger crossed=True."""
        state = self._make_state()
        states = {"TESTUSDT": state}
        with patch.object(exits_mod.ctx, "STATES", states):
            exits_mod.update_ma_peak_lock("TESTUSDT", 100.8, is_long=True)
            crossed, lock_price = exits_mod.update_ma_peak_lock("TESTUSDT", 100.40, is_long=True)
        self.assertTrue(crossed, f"Expected crossed=True at 100.40 (lock={lock_price})")

    def test_peak_lock_short_large_atr_respects_60pct_floor(self):
        """
        avg=100, price=99.2 → short peak=0.8%.
        ATR=2.0 → peak_price+atr*0.5 = 99.2+1.0 = 100.2 (above entry).
        60% floor: avg*(1-0.008*0.6) = 99.52.
        Expect lock_price ≈ 99.52.
        """
        state = self._make_state()
        states = {"TESTUSDT": state}
        with patch.object(exits_mod.ctx, "STATES", states):
            crossed, lock_price = exits_mod.update_ma_peak_lock("TESTUSDT", 99.2, is_long=False)
        self.assertAlmostEqual(lock_price, 99.52, places=4,
            msg=f"Expected lock_price ≈ 99.52 but got {lock_price}")
        self.assertFalse(crossed)

    def test_peak_lock_short_crossed_at_60pct_floor(self):
        """After setting the short peak, rising above 60% floor should trigger crossed=True."""
        state = self._make_state()
        states = {"TESTUSDT": state}
        with patch.object(exits_mod.ctx, "STATES", states):
            exits_mod.update_ma_peak_lock("TESTUSDT", 99.2, is_long=False)
            crossed, lock_price = exits_mod.update_ma_peak_lock("TESTUSDT", 99.60, is_long=False)
        self.assertTrue(crossed, f"Expected crossed=True at 99.60 (lock={lock_price})")

if __name__ == '__main__':
    unittest.main()

