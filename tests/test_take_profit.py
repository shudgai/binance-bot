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
            [0, 100.0, 100.60, 99.8, 100.37, 500],
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

    def test_post_entry_early_exit_triggers_on_wrong_direction(self):
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
                mock_close.assert_called_once()
                self.assertEqual(s.get("wrong_dir_side"), "buy")
                self.assertEqual(s.get("pending_reverse"), "sell")

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


if __name__ == "__main__":
    unittest.main()
