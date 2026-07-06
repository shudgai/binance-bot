import asyncio
import unittest
import sys
import os
import time
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core import exchange_client
from core.orders import (execute_order, check_paper_pending_order, _entry_pending_adverse_guard,
                         _replace_exchange_exit_orders, _entry_exchange_direction_guard,
                         _ensure_exchange_exit_orders, _recover_market_entry_fill)


class EntryRiskTests(unittest.TestCase):
    def test_additional_entry_updates_average_price_safely(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["last_entry_price"] = 100.0
        s["entry_count"] = 1
        s["last_entry_time"] = 0.0
        s["close_price"] = 110.0
        s["current_atr"] = 1.0
        s["current_vol"] = 1000.0
        s["vol_ma20"] = 100.0
        s["macd_line"] = 2.0
        s["macd_signal"] = 1.0
        s["prev_macd_line"] = 1.0
        s["prev_macd_signal"] = 0.8
        s["ohlcv"] = [
            [0, 100, 105, 95, 100, 1000],
            [0, 100, 105, 95, 105, 1000],
            [0, 105, 110, 100, 110, 1000]
        ]

        mock_exchange = AsyncMock()
        mock_exchange.fetch_mark_price.side_effect = Exception("no mark")
        mock_exchange.fetch_ticker.return_value = {"last": 110.0}
        mock_exchange.fetch_order_book.return_value = {"bids": [[110.0, 1000.0]], "asks": [[110.1, 100.0]]}
        mock_exchange.fetch_balance.return_value = {"USDT": {"total": 10000.0, "free": 10000.0}}
        mock_exchange.fetch_order.return_value = {"status": "closed", "filled": 4.5, "average": 110.0, "price": 110.0}
        mock_exchange.fetch_positions.return_value = []
        mock_exchange.create_order.return_value = {"id": "12345"}
        
        with patch("core.orders.compute_per_coin_margin", return_value=3000.0), \
             patch("core.orders.get_balance", return_value=10000.0), \
             patch("core.entry_filter.is_entry_allowed", return_value=True), \
             patch("core.entry_filter.is_entry_volume_confirmed", return_value=True), \
             patch("core.orders.sanitize_order_qty", side_effect=lambda sym, q: 4.5), \
             patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_market_data", mock_exchange), \
             patch("core.orders.exchange_futures", mock_exchange):
            asyncio.run(execute_order(sym, "buy", 110.0))

        self.assertEqual(s["entry_count"], 1)
        self.assertEqual(s["avg_price"], 100.0)

    def test_losing_position_skips_additional_entry(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 95.0
        s["entry_count"] = 1
        s["last_entry_time"] = 0.0

        mock_exchange = AsyncMock()
        mock_exchange.fetch_mark_price.side_effect = Exception("no mark")
        mock_exchange.fetch_ticker.return_value = {"last": 95.0}
        mock_exchange.fetch_order_book.return_value = {"bids": [[95.0, 1000.0]], "asks": [[95.1, 100.0]]}
        with patch("core.orders.compute_per_coin_margin", return_value=3000.0), \
             patch("core.orders.get_balance", return_value=10000.0), \
             patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_market_data", mock_exchange), \
             patch("core.orders.exchange_futures", mock_exchange):
            asyncio.run(execute_order(sym, "buy", 95.0))

        self.assertEqual(s["entry_count"], 1)


    def test_exchange_exit_orders_include_take_profit(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)

        s["qty"] = 2.0
        s["avg_price"] = 100.0
        s["current_atr"] = 1.0
        s["atr_history"] = [1.0] * 20
        s["sl_atr_multiplier"] = 1.0
        s["tp_atr_multiplier"] = 3.0
        s["hard_stop_loss_pct"] = 0.02
        s["entry_reason"] = "a"

        mock_exchange = AsyncMock()
        mock_exchange.create_order.side_effect = [{"id": "sl-1"}, {"id": "tp-1"}]

        async def fake_precision(_sym):
            return {"tick_size": 0.01, "step_size": 0.001}

        with patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_futures", mock_exchange), \
             patch("core.orders.get_contract_precision", side_effect=fake_precision):
            asyncio.run(_replace_exchange_exit_orders(sym))

        order_types = [call.kwargs["type"] for call in mock_exchange.create_order.await_args_list]
        self.assertEqual(order_types, ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
        for call in mock_exchange.create_order.await_args_list:
            self.assertTrue(call.kwargs["params"]["reduceOnly"])
            self.assertEqual(call.kwargs["amount"], 2.0)
        self.assertEqual(s["exchange_stop_order_id"], "sl-1")
        self.assertEqual(s["exchange_take_profit_order_id"], "tp-1")

    def test_pending_adverse_guard_blocks_wrong_way_move(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["current_atr"] = 1.0

        ok, reason = _entry_pending_adverse_guard(sym, "buy", 100.0, 98.5)
        self.assertFalse(ok)
        self.assertIn("pending adverse move", reason)

        ok, reason = _entry_pending_adverse_guard(sym, "sell", 100.0, 101.5)
        self.assertFalse(ok)
        self.assertIn("pending adverse move", reason)

        ok, _ = _entry_pending_adverse_guard(sym, "buy", 100.0, 100.3)
        self.assertTrue(ok)

    def test_paper_pending_order_cancels_when_price_runs_adverse(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["close_price"] = 98.5
        s["current_atr"] = 1.0
        s["pending_paper_order"] = {
            "side": "buy",
            "limit_price": 99.0,
            "signal_price": 100.0,
            "qty": 1.0,
            "margin": 10.0,
            "placed_at": time.time(),
            "timeout": 999999.0,
            "is_rescue_dca": False,
        }

        asyncio.run(check_paper_pending_order(sym))

        self.assertIsNone(s["pending_paper_order"])
        self.assertEqual(s["entry_count"], 0)
        self.assertEqual(s["qty"], 0.0)


    def test_market_entry_blocks_when_signal_price_drifted_from_mark_price(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["close_price"] = 102.0
        s["current_atr"] = 1.0

        market_data = AsyncMock()
        market_data.fetch_order_book.return_value = {
            "bids": [[102.0, 100.0]],
            "asks": [[102.1, 100.0]],
        }
        exchange = AsyncMock()
        exchange.fetch_mark_price.return_value = {"markPrice": 102.0}

        with patch("core.orders.compute_per_coin_margin", return_value=100.0), \
             patch("core.orders.get_balance", return_value=1000.0), \
             patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_market_data", market_data), \
             patch("core.orders.exchange_futures", exchange):
            asyncio.run(execute_order(
                sym, "buy", 100.0, entry_mode_override="market",
            ))

        exchange.create_order.assert_not_awaited()
        self.assertEqual(s["qty"], 0.0)

    def test_filled_entry_uses_exchange_position_without_double_counting(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["close_price"] = 100.0
        s["current_atr"] = 1.0

        market_data = AsyncMock()
        market_data.fetch_order_book.return_value = {
            "bids": [[100.0, 100.0]],
            "asks": [[100.1, 100.0]],
        }
        exchange = AsyncMock()
        exchange.fetch_mark_price.return_value = {"markPrice": 100.0}
        exchange.fetch_order_book.return_value = {
            "bids": [[100.0, 100.0]],
            "asks": [[100.1, 100.0]],
        }
        exchange.fetch_balance.return_value = {
            "USDT": {"total": 1000.0, "free": 1000.0},
        }
        exchange.create_order.return_value = {"id": "entry-1"}
        exchange.fetch_order.return_value = {
            "status": "closed", "filled": 1.0, "average": 100.1,
        }
        exchange.fetch_positions.side_effect = [[], [{
            "symbol": "XRP/USDT:USDT",
            "contracts": 1.0,
            "side": "long",
            "entryPrice": 100.25,
            "info": {"positionAmt": "1.0"},
        }]]

        async def fake_precision(_sym):
            return {"tick_size": 0.01, "step_size": 0.001}

        with patch("core.orders.compute_per_coin_margin", return_value=100.0), \
             patch("core.orders.get_balance", return_value=1000.0), \
             patch("core.orders.sanitize_order_qty", return_value=1.0), \
             patch("core.orders.get_contract_precision", side_effect=fake_precision), \
             patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_market_data", market_data), \
             patch("core.orders.exchange_futures", exchange), \
             patch("core.orders.asyncio.sleep", new=AsyncMock()), \
             patch("core.orders._replace_exchange_exit_orders", new=AsyncMock()), \
             patch("core.orders._import_update_trailing_stop", return_value=lambda *_args: None):
            asyncio.run(execute_order(
                sym, "buy", 100.0, entry_mode_override="chase",
            ))

        self.assertEqual(s["qty"], 1.0)
        self.assertEqual(s["avg_price"], 100.25)
        self.assertEqual(s["entry_count"], 1)


    def test_exchange_direction_guard_blocks_stale_opposite_position(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)

        exchange = AsyncMock()
        exchange.fetch_positions.return_value = [{
            "symbol": "XRP/USDT:USDT",
            "contracts": 2.0,
            "side": "short",
            "entryPrice": 101.5,
            "info": {"positionAmt": "-2.0"},
        }]

        with patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_futures", exchange):
            allowed, reason = asyncio.run(
                _entry_exchange_direction_guard(sym, "buy")
            )

        self.assertFalse(allowed)
        self.assertIn("local position stale", reason)
        self.assertEqual(s["qty"], -2.0)
        self.assertEqual(s["avg_price"], 101.5)

    def test_final_direction_revalidation_blocks_flipped_signal(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["close_price"] = 100.0
        s["current_atr"] = 1.0

        market_data = AsyncMock()
        market_data.fetch_order_book.return_value = {
            "bids": [[100.0, 100.0]],
            "asks": [[100.1, 100.0]],
        }
        exchange = AsyncMock()
        exchange.fetch_mark_price.return_value = {"markPrice": 100.0}

        with patch("core.orders.compute_per_coin_margin", return_value=100.0), \
             patch("core.orders.get_balance", return_value=1000.0), \
             patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_market_data", market_data), \
             patch("core.orders.exchange_futures", exchange), \
             patch("core.check_entries.is_entry_candidate_still_valid", return_value=(False, "latest signal is sell")):
            asyncio.run(execute_order(
                sym, "buy", 100.0, signal_strength=20.0, entry_route="a",
            ))

        exchange.create_order.assert_not_awaited()
        exchange.fetch_positions.assert_not_awaited()
        self.assertEqual(s["qty"], 0.0)


    def test_ensure_exit_orders_adopts_existing_exchange_bracket(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 2.0
        s["avg_price"] = 100.0

        exchange = AsyncMock()
        exchange.fapiPrivateGetOpenAlgoOrders.return_value = [
            {"algoId": "sl-live", "orderType": "STOP_MARKET", "reduceOnly": True,
             "quantity": "2.0", "side": "SELL", "algoStatus": "NEW", "createTime": "1"},
            {"algoId": "tp-live", "orderType": "TAKE_PROFIT_MARKET", "reduceOnly": True,
             "quantity": "2.0", "side": "SELL", "algoStatus": "NEW", "createTime": "2"},
        ]

        with patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_futures", exchange), \
             patch("core.orders._replace_exchange_exit_orders", new=AsyncMock()) as replace_orders:
            asyncio.run(_ensure_exchange_exit_orders(sym))

        replace_orders.assert_not_awaited()
        self.assertEqual(s["exchange_stop_order_id"], "sl-live")
        self.assertEqual(s["exchange_take_profit_order_id"], "tp-live")

    def test_ensure_exit_orders_cancels_stale_algo_orders(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 2.0
        s["avg_price"] = 100.0

        exchange = AsyncMock()
        exchange.fapiPrivateGetOpenAlgoOrders.return_value = [
            {"algoId": "sl-current", "orderType": "STOP_MARKET", "reduceOnly": True,
             "quantity": "2.0", "side": "SELL", "algoStatus": "NEW", "createTime": "3"},
            {"algoId": "tp-current", "orderType": "TAKE_PROFIT_MARKET", "reduceOnly": True,
             "quantity": "2.0", "side": "SELL", "algoStatus": "NEW", "createTime": "4"},
            {"algoId": "sl-stale", "orderType": "STOP_MARKET", "reduceOnly": True,
             "quantity": "1.0", "side": "SELL", "algoStatus": "NEW", "createTime": "1"},
        ]

        with patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_futures", exchange), \
             patch("core.orders._replace_exchange_exit_orders", new=AsyncMock()) as replace_orders:
            asyncio.run(_ensure_exchange_exit_orders(sym))

        replace_orders.assert_not_awaited()
        exchange.fapiPrivateDeleteAlgoOrder.assert_awaited_once_with({
            "symbol": sym, "algoId": "sl-stale",
        })

    def test_ensure_exit_orders_repairs_missing_exchange_bracket(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 2.0
        s["avg_price"] = 100.0

        exchange = AsyncMock()
        exchange.fapiPrivateGetOpenAlgoOrders.return_value = []

        with patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_futures", exchange), \
             patch("core.orders._replace_exchange_exit_orders", new=AsyncMock()) as replace_orders:
            asyncio.run(_ensure_exchange_exit_orders(sym))

        replace_orders.assert_awaited_once_with(sym)

    def test_market_fill_recovers_from_exchange_position_delta(self):
        sym = "XRPUSDT"
        exchange = AsyncMock()
        exchange.fetch_positions.return_value = [{
            "symbol": "XRP/USDT:USDT",
            "contracts": 1.5,
            "side": "long",
            "entryPrice": 100.25,
            "info": {"positionAmt": "1.5"},
        }]

        with patch("core.orders.exchange_futures", exchange):
            recovered = asyncio.run(_recover_market_entry_fill(
                sym, "buy", prior_qty=0.5, requested_qty=1.0,
                fallback_price=100.0,
            ))

        self.assertEqual(recovered["status"], "closed")
        self.assertEqual(recovered["filled"], 1.0)
        self.assertEqual(recovered["average"], 100.25)


if __name__ == "__main__":
    unittest.main()
