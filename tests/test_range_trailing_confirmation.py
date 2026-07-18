import time
import unittest
from unittest.mock import AsyncMock, patch

from core import ctx
from core.exits import check_exits
from core.state_manager import build_symbol_state


def _candle(ts, open_price, high, low, close):
    return [ts, open_price, high, low, close, 100.0]


class RangeTrailingConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sym = "RANGETRAILTESTUSDT"
        self.original_state = ctx.STATES.get(self.sym)
        self.original_symbols = list(ctx.ALL_SYMBOLS)
        ctx.STATES[self.sym] = build_symbol_state(self.sym)
        if self.sym not in ctx.ALL_SYMBOLS:
            ctx.ALL_SYMBOLS.append(self.sym)

    def tearDown(self):
        ctx.ALL_SYMBOLS[:] = self.original_symbols
        if self.original_state is None:
            ctx.STATES.pop(self.sym, None)
        else:
            ctx.STATES[self.sym] = self.original_state

    def _short_state(self):
        state = ctx.STATES[self.sym]
        state.update({
            "qty": -1.0,
            "avg_price": 100.0,
            "close_price": 100.2,
            "current_atr": 1.0,
            "entry_atr": 1.0,
            "open_time": time.time() - 600,
            "entry_reason": "Range_Resistance_Short",
            "range_sl_price": 102.0,
            "trailing_stop_price": 99.5,
            "stop_loss": 99.5,
            "highest_profit_pct": 0.005,
            "trailing_lowest": 98.0,
            "current_vol": 100.0,
            "vol_ma20": 100.0,
            "ohlcv": [
                _candle(0, 100.0, 100.0, 99.8, 99.9),
                _candle(60000, 99.0, 100.2, 98.0, 100.2),
            ],
        })
        return state

    async def test_intracandle_range_trailing_breach_is_deferred(self):
        state = self._short_state()
        with patch("core.exits.update_trailing_stop", return_value=(False, 99.5)), \
             patch("core.orders.close_position", AsyncMock()) as close_mock:
            await check_exits(self.sym)

        close_mock.assert_not_awaited()
        self.assertTrue(state["range_trailing_pending"])
        self.assertEqual(state["range_trailing_pending_candle_ts"], 60000)
        self.assertEqual(state["range_trailing_pending_stop"], 99.5)

    async def test_upper_wick_that_closes_back_below_stop_keeps_short_open(self):
        state = self._short_state()
        state.update({
            "close_price": 99.2,
            "range_trailing_pending": True,
            "range_trailing_pending_candle_ts": 60000,
            "range_trailing_pending_stop": 99.5,
            "ohlcv": [
                _candle(0, 100.0, 100.0, 99.8, 99.9),
                # ZEC 型態：盤中上刺停損，但收盤重新回到空單有利側。
                _candle(60000, 99.0, 100.2, 98.0, 99.2),
                _candle(120000, 99.2, 99.3, 99.1, 99.2),
            ],
        })
        with patch("core.exits.update_trailing_stop", return_value=(False, 99.5)), \
             patch("core.orders.close_position", AsyncMock()) as close_mock:
            await check_exits(self.sym)

        close_mock.assert_not_awaited()
        self.assertFalse(state["range_trailing_pending"])

    async def test_completed_close_beyond_stop_exits_range_short(self):
        state = self._short_state()
        state.update({
            "range_trailing_pending": True,
            "range_trailing_pending_candle_ts": 60000,
            "range_trailing_pending_stop": 99.5,
            "ohlcv": [
                _candle(0, 100.0, 100.0, 99.8, 99.9),
                _candle(60000, 99.0, 100.2, 98.0, 99.7),
                _candle(120000, 99.7, 100.0, 99.6, 99.8),
            ],
        })
        with patch("core.orders.close_position", AsyncMock()) as close_mock:
            await check_exits(self.sym)

        close_mock.assert_awaited_once()
        self.assertEqual(
            close_mock.call_args.kwargs["reason"],
            "[Range_Trailing_Closed_Confirm]",
        )

    async def test_range_structural_stop_remains_immediate(self):
        state = self._short_state()
        state["close_price"] = 102.1
        with patch("core.orders.close_position", AsyncMock()) as close_mock:
            await check_exits(self.sym)

        close_mock.assert_awaited_once()
        self.assertEqual(close_mock.call_args.kwargs["reason"], "[Range_SL]")
        self.assertTrue(close_mock.call_args.kwargs["is_stop_loss"])
