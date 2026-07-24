import unittest
import time
import asyncio
from core import ctx
from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state, mark_exit
from unittest.mock import patch, Mock

class StopLossCooldownZoneTests(unittest.TestCase):
    def setUp(self):
        self.sym = "SOLUSDT"
        init_states([self.sym])
        reset_coin_state(self.sym)

    def test_stop_loss_cooldown_duration(self):
        # Reset stop count to prevent BANNED state
        STATES[self.sym]["stop_count"] = 0
        STATES[self.sym]["first_stop_time"] = 0.0

        # 1. Test stop loss cooldown is 1800s (30 minutes)
        mark_exit(self.sym, is_stop_loss=True, reason="Stop Loss")
        s = STATES[self.sym]
        self.assertEqual(s["status"], "COOLDOWN")
        expected_remaining = s["next_status_time"] - time.time()
        self.assertTrue(1700 < expected_remaining <= 1800)

        # 2. Test general exit cooldown is COOLDOWN_SEC (300s)
        reset_coin_state(self.sym)
        STATES[self.sym]["stop_count"] = 0
        STATES[self.sym]["first_stop_time"] = 0.0
        
        mark_exit(self.sym, is_stop_loss=False, reason="Take Profit")
        s = STATES[self.sym]
        self.assertEqual(s["status"], "COOLDOWN")
        expected_remaining = s["next_status_time"] - time.time()
        self.assertTrue(250 < expected_remaining <= 300)

    @patch("core.ctx.ALL_SYMBOLS", ["SOLUSDT"])
    def test_stop_loss_same_zone_lock(self):
        from core.check_entries import check_entries
        s = STATES[self.sym]
        s.update({
            "status": "ACTIVE",
            "close_price": 100.0,
            "last_entry_price": 100.2,
            "last_entry_direction": "buy",
            "last_exit_reason": "[Stop_Loss] Hit",
            "qty": 0.0,
            "ohlcv": [
                [0, 100.0, 102.0, 99.0, 101.0, 1000.0],
                [1, 100.0, 102.0, 99.0, 101.0, 1000.0],
                [2, 100.0, 102.0, 99.0, 101.0, 1000.0],
            ],
            "vol_ma20": 1000.0,
            "current_atr": 1.0,
            "atr_history": [1.0, 1.0, 1.0],
        })

        # Mock dependencies to let candidate evaluation run through check_entries()
        with patch("core.check_entries._load_disabled_symbols", return_value=set()), \
             patch("core.check_entries.is_daily_loss_halted", return_value=False), \
             patch("core.check_entries.get_open_position_count", return_value=0), \
             patch("core.balance.get_dynamic_max_slots", return_value=5), \
             patch("core.check_entries._is_confirmable_exit_cooldown", return_value=False), \
             patch("core.check_entries.compute_signal_strength", return_value=("buy", 15.0, "MA_Cross")), \
             patch("core.check_entries.is_entry_candidate_still_valid", return_value=(True, "")), \
             patch("core.check_entries._ma_candidate_quality", return_value=(True, "", 10.0)), \
             patch("core.check_entries.is_entry_allowed", return_value=True), \
             patch("core.check_entries._calc_sl_tp", return_value=(1.0, 1.5, 3.0, 2.0)), \
             patch("core.orders.execute_order") as mock_execute, \
             patch("core.check_entries.logger.info") as mock_logger_info:
            
            asyncio.run(check_entries())
            
            # Verify execute_order was not called because it was blocked by the StopLossZone price lock
            mock_execute.assert_not_called()
            
            # Verify the correct rejection log was printed
            log_messages = [call.args[0] for call in mock_logger_info.call_args_list]
            self.assertTrue(any("StopLossZone" in msg for msg in log_messages))

    @patch("core.ctx.ALL_SYMBOLS", ["SOLUSDT"])
    def test_stop_loss_same_zone_lock_allows_when_price_escapes(self):
        from core.check_entries import check_entries
        s = STATES[self.sym]
        s.update({
            "status": "ACTIVE",
            "close_price": 105.0, # 105 is > 0.5% away from 100.0
            "last_entry_price": 100.0,
            "last_entry_direction": "buy",
            "last_exit_reason": "[Stop_Loss] Hit",
            "qty": 0.0,
            "ohlcv": [
                [0, 100.0, 102.0, 99.0, 101.0, 1000.0],
                [1, 100.0, 102.0, 99.0, 101.0, 1000.0],
                [2, 100.0, 102.0, 99.0, 101.0, 1000.0],
            ],
            "vol_ma20": 1000.0,
            "current_atr": 1.0,
            "atr_history": [1.0, 1.0, 1.0],
        })

        with patch("core.check_entries._load_disabled_symbols", return_value=set()), \
             patch("core.check_entries.is_daily_loss_halted", return_value=False), \
             patch("core.check_entries.get_open_position_count", return_value=0), \
             patch("core.balance.get_dynamic_max_slots", return_value=5), \
             patch("core.check_entries._is_confirmable_exit_cooldown", return_value=False), \
             patch("core.check_entries.compute_signal_strength", return_value=("buy", 15.0, "MA_Cross")), \
             patch("core.check_entries.is_entry_candidate_still_valid", return_value=(True, "")), \
             patch("core.check_entries._ma_candidate_quality", return_value=(True, "", 10.0)), \
             patch("core.check_entries.is_entry_allowed", return_value=True), \
             patch("core.check_entries._calc_sl_tp", return_value=(1.0, 1.5, 3.0, 2.0)), \
             patch("core.orders.execute_order") as mock_execute:
            
            asyncio.run(check_entries())
            # Since the price escaped the zone, the order should be executed
            mock_execute.assert_called_once()
