import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ctx
from core.ctx import init_states
from core.orders import _record_missed_limit_fill
from core.state_manager import reset_coin_state


class PendingLimitFillTests(unittest.TestCase):
    def setUp(self):
        self.sym = "INJUSDT"
        self.original_state = ctx.STATES.get(self.sym)
        ctx.STATES.pop(self.sym, None)
        init_states([self.sym])
        reset_coin_state(self.sym)

    def tearDown(self):
        if self.original_state is None:
            ctx.STATES.pop(self.sym, None)
        else:
            ctx.STATES[self.sym] = self.original_state

    def test_partial_fill_is_adopted_as_fresh_position_without_old_peak(self):
        state = ctx.STATES[self.sym]
        state.update({
            "highest_profit_pct": 0.004,
            "max_profit_reached": 0.004,
            "ma_peak_saved_pct": 0.004,
            "ma_peak_lock_armed": True,
            "ma_peak_lock_price": 5.17,
            "ma_profit_floor_armed": True,
            "ma_profit_floor_price": 5.167,
            "ma_profit_floor_cross_count": 1,
            "ma_profit_floor_cross_since": 123.0,
            "realtime_peak_candidate_price": 5.17,
            "realtime_peak_candidate_profit": 0.004,
            "realtime_peak_candidate_time": 123.0,
            "dynamic_exit_manager": object(),
        })
        positions = [{
            "symbol": "INJ/USDT:USDT",
            "contracts": 5.9,
            "side": "long",
            "entryPrice": 5.153,
            "info": {"entryPrice": "5.153"},
        }]
        fetched = {
            "status": "open",
            "filled": 5.9,
            "average": 5.153,
            "price": 5.153,
        }
        info = {
            "entry_route": "MA7_Simple",
            "signal_strength": 26.06,
        }

        with patch("core.entry_time_store.save_entry_time"), \
             patch("core.entry_reason_store.save_entry_reason"), \
             patch("core.orders.clear_peak") as clear_peak, \
             patch("core.orders._ensure_exchange_exit_orders", new=AsyncMock()) as ensure:
            asyncio.run(_record_missed_limit_fill(
                self.sym, "buy", "partial-1", info, fetched, positions=positions,
            ))

        self.assertEqual(state["qty"], 5.9)
        self.assertEqual(state["avg_price"], 5.153)
        self.assertEqual(state["entry_count"], 1)
        self.assertEqual(state["entry_reason"], "MA7_Simple")
        self.assertEqual(state["entry_strength"], 26.06)
        self.assertEqual(state["highest_profit_pct"], 0.0)
        self.assertEqual(state["max_profit_reached"], 0.0)
        self.assertEqual(state["ma_peak_saved_pct"], 0.0)
        self.assertFalse(state["ma_peak_lock_armed"])
        self.assertFalse(state["ma_profit_floor_armed"])
        self.assertEqual(state["realtime_peak_candidate_profit"], 0.0)
        self.assertNotIn("dynamic_exit_manager", state)
