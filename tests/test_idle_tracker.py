import unittest
import os
import time
import json
from core.idle_tracker import StrategyIdleTracker, IDLE_STATE_FILE


class TestStrategyIdleTracker(unittest.TestCase):
    def setUp(self):
        # Ensure state file is clean before each test
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

    # ------------------------------------------------------------------
    # 原有測試
    # ------------------------------------------------------------------

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

        # Mark one active again — resets timer, clears block → no longer idle
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

    # ------------------------------------------------------------------
    # 新增測試：get_removable_symbols
    # ------------------------------------------------------------------

    def test_get_removable_symbols_no_position(self):
        """無持倉閒置幣種應進入 safe_to_remove，不應出現在 hold_for_exit 或孤兒清單。"""
        symbol = "NOPUSDT"
        self.tracker.mark_blocked(symbol, "MA_Strategy", "No signal")
        self.tracker.mark_blocked(symbol, "Range_Strategy", "ADX too high")
        time.sleep(1.1)
        self.assertTrue(self.tracker.is_idle(symbol, 2))

        safe_to_remove, hold_for_exit = self.tracker.get_removable_symbols(
            [symbol],
            position_checker=lambda s: False,  # 無持倉
        )

        self.assertIn(symbol, safe_to_remove)
        self.assertNotIn(symbol, hold_for_exit)
        self.assertNotIn(symbol, self.tracker.get_orphaned_positions())

    def test_get_removable_symbols_with_position(self):
        """有持倉閒置幣種應進入 hold_for_exit，並被加入孤兒清單。"""
        symbol = "POSUSDT"
        self.tracker.mark_blocked(symbol, "MA_Strategy", "RSI high")
        self.tracker.mark_blocked(symbol, "Range_Strategy", "ADX too high")
        time.sleep(1.1)
        self.assertTrue(self.tracker.is_idle(symbol, 2))

        safe_to_remove, hold_for_exit = self.tracker.get_removable_symbols(
            [symbol],
            position_checker=lambda s: True,  # 有持倉
        )

        self.assertNotIn(symbol, safe_to_remove)
        self.assertIn(symbol, hold_for_exit)
        self.assertIn(symbol, self.tracker.get_orphaned_positions())

    def test_confirm_position_closed(self):
        """平倉確認後，孤兒清單應正確清空，idle 紀錄也一併清除。"""
        symbol = "CLOSEDUSDT"
        self.tracker.mark_blocked(symbol, "MA_Strategy", "Flat chop")
        self.tracker.mark_blocked(symbol, "Range_Strategy", "ADX too high")
        time.sleep(1.1)

        # 將其歸入孤兒清單
        self.tracker.get_removable_symbols(
            [symbol],
            position_checker=lambda s: True,
        )
        self.assertIn(symbol, self.tracker.get_orphaned_positions())

        # 平倉確認
        self.tracker.confirm_position_closed(symbol)

        self.assertNotIn(symbol, self.tracker.get_orphaned_positions())
        # reset 也清掉 block_reasons 與 last_active_ts
        self.assertNotIn(symbol, self.tracker._block_reasons)
        self.assertNotIn(symbol, self.tracker._last_active_ts)

    def test_orphaned_positions_persistence(self):
        """孤兒清單應在重啟後能從 JSON 正確恢復。"""
        symbol = "PERSISTORPHANUSDT"
        self.tracker.mark_blocked(symbol, "MA_Strategy", "No signal")
        self.tracker.mark_blocked(symbol, "Range_Strategy", "ADX too high")
        time.sleep(1.1)

        self.tracker.get_removable_symbols(
            [symbol],
            position_checker=lambda s: True,
        )
        self.assertIn(symbol, self.tracker.get_orphaned_positions())

        # 驗證 JSON 已寫入孤兒清單
        with open(IDLE_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn(symbol, data.get("orphaned_positions", []))

        # 重新初始化，確認孤兒清單被恢復
        new_tracker = StrategyIdleTracker(idle_threshold_sec=1)
        self.assertIn(symbol, new_tracker.get_orphaned_positions())


class TestOrphanGateMembership(unittest.TestCase):
    """
    驗證孤兒閘門的判斷邏輯：
    - 孤兒幣在 get_orphaned_positions() 中 → 閘門應攔截（模擬 check_entries 中的 `in` 判斷）
    - confirm_position_closed 後 → 閘門不再攔截
    """

    def setUp(self):
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

    def test_orphan_gate_blocks_entry(self):
        """孤兒幣應被閘門攔截（symbol in get_orphaned_positions() == True）。"""
        symbol = "GATEUSDT"
        self.tracker.mark_blocked(symbol, "MA_Strategy", "RSI stuck")
        self.tracker.mark_blocked(symbol, "Range_Strategy", "ADX too high")
        time.sleep(1.1)

        self.tracker.get_removable_symbols(
            [symbol],
            position_checker=lambda s: True,  # 有持倉 → 孤兒
        )

        # 模擬 check_entries 中的閘門判斷
        orphans = self.tracker.get_orphaned_positions()
        self.assertIn(symbol, orphans,
                      "閘門應攔截：孤兒幣必須出現在 get_orphaned_positions() 中")

    def test_orphan_gate_allows_after_close(self):
        """平倉後 confirm_position_closed → 幣種應從孤兒清單移除，閘門不再攔截。"""
        symbol = "GATEUSDT"
        self.tracker.mark_blocked(symbol, "MA_Strategy", "RSI stuck")
        self.tracker.mark_blocked(symbol, "Range_Strategy", "ADX too high")
        time.sleep(1.1)

        self.tracker.get_removable_symbols(
            [symbol],
            position_checker=lambda s: True,
        )
        self.assertIn(symbol, self.tracker.get_orphaned_positions())

        # 平倉確認
        self.tracker.confirm_position_closed(symbol)

        # 閘門應放行
        self.assertNotIn(symbol, self.tracker.get_orphaned_positions(),
                         "平倉後閘門應放行：孤兒清單不應再包含此幣種")

    def test_no_position_symbol_never_enters_orphan(self):
        """無持倉的閒置幣不應進入孤兒清單，閘門對其不應攔截。"""
        symbol = "NOPGATEUSDT"
        self.tracker.mark_blocked(symbol, "MA_Strategy", "No volume")
        self.tracker.mark_blocked(symbol, "Range_Strategy", "No zones")
        time.sleep(1.1)

        self.tracker.get_removable_symbols(
            [symbol],
            position_checker=lambda s: False,  # 無持倉 → safe_to_remove
        )

        # 無持倉的閒置幣不應進入孤兒清單
        self.assertNotIn(symbol, self.tracker.get_orphaned_positions(),
                         "無持倉幣種不應出現在孤兒清單，閘門不應攔截")


if __name__ == "__main__":
    unittest.main()
