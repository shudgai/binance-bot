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
        avg=100, price=101.2 → peak=1.2% (above the current 1.0% arm threshold).
        ATR=2.0 → the ATR gap alone would place the lock below entry.
        60% floor: avg*(1+0.012*0.6) = 100.72.
        Expect lock_price ≈ 100.72.
        """
        state = self._make_state()
        states = {"TESTUSDT": state}
        with patch.object(exits_mod.ctx, "STATES", states):
            crossed, lock_price = exits_mod.update_ma_peak_lock("TESTUSDT", 101.2, is_long=True)
        self.assertAlmostEqual(lock_price, 100.72, places=4,
            msg=f"Expected lock_price ≈ 100.72 but got {lock_price}")
        self.assertFalse(crossed)

    def test_peak_lock_long_crossed_at_60pct_floor(self):
        """After setting the peak, falling below 60% floor should trigger crossed=True."""
        state = self._make_state()
        states = {"TESTUSDT": state}
        with patch.object(exits_mod.ctx, "STATES", states):
            exits_mod.update_ma_peak_lock("TESTUSDT", 101.2, is_long=True)
            crossed, lock_price = exits_mod.update_ma_peak_lock("TESTUSDT", 100.70, is_long=True)
        self.assertTrue(crossed, f"Expected crossed=True at 100.70 (lock={lock_price})")

    def test_peak_lock_short_large_atr_respects_60pct_floor(self):
        """
        avg=100, price=98.8 → short peak=1.2% (above the current 1.0% arm threshold).
        ATR=2.0 → the ATR gap alone would place the lock above entry.
        60% floor: avg*(1-0.012*0.6) = 99.28.
        Expect lock_price ≈ 99.28.
        """
        state = self._make_state()
        states = {"TESTUSDT": state}
        with patch.object(exits_mod.ctx, "STATES", states):
            crossed, lock_price = exits_mod.update_ma_peak_lock("TESTUSDT", 98.8, is_long=False)
        self.assertAlmostEqual(lock_price, 99.28, places=4,
            msg=f"Expected lock_price ≈ 99.28 but got {lock_price}")
        self.assertFalse(crossed)

    def test_peak_lock_short_crossed_at_60pct_floor(self):
        """After setting the short peak, rising above 60% floor should trigger crossed=True."""
        state = self._make_state()
        states = {"TESTUSDT": state}
        with patch.object(exits_mod.ctx, "STATES", states):
            exits_mod.update_ma_peak_lock("TESTUSDT", 98.8, is_long=False)
            crossed, lock_price = exits_mod.update_ma_peak_lock("TESTUSDT", 99.30, is_long=False)
        self.assertTrue(crossed, f"Expected crossed=True at 99.30 (lock={lock_price})")

if __name__ == '__main__':
    unittest.main()

