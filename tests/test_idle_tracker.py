import unittest
import os
import time
import json
from core.idle_tracker import StrategyIdleTracker, IDLE_STATE_FILE

class TestStrategyIdleTracker(unittest.TestCase):
    def setUp(self):
        # Ensure state file is clean
        if os.path.exists(IDLE_STATE_FILE):
            try:
                os.remove(IDLE_STATE_FILE)
            except Exception:
                pass
        self.tracker = StrategyIdleTracker(idle_threshold_sec=1)

    def tearDown(self):
        if os.path.exists(IDLE_STATE_FILE):
            try:
                os.remove(IDLE_STATE_FILE)
            except Exception:
                pass

    def test_basic_idle_tracking(self):
        symbol = "TESTUSDT"
        
        # Initially not idle
        self.assertFalse(self.tracker.is_idle(symbol, 2))
        
        # Mark one blocked
        self.tracker.mark_blocked(symbol, "MA_Strategy", "No volume")
        self.assertFalse(self.tracker.is_idle(symbol, 2))
        
        # Mark second blocked
        self.tracker.mark_blocked(symbol, "Range_Strategy", "ADX high")
        # Should still not be idle because threshold is 1 second and we haven't slept
        self.assertFalse(self.tracker.is_idle(symbol, 2))
        
        # Wait 1.1s for threshold
        time.sleep(1.1)
        self.assertTrue(self.tracker.is_idle(symbol, 2))
        
        # Mark one active again
        self.tracker.mark_active(symbol, "MA_Strategy")
        self.assertFalse(self.tracker.is_idle(symbol, 2))

    def test_state_persistence(self):
        symbol = "PERSISTUSDT"
        self.tracker.mark_blocked(symbol, "MA_Strategy", "Blocked reason")
        self.tracker.mark_blocked(symbol, "Range_Strategy", "Blocked range")
        
        # Re-initialize tracker to check if state persists
        new_tracker = StrategyIdleTracker(idle_threshold_sec=1)
        self.assertIn("MA_Strategy", new_tracker._block_reasons.get(symbol, {}))
        self.assertEqual(new_tracker._block_reasons[symbol]["MA_Strategy"], "Blocked reason")

if __name__ == '__main__':
    unittest.main()
