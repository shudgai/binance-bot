import asyncio
import time
import unittest
from unittest.mock import patch

from core import ctx
from core.ctx import init_states
from core.exits import _opposite_signal_blocks_rescue
from core.orders import is_effective_rescue_dca
from core.signal_engine import is_eligible_for_reverse
from core.state_manager import reset_coin_state


class RescueDirectionTests(unittest.TestCase):
    def setUp(self):
        self.sym = "XRPUSDT"
        init_states([self.sym])
        reset_coin_state(self.sym)
        self.s = ctx.STATES[self.sym]

    def test_rescue_price_and_average_must_both_improve(self):
        self.s.update(qty=-1000.0, avg_price=100.0, current_atr=0.5)
        self.assertFalse(is_effective_rescue_dca(self.s, "sell", 100.5, 100.0)[0])
        self.assertFalse(is_effective_rescue_dca(self.s, "sell", 101.0, 10.0)[0])
        self.assertTrue(is_effective_rescue_dca(self.s, "sell", 102.0, 500.0)[0])

    def test_opposite_signal_blocks_rescue(self):
        with patch("core.signal_engine.compute_signal_strength", return_value=("buy", 12.2, "a")):
            blocked, _ = _opposite_signal_blocks_rescue(self.sym, False)
        self.assertTrue(blocked)

    def test_losing_position_can_enter_reverse_confirmation_at_twelve(self):
        self.s.update(qty=-100.0, avg_price=100.0, close_price=101.0,
                      open_time=time.time() - 600, last_reverse_time=0.0,
                      pending_reverse_trigger=None)
        self.assertTrue(asyncio.run(is_eligible_for_reverse(self.sym, 12.2)))

    def test_profitable_position_keeps_fifteen_threshold(self):
        self.s.update(qty=-100.0, avg_price=100.0, close_price=99.0,
                      open_time=time.time() - 600, last_reverse_time=0.0,
                      pending_reverse_trigger=None)
        self.assertFalse(asyncio.run(is_eligible_for_reverse(self.sym, 12.2)))


if __name__ == "__main__":
    unittest.main()
