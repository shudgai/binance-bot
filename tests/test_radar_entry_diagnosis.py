import asyncio
import unittest
from unittest.mock import patch

from core import ctx
from core.state_manager import build_symbol_state
from core.check_entries import (
    check_entries,
    _radar_entry_block_reason,
    _radar_signal_block_message,
)


class RadarEntryDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.sym = "RADARTESTUSDT"
        self.original_symbols = list(ctx.ALL_SYMBOLS)
        self.original_state = ctx.STATES.get(self.sym)
        ctx.ALL_SYMBOLS[:] = [self.sym]
        ctx.STATES[self.sym] = build_symbol_state(self.sym)
        ctx.STATES[self.sym].update({
            "status": "ACTIVE",
            "qty": 0.0,
            "is_ordering": False,
            "ohlcv": [],
        })

    def tearDown(self):
        ctx.ALL_SYMBOLS[:] = self.original_symbols
        if self.original_state is None:
            ctx.STATES.pop(self.sym, None)
        else:
            ctx.STATES[self.sym] = self.original_state

    def _reaches_guard_after_radar(self, route):
        with patch("core.check_entries.is_daily_loss_halted", return_value=False), \
             patch("core.check_entries.get_open_position_count", return_value=0), \
             patch("core.balance.get_dynamic_max_slots", return_value=3), \
             patch("core.check_entries._load_disabled_symbols", return_value=set()), \
             patch("core.check_entries._is_confirmable_exit_cooldown", return_value=False), \
             patch("core.check_entries.compute_signal_strength",
                   return_value=("buy", 25.0, route)), \
             patch("core.check_entries.btc_macro_entry_guard",
                   return_value=(False, "測試終止", "MIXED")) as macro_guard, \
             patch("core.check_entries.set_entry_diagnosis"), \
             patch("core.symbol_profile.SYMBOL_PROFILES", {
                 self.sym: {
                     "_trade_eligible": False,
                     "_trade_eligibility_reason": "雷達觀察中",
                 },
             }):
            asyncio.run(check_entries())
        return macro_guard.called

    def test_ma7_simple_bypasses_radar_block_and_continues(self):
        self.assertTrue(self._reaches_guard_after_radar("MA7_Simple"))

    def test_ma_cross_is_still_blocked_by_radar(self):
        self.assertFalse(self._reaches_guard_after_radar("MA_Cross"))

    def test_missing_profile_fails_closed(self):
        self.assertEqual(_radar_entry_block_reason({}), "尚無雷達交易資格")

    def test_observation_reason_is_preserved(self):
        profile = {
            "_trade_eligible": False,
            "_trade_eligibility_reason": "雷達確認 1/2，觀察中",
        }
        self.assertEqual(
            _radar_entry_block_reason(profile),
            "雷達確認 1/2，觀察中",
        )

    def test_eligible_profile_has_no_block(self):
        self.assertEqual(_radar_entry_block_reason({"_trade_eligible": True}), "")

    def test_signal_message_never_claims_order_is_ready(self):
        message = _radar_signal_block_message(
            "1000XECUSDT", "MA7_Simple", "雷達觀察中",
        )
        self.assertEqual(
            message,
            "1000XECUSDT: 偵測到 MA7_Simple 訊號，但雷達觀察中，不送單",
        )
        self.assertNotIn("準備送單", message)


if __name__ == "__main__":
    unittest.main()
