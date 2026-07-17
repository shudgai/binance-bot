import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ctx
from core.ctx import init_states
from core.runner import calibrate_with_exchange, cancel_orphan_exchange_entry_orders
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

    def test_startup_cancels_only_untracked_entry_orders(self):
        exchange = AsyncMock()
        exchange.options = {"warnOnFetchOpenOrdersWithoutSymbol": True}
        exchange.fetch_open_orders.return_value = [
            {"id": "orphan-entry", "symbol": "XRP/USDT:USDT", "status": "open",
             "side": "buy", "type": "limit", "reduceOnly": False},
            {"id": "tracked-entry", "symbol": "XRP/USDT:USDT", "status": "open",
             "side": "buy", "type": "limit", "reduceOnly": False},
            {"id": "protective-stop", "symbol": "XRP/USDT:USDT", "status": "open",
             "side": "sell", "type": "stop_market", "info": {"reduceOnly": "true"}},
        ]
        original_pending = dict(ctx.PENDING_LIMIT_ORDERS)
        try:
            ctx.PENDING_LIMIT_ORDERS.clear()
            ctx.PENDING_LIMIT_ORDERS["tracked-entry"] = {"sym": self.sym}
            cancelled = asyncio.run(cancel_orphan_exchange_entry_orders(exchange))
        finally:
            ctx.PENDING_LIMIT_ORDERS.clear()
            ctx.PENDING_LIMIT_ORDERS.update(original_pending)

        self.assertEqual(cancelled, 1)
        self.assertTrue(exchange.options["warnOnFetchOpenOrdersWithoutSymbol"])
        exchange.cancel_order.assert_awaited_once_with(
            "orphan-entry", "XRP/USDT:USDT",
        )

    def test_startup_orphan_scan_preserves_close_position_order(self):
        exchange = AsyncMock()
        exchange.fetch_open_orders.return_value = [{
            "id": "protect-position", "symbol": "XRP/USDT:USDT",
            "status": "new", "info": {"closePosition": "true"},
        }]

        cancelled = asyncio.run(cancel_orphan_exchange_entry_orders(exchange))

        self.assertEqual(cancelled, 0)
        exchange.cancel_order.assert_not_awaited()

    def test_startup_orphan_scan_failure_does_not_block_boot(self):
        exchange = AsyncMock()
        exchange.fetch_open_orders.side_effect = RuntimeError("temporary API error")

        cancelled = asyncio.run(cancel_orphan_exchange_entry_orders(exchange))

        self.assertEqual(cancelled, 0)
        exchange.cancel_order.assert_not_awaited()

    def test_exchange_closed_position_cancels_remaining_exit_orders_and_resets_state(self):
        state = ctx.STATES[self.sym]
        state["qty"] = 2.0
        state["avg_price"] = 100.0
        state["exchange_stop_order_id"] = "sl-1"
        state["exchange_take_profit_order_id"] = "tp-1"

        exchange = AsyncMock()
        exchange.fetch_positions.return_value = []

        with patch("core.runner.PAPER_TRADING", False), \
             patch("core.orders._cancel_exchange_exit_order_id", new=AsyncMock()) as cancel_exit:
            asyncio.run(calibrate_with_exchange(exchange))

        self.assertEqual(
            cancel_exit.await_args_list,
            [
                call(self.sym, "sl-1", "校準殘留止損"),
                call(self.sym, "tp-1", "校準殘留停利"),
            ],
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
        cancel_exit = AsyncMock(side_effect=[RuntimeError("unknown order"), None])

        with patch("core.runner.PAPER_TRADING", False), \
             patch("core.orders._cancel_exchange_exit_order_id", new=cancel_exit):
            asyncio.run(calibrate_with_exchange(exchange))

        self.assertEqual(cancel_exit.await_count, 2)
        self.assertEqual(state["qty"], 0.0)
        self.assertIsNone(state["exchange_stop_order_id"])
        self.assertIsNone(state["exchange_take_profit_order_id"])

    def test_external_manual_close_records_exchange_realized_pnl_before_reset(self):
        state = ctx.STATES[self.sym]
        state["qty"] = -2.0
        state["avg_price"] = 100.0
        state["open_time"] = time.time() - 600
        state["entry_reason"] = "test-entry"

        exchange = AsyncMock()
        exchange.fetch_positions.return_value = []
        exchange.fetch_my_trades.return_value = [{
            "id": "trade-1",
            "order": "manual-order-1",
            "timestamp": int(time.time() * 1000),
            "side": "buy",
            "amount": 2.0,
            "price": 105.0,
            "fee": {"cost": 0.2},
            "info": {"realizedPnl": "-10.0"},
        }]

        with patch("core.runner.PAPER_TRADING", False), \
             patch("core.orders._cancel_exchange_exit_order_id", new=AsyncMock()), \
             patch("core.orders.record_trade_result", return_value=True) as record_result:
            asyncio.run(calibrate_with_exchange(exchange))

        kwargs = record_result.call_args.kwargs
        self.assertEqual(kwargs["exchange_close_id"], "XRPUSDT:manual-order-1")
        self.assertEqual(kwargs["realized_pnl_usdt"], -10.0)
        self.assertEqual(kwargs["fees"], 0.2)
        self.assertEqual(kwargs["actual_exit"], 105.0)
        self.assertEqual(state["qty"], 0.0)

    def test_exchange_close_id_is_deduplicated_in_trade_history(self):
        from core.orders import record_trade_result

        with tempfile.TemporaryDirectory() as temp_dir:
            history_path = os.path.join(temp_dir, "trade_history.json")
            with patch("core.orders.TRADE_HISTORY_FILE", history_path):
                first = record_trade_result(
                    "XRPUSDT", "entry", "[External_Close]", -0.05, 1.0,
                    expected_entry=100.0, expected_exit=105.0,
                    actual_entry=100.0, actual_exit=105.0,
                    fees=0.2, qty=2.0,
                    exchange_close_id="XRPUSDT:manual-order-1",
                    realized_pnl_usdt=-10.0,
                    timestamp_ms=1700000000000,
                )
                second = record_trade_result(
                    "XRPUSDT", "entry", "[External_Close]", -0.05, 1.0,
                    expected_entry=100.0, expected_exit=105.0,
                    actual_entry=100.0, actual_exit=105.0,
                    fees=0.2, qty=2.0,
                    exchange_close_id="XRPUSDT:manual-order-1",
                    realized_pnl_usdt=-10.0,
                    timestamp_ms=1700000000000,
                )
            with open(history_path, "r", encoding="utf-8") as handle:
                history = json.load(handle)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["realized_pnl_usdt"], -10.0)


    def test_live_position_calibration_ensures_exchange_exit_orders(self):
        exchange = AsyncMock()
        exchange.fetch_positions.return_value = [{
            "symbol": "XRP/USDT:USDT",
            "contracts": 2.0,
            "side": "long",
            "entryPrice": 100.0,
            "info": {"positionAmt": "2.0"},
        }]

        with patch("core.runner.PAPER_TRADING", False), \
             patch("core.orders._ensure_exchange_exit_orders", new=AsyncMock()) as ensure_orders:
            asyncio.run(calibrate_with_exchange(exchange))

        ensure_orders.assert_awaited_once_with(self.sym)
        self.assertEqual(ctx.STATES[self.sym]["qty"], 2.0)
        self.assertEqual(ctx.STATES[self.sym]["avg_price"], 100.0)
        self.assertEqual(ctx.STATES[self.sym]["first_entry_price"], 100.0)
        self.assertEqual(ctx.STATES[self.sym]["last_entry_price"], 100.0)
        self.assertEqual(ctx.STATES[self.sym]["last_entry_direction"], "buy")
        self.assertTrue(ctx.STATES[self.sym]["restored_from_exchange"])

    def test_live_position_calibration_does_not_overwrite_qty_while_closing(self):
        state = ctx.STATES[self.sym]
        state["qty"] = -2.0
        state["avg_price"] = 100.0
        state["_is_closing"] = True

        exchange = AsyncMock()
        exchange.fetch_positions.return_value = [{
            "symbol": "XRP/USDT:USDT",
            "contracts": 1.0,
            "side": "short",
            "entryPrice": 100.0,
            "info": {"positionAmt": "-1.0"},
        }]

        with patch("core.runner.PAPER_TRADING", False), \
             patch("core.orders._ensure_exchange_exit_orders", new=AsyncMock()) as ensure_orders:
            asyncio.run(calibrate_with_exchange(exchange))

        self.assertEqual(state["qty"], -2.0)
        ensure_orders.assert_not_awaited()

    def test_restored_short_keeps_a_valid_hard_stop_reference(self):
        exchange = AsyncMock()
        exchange.fetch_positions.return_value = [{
            "symbol": "XRP/USDT:USDT",
            "contracts": 2.0,
            "side": "short",
            "entryPrice": 100.0,
            "info": {"positionAmt": "-2.0"},
        }]

        with patch("core.runner.PAPER_TRADING", False), \
             patch("core.orders._ensure_exchange_exit_orders", new=AsyncMock()):
            asyncio.run(calibrate_with_exchange(exchange))

        state = ctx.STATES[self.sym]
        self.assertEqual(state["qty"], -2.0)
        self.assertEqual(state["first_entry_price"], 100.0)
        self.assertEqual(state["last_entry_direction"], "sell")
        self.assertTrue(state["restored_from_exchange"])


if __name__ == "__main__":
    unittest.main()
