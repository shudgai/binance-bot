import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from core import ctx
from core.check_entries import compute_indicators
from core.exits import check_exits
from core.state_manager import build_symbol_state


class MALifecycleTests(unittest.TestCase):
    def setUp(self):
        self.sym = "MATESTUSDT"
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

    def test_ma_values_use_closed_candles_only(self):
        closes = [float(i) for i in range(1, 102)]
        candles = [
            [i * 300000, close, close + 0.5, close - 0.5, close, 1000.0]
            for i, close in enumerate(closes)
        ]
        # The live candle is intentionally extreme and must not enter any MA.
        candles[-1][4] = 10000.0
        ctx.STATES[self.sym]["ohlcv"] = candles

        compute_indicators(self.sym)
        state = ctx.STATES[self.sym]
        completed = np.array(closes[:-1])

        self.assertAlmostEqual(state["ma7"], float(np.mean(completed[-7:])))
        self.assertAlmostEqual(state["ma25"], float(np.mean(completed[-25:])))
        self.assertAlmostEqual(state["ma99"], float(np.mean(completed[-99:])))
        self.assertAlmostEqual(state["prev_ma7"], float(np.mean(completed[-8:-1])))
        self.assertEqual(state["ma_candle_ts"], candles[-2][0])

    def _position_state(self, closed_price, ma7=100.0, ma25=99.0, candle_ts=1):
        state = ctx.STATES[self.sym]
        state.update({
            "qty": 1.0,
            "avg_price": 100.0,
            "close_price": 100.0,
            "current_atr": 0.5,
            "current_vol": 100.0,
            "vol_ma20": 100.0,
            "open_time": time.time(),
            "entry_reason": "MA_Cross",
            "ma7": ma7,
            "ma25": ma25,
            "prev_ma7": ma7 + 0.1,
            "prev_ma25": ma25,
            "ma_candle_ts": candle_ts,
            "ohlcv": [
                [0, 100.0, 100.2, 99.8, 100.0, 100.0],
                [candle_ts, 100.0, 100.2, closed_price, closed_price, 100.0],
                [candle_ts + 1, 100.0, 100.2, 99.8, 100.0, 1.0],
            ],
        })
        return state

    def test_ma7_break_does_not_exit_before_opposite_cross(self):
        self._position_state(closed_price=99.5)

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_not_called()

        asyncio.run(run())

    def test_confirmed_wrong_direction_exits_before_disaster_stop(self):
        state = self._position_state(closed_price=99.2)
        opened = time.time() - 700
        opened_ms = int(opened * 1000)
        state.update({
            "open_time": opened,
            "close_price": 99.3,
            "ohlcv": [
                [opened_ms, 100.0, 100.1, 99.6, 99.7, 100.0],
                [opened_ms + 300000, 99.7, 99.8, 99.1, 99.2, 100.0],
                [opened_ms + 600000, 99.2, 99.4, 99.1, 99.3, 1.0],
            ],
        })

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA_Wrong_Direction_Confirmed]")

        asyncio.run(run())

    def test_single_reverse_candle_does_not_confirm_wrong_direction(self):
        state = self._position_state(closed_price=99.2)
        opened = time.time() - 700
        opened_ms = int(opened * 1000)
        state.update({
            "open_time": opened,
            "close_price": 99.3,
            "ohlcv": [
                [opened_ms, 99.5, 100.1, 99.4, 99.8, 100.0],
                [opened_ms + 300000, 99.8, 99.9, 99.1, 99.2, 100.0],
                [opened_ms + 600000, 99.2, 99.4, 99.1, 99.3, 1.0],
            ],
        })

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_not_called()

        asyncio.run(run())

    def test_disaster_stop_remains_as_last_resort(self):
        state = self._position_state(closed_price=99.5)
        state["close_price"] = 98.4

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA_Disaster_Stop]")

        asyncio.run(run())

    def test_death_cross_exits_long_even_before_price_breaks_ma7(self):
        self._position_state(closed_price=99.5, ma7=98.9, ma25=99.0)

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA7_MA25_Death_Cross]")

        asyncio.run(run())



if __name__ == "__main__":
    unittest.main()
