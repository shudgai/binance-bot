import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ctx
from core.ctx import init_states
from core.runner import calibrate_with_exchange
from core.state_manager import reset_coin_state


class ExchangeCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.sym = "XRPUSDT"
        self.original_symbols = list(ctx.ALL_SYMBOLS)
        self.original_state = ctx.STATES.get(self.sym)
        ctx.ALL_SYMBOLS[:] = [sym for sym in ctx.ALL_SYMBOLS if sym != self.sym]
        ctx.STATES.pop(self.sym, None)
        init_states([self.sym])
        reset_coin_state(self.sym)

    def tearDown(self):
        ctx.ALL_SYMBOLS[:] = self.original_symbols
        if self.original_state is None:
            ctx.STATES.pop(self.sym, None)
        else:
            ctx.STATES[self.sym] = self.original_state

    def test_exchange_closed_position_cancels_remaining_exit_orders_and_resets_state(self):
        state = ctx.STATES[self.sym]
        state["qty"] = 2.0
        state["avg_price"] = 100.0
        state["exchange_stop_order_id"] = "sl-1"
        state["exchange_take_profit_order_id"] = "tp-1"

        exchange = AsyncMock()
        exchange.fetch_positions.return_value = []

        with patch("core.runner.PAPER_TRADING", False):
            asyncio.run(calibrate_with_exchange(exchange))

        self.assertEqual(
            exchange.cancel_order.await_args_list,
            [call("sl-1", self.sym), call("tp-1", self.sym)],
        )
        self.assertEqual(state["qty"], 0.0)
        self.assertEqual(state["avg_price"], 0.0)
        self.assertIsNone(state["exchange_stop_order_id"])
        self.assertIsNone(state["exchange_take_profit_order_id"])

    def test_exchange_closed_position_resets_state_when_triggered_order_is_already_gone(self):
        state = ctx.STATES[self.sym]
        state["qty"] = -2.0
        state["avg_price"] = 100.0
        state["exchange_stop_order_id"] = "triggered-sl"
        state["exchange_take_profit_order_id"] = "tp-1"

        exchange = AsyncMock()
        exchange.fetch_positions.return_value = []
        exchange.cancel_order.side_effect = [RuntimeError("unknown order"), None]

        with patch("core.runner.PAPER_TRADING", False):
            asyncio.run(calibrate_with_exchange(exchange))

        self.assertEqual(exchange.cancel_order.await_count, 2)
        self.assertEqual(state["qty"], 0.0)
        self.assertIsNone(state["exchange_stop_order_id"])
        self.assertIsNone(state["exchange_take_profit_order_id"])


if __name__ == "__main__":
    unittest.main()
