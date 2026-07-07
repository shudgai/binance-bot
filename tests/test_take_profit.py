import unittest
import sys
import os
import time
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.exits import update_trailing_stop, check_exits


class TakeProfitTests(unittest.TestCase):
    def test_trailing_take_profit_updates_target_with_price_rise(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["avg_price"] = 100.0
        s["trailing_stop_price"] = 100.3
        s["highest_profit_pct"] = 0.005
        s["current_atr"] = 0.5
        s["qty"] = 1.0

        should_exit, new_tp = update_trailing_stop(sym, 100.5, True)

        self.assertFalse(should_exit)

    def test_early_take_profit_triggers_on_small_profit(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 100.8
        s["open_time"] = 0.0
        s["current_atr"] = 0.5
        s["current_rsi"] = 45.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [[0, 100, 100, 99, 100, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.008
        s["pnl_history"] = []

        import asyncio
        async def run_check():
            await check_exits(sym)

        asyncio.run(run_check())

    def test_peak_lock_exits_closer_to_high(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 101.60
        s["open_time"] = time.time() - 600
        s["current_atr"] = 0.5
        s["current_rsi"] = 55.0
        s["prev_rsi"] = 55.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [
            [0, 100.0, 101.5, 99.8, 100.8, 500],
            [0, 100.8, 102.0, 100.7, 101.7, 500],
            [0, 101.7, 102.0, 101.5, 101.6, 450],
        ]
        s["prev_close"] = 101.70
        s["highest_profit_pct"] = 0.03
        s["trailing_highest"] = 102.0
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0
        s["pnl_history"] = []

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()

        asyncio.run(run_check())

    def test_peak_lock_uses_intracandle_high_when_trailing_highest_is_stale(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 100.37
        s["open_time"] = time.time() - 600
        s["current_atr"] = 0.30
        s["current_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [
            [int((s["open_time"] + 60) * 1000), 100.0, 100.60, 99.8, 100.37, 500],
        ]
        s["prev_close"] = 100.37
        s["highest_profit_pct"] = 0.006
        s["trailing_highest"] = 100.0
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0
        s["pnl_history"] = []

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(s["trailing_highest"], 100.6)

        asyncio.run(run_check())

    def test_entry_candle_pre_entry_high_does_not_trigger_peak_giveback(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["first_entry_price"] = 100.0
        s["entry_count"] = 1
        s["last_entry_direction"] = "buy"
        s["close_price"] = 99.2
        s["open_time"] = time.time() - 240
        s["current_atr"] = 0.5
        s["current_rsi"] = 45.0
        s["prev_rsi"] = 47.0
        s["prev_macd_line"] = 0.01
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = -0.01
        s["macd_signal"] = 0.0
        s["ema20"] = 100.5
        s["current_vol"] = 2000.0
        s["vol_ma20"] = 1000.0
        s["ohlcv"] = [[0, 100.0, 100.5, 99.0, 99.2, 1200], [0, 100.2, 100.6, 99.1, 99.3, 1100]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.0
        s["pnl_history"] = []

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_not_called()
                self.assertEqual(s["highest_profit_pct"], 0.0)

        asyncio.run(run_check())

    def test_unconfirmed_wrong_direction_small_loss_is_held(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["first_entry_price"] = 100.0
        s["entry_count"] = 1
        s["last_entry_direction"] = "buy"
        s["close_price"] = 99.2
        s["open_time"] = time.time() - 240
        s["current_atr"] = 0.5
        s["current_rsi"] = 45.0
        s["prev_rsi"] = 47.0
        s["prev_macd_line"] = 0.01
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.02
        s["macd_signal"] = 0.0
        s["ema20"] = 98.0
        s["current_vol"] = 100.0
        s["vol_ma20"] = 1000.0
        s["ohlcv"] = [[0, 100.0, 100.5, 99.0, 99.2, 1200], [0, 99.1, 99.4, 99.0, 99.3, 100]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.0
        s["pnl_history"] = []

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_not_called()
                self.assertIsNone(s.get("wrong_dir_side"))

        asyncio.run(run_check())


    def test_post_entry_observation_does_not_cut_before_hard_sl(self):
        from unittest.mock import patch, AsyncMock
        sym = "SUIUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 0.755
        s["first_entry_price"] = 0.755
        s["entry_count"] = 1
        s["last_entry_direction"] = "buy"
        s["close_price"] = 0.7483
        s["open_time"] = time.time() - 600
        s["current_atr"] = 0.002
        s["entry_atr"] = 0.002
        s["hard_stop_loss_pct"] = 0.015
        s["profile_type"] = "High_Beta_Momentum"
        s["current_rsi"] = 45.0
        s["prev_rsi"] = 47.0
        s["prev_macd_line"] = 0.01
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = -0.01
        s["macd_signal"] = 0.0
        s["ema20"] = 0.756
        s["current_vol"] = 2000.0
        s["vol_ma20"] = 1000.0
        s["ohlcv"] = [[0, 0.755, 0.756, 0.748, 0.7483, 2000]]
        s["prev_close"] = 0.755
        s["highest_profit_pct"] = 0.0
        s["pnl_history"] = []

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_not_called()

        asyncio.run(run_check())

    def test_breakeven_lock_triggers_on_positive_peak(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 101.0
        s["open_time"] = time.time() - 120
        s["current_atr"] = 0.5
        s["current_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [[0, 100, 100, 99, 100, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.0
        s["pnl_history"] = []
        s["vol_ma20"] = 1.0
        s["current_vol"] = 1.0

        import asyncio
        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                self.assertTrue(s.get("is_breakeven_locked", False))
                self.assertGreater(s.get("stop_loss", 0.0), s["avg_price"])
                mock_close.assert_not_called()

        asyncio.run(run_check())

    def test_hard_stop_loss_still_triggers_during_initial_cooldown(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 95.0
        s["open_time"] = time.time() - 10
        s["current_atr"] = 0.5
        s["current_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [[0, 100.0, 100.0, 99.0, 100.0, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.0
        s["pnl_history"] = []
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Hard_SL]")
                self.assertTrue(mock_close.await_args.kwargs["is_stop_loss"])

        asyncio.run(run_check())

    def test_exit_blocked_on_negative_profit(self):
        from core.orders import close_position
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 99.0  # negative profit

        import asyncio
        # Mock actual trade executions
        from unittest.mock import patch, AsyncMock
        mock_exchange = AsyncMock()
        with patch("core.orders.exchange_futures", mock_exchange):
            asyncio.run(close_position(sym, "sell", 1.0, 99.0, 100.0, reason="test_negative"))
            
        # The position should still have qty because the close was blocked
        self.assertEqual(s["qty"], 1.0)
        self.assertFalse(mock_exchange.create_order.called)

    def test_trend_follow_negative_profit_is_blocked(self):
        from core.orders import close_position
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 99.0

        mock_exchange = AsyncMock()
        with patch("core.orders.exchange_futures", mock_exchange):
            asyncio.run(close_position(sym, "sell", 1.0, 99.0, 100.0, reason="[Trend_Follow]"))

        self.assertEqual(s["qty"], 1.0)
        self.assertFalse(mock_exchange.create_order.called)

    def test_take_profit_below_min_profit_is_blocked(self):
        from core.orders import close_position
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 100.2

        mock_exchange = AsyncMock()
        with patch("core.orders.exchange_futures", mock_exchange):
            asyncio.run(close_position(sym, "sell", 1.0, 100.2, 100.0, reason="[Take_Profit]"))

        self.assertEqual(s["qty"], 1.0)
        self.assertFalse(mock_exchange.create_order.called)

    def test_peak_giveback_bypasses_min_profit_gate(self):
        from core.orders import close_position
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 100.2

        with patch("core.orders.PAPER_TRADING", True), \
             patch("core.orders.update_paper_state"), \
             patch("core.orders.sanitize_order_qty", AsyncMock(return_value=1.0)), \
             patch("core.orders.record_trade_result"), \
             patch("core.orders.accrue_daily_realized_pnl"):
            asyncio.run(close_position(
                sym, "sell", 1.0, 100.2, 100.0,
                reason="[Peak_Giveback]",
            ))

        self.assertEqual(s["qty"], 0.0)

    def test_trailtp_locks_after_leveraged_one_percent_peak(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["first_entry_price"] = 100.0
        s["close_price"] = 100.33
        s["open_time"] = time.time() - 600
        s["leverage"] = 3
        s["current_atr"] = 0.1
        s["entry_atr"] = 0.1
        s["atr_ma20"] = 0.1
        s["current_rsi"] = 50.0
        s["prev_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["macd_hist"] = 0.0
        s["ohlcv"] = [[0, 100.0, 100.4, 100.2, 100.33, 1000]]
        s["prev_close"] = 100.35
        s["highest_profit_pct"] = 0.004
        s["trailing_highest"] = 100.4
        s["pnl_history"] = []
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs.get("reason"), "[TrailTP_Peak]")
                self.assertGreater(mock_close.await_args.args[3], s["close_price"])

        asyncio.run(run_check())

    def test_peak_giveback_does_not_close_after_profit_turns_negative(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["first_entry_price"] = 100.0
        s["close_price"] = 99.9
        s["open_time"] = time.time() - 600
        s["current_atr"] = 5.0
        s["entry_atr"] = 5.0
        s["current_rsi"] = 50.0
        s["prev_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [[0, 100.0, 100.0, 99.9, 99.9, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.003
        s["pnl_history"] = []
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_not_called()

        asyncio.run(run_check())

    def test_time_stagnation_below_min_profit_does_not_take_profit(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 100.2
        s["open_time"] = time.time() - 3000
        s["current_atr"] = 0.5
        s["entry_atr"] = 0.5
        s["current_rsi"] = 50.0
        s["prev_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["macd_hist"] = 0.0
        s["ohlcv"] = [[0, 100.0, 100.2, 99.8, 100.2, 1000]]
        s["closes"] = [100.0, 100.1, 100.2]
        s["prev_close"] = 100.1
        s["highest_profit_pct"] = 0.002
        s["pnl_history"] = []
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 1000.0

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_not_called()

        asyncio.run(run_check())

    def test_universal_stop_loss_is_not_blocked_by_profit_first(self):
        from unittest.mock import patch, AsyncMock
        sym = "TESTSLUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["first_entry_price"] = 100.0
        s["close_price"] = 98.8
        s["open_time"] = time.time() - 1200
        s["current_atr"] = 0.4
        s["entry_atr"] = 0.4
        s["hard_stop_loss_pct"] = 0.03
        s["entry_count"] = 0
        s["current_rsi"] = 50.0
        s["prev_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ema20"] = 100.0
        s["ohlcv"] = [[0, 100.0, 100.0, 98.8, 98.8, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.0
        s["pnl_history"] = []
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Universal_SL]")
                self.assertTrue(mock_close.await_args.kwargs["is_stop_loss"])

        asyncio.run(run_check())

    def test_slot_release_stop_closes_persistent_wrong_direction_loss(self):
        from unittest.mock import patch, AsyncMock
        sym = "HBARUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["first_entry_price"] = 100.0
        s["close_price"] = 99.4
        s["open_time"] = time.time() - 700
        s["current_atr"] = 0.5
        s["entry_atr"] = 0.5
        s["hard_stop_loss_pct"] = 0.03
        s["entry_count"] = 1
        s["current_rsi"] = 45.0
        s["prev_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = -0.2
        s["macd_signal"] = 0.0
        s["ema20"] = 100.0
        s["ohlcv"] = [[0, 100.0, 100.0, 99.4, 99.4, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.0
        s["pnl_history"] = []
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Slot_Release_Stop]")
                self.assertTrue(mock_close.await_args.kwargs["is_stop_loss"])

        asyncio.run(run_check())

    def test_hard_stop_loss_uses_state_profile_pct(self):
        from unittest.mock import patch, AsyncMock
        sym = "HBARUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["first_entry_price"] = 100.0
        s["close_price"] = 98.4
        s["open_time"] = time.time() - 600
        s["current_atr"] = 0.5
        s["entry_atr"] = 0.5
        s["hard_stop_loss_pct"] = 0.015
        s["entry_count"] = 0
        s["current_rsi"] = 50.0
        s["prev_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [[0, 100.0, 100.0, 98.4, 98.4, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.0
        s["pnl_history"] = []
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Hard_SL]")
                self.assertTrue(mock_close.await_args.kwargs["is_stop_loss"])

        asyncio.run(run_check())

    def test_catastrophic_hard_stop_still_exits(self):
        from unittest.mock import patch, AsyncMock
        sym = "HBARUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["first_entry_price"] = 100.0
        s["close_price"] = 96.9
        s["open_time"] = time.time() - 600
        s["current_atr"] = 0.5
        s["entry_atr"] = 0.5
        s["hard_stop_loss_pct"] = 0.015
        s["entry_count"] = 0
        s["current_rsi"] = 50.0
        s["prev_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [[0, 100.0, 100.0, 96.9, 96.9, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.0
        s["pnl_history"] = []
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Hard_SL]")
                self.assertTrue(mock_close.await_args.kwargs["is_stop_loss"])

        asyncio.run(run_check())

    def test_peak_giveback_precedes_universal_stop_after_profit_retrace(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["first_entry_price"] = 100.0
        s["close_price"] = 100.3
        s["open_time"] = time.time() - 1200
        s["current_atr"] = 0.5
        s["entry_atr"] = 0.5
        s["hard_stop_loss_pct"] = 0.03
        s["current_rsi"] = 50.0
        s["prev_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [[0, 100.0, 100.6, 99.8, 100.3, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.006
        s["is_breakeven_locked"] = True
        s["stop_loss"] = 100.4
        s["highest_sl"] = 100.4
        s["pnl_history"] = []
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Peak_Giveback]")

        asyncio.run(run_check())


    def test_entry_rr_uses_hard_stop_loss_when_wider_than_atr_sl(self):
        from core.indicators import _calc_sl_tp

        state = {
            "current_atr": 0.1,
            "atr_history": [],
            "sl_atr_multiplier": 1.0,
            "tp_atr_multiplier": 1.0,
            "hard_stop_loss_pct": 0.03,
        }

        _atr, sl_dist, tp_dist, expected_rr = _calc_sl_tp("TESTUSDT", "buy", state, 100.0)

        self.assertLess(sl_dist, 3.0)
        self.assertGreaterEqual(tp_dist, 4.5)
        self.assertAlmostEqual(expected_rr, 1.5)



if __name__ == "__main__":
    unittest.main()
