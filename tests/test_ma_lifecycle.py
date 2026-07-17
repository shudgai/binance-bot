import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from core import ctx
from core.check_entries import compute_indicators
from core.exits import _ma_peak_keep_ratio, check_exits, update_ma_peak_lock, update_trailing_stop
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
        ctx.STATES[self.sym]["ma_exit_invalid_count"] = 1

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA7_MA25_Death_Cross]")

        asyncio.run(run())



    def test_ma_peak_lock_uses_tighter_tiers_as_profit_grows(self):
        self.assertEqual(_ma_peak_keep_ratio(0.010), 0.75)
        self.assertEqual(_ma_peak_keep_ratio(0.020), 0.85)
        self.assertEqual(_ma_peak_keep_ratio(0.030), 0.90)

    def test_ma_peak_lock_does_not_arm_below_point_six_percent(self):
        state = self._position_state(closed_price=100.6)
        state["close_price"] = 100.3
        state["highest_profit_pct"] = 0.003

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_not_called()
                self.assertFalse(state["ma_peak_lock_armed"])

        asyncio.run(run())

    def test_ma_profit_floor_arms_at_point_three_percent_without_tight_trailing(self):
        state = self._position_state(closed_price=100.3)
        state.update({"close_price": 100.3, "highest_profit_pct": 0.003})

        hit, floor_price = update_ma_peak_lock(self.sym, 100.3, True)

        self.assertFalse(hit)
        self.assertTrue(state["ma_profit_floor_armed"])
        self.assertFalse(state["ma_peak_lock_armed"])
        self.assertAlmostEqual(floor_price, 100.25)

    def test_ma_profit_floor_exits_before_meaningful_profit_becomes_loss(self):
        state = self._position_state(closed_price=100.24)
        state.update({"close_price": 100.24, "highest_profit_pct": 0.0035})

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA_Profit_Floor]")
                self.assertFalse(close_mock.call_args.kwargs["is_stop_loss"])

        asyncio.run(run())

    def test_ma_short_profit_floor_is_symmetric(self):
        state = self._position_state(closed_price=99.76, ma7=99.0, ma25=100.0)
        state.update({"qty": -1.0, "close_price": 99.76, "highest_profit_pct": 0.0035})

        hit, floor_price = update_ma_peak_lock(self.sym, 99.76, False)

        self.assertTrue(hit)
        self.assertAlmostEqual(floor_price, 99.75)

    def test_ma_short_early_momentum_flip_exits_after_two_confirmations(self):
        state = self._position_state(closed_price=100.3, ma7=100.1, ma25=101.0)
        state.update({
            "qty": -1.0, "close_price": 100.3,
            "open_time": time.time() - 120, "current_rsi": 50.0,
        })

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_not_called()
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA_Early_Momentum_Flip]")

        asyncio.run(run())

    def test_ma_long_early_momentum_flip_is_symmetric(self):
        state = self._position_state(closed_price=99.7, ma7=99.9, ma25=99.0)
        state.update({
            "close_price": 99.7, "open_time": time.time() - 120,
            "current_rsi": 50.0,
        })

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA_Early_Momentum_Flip]")

        asyncio.run(run())

    def test_ma_early_momentum_flip_confirmation_resets_when_structure_recovers(self):
        state = self._position_state(closed_price=100.3, ma7=100.1, ma25=101.0)
        state.update({
            "qty": -1.0, "close_price": 100.3,
            "open_time": time.time() - 120, "current_rsi": 50.0,
        })

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                self.assertEqual(state["ma_momentum_flip_count"], 1)
                state["close_price"] = 100.0
                await check_exits(self.sym)
                self.assertEqual(state["ma_momentum_flip_count"], 0)
                state["close_price"] = 100.3
                await check_exits(self.sym)
                close_mock.assert_not_called()
                self.assertEqual(state["ma_momentum_flip_count"], 1)

        asyncio.run(run())

    def test_ma_early_momentum_flip_does_not_apply_after_window(self):
        state = self._position_state(closed_price=100.3, ma7=100.1, ma25=101.0)
        state.update({
            "qty": -1.0, "close_price": 100.3,
            "open_time": time.time() - 1900, "current_rsi": 50.0,
        })

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                await check_exits(self.sym)
                close_mock.assert_not_called()

        asyncio.run(run())

    def test_ma_active_risk_stop_requires_two_breaches(self):
        state = self._position_state(closed_price=99.4)
        state.update({"close_price": 99.4, "open_time": time.time() - 120, "current_rsi": 55.0})

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_not_called()
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA_Active_Risk_Stop]")

        asyncio.run(run())

    def test_ma_dynamic_tp_does_not_take_sub_point_six_percent_profit(self):
        state = self._position_state(closed_price=100.3)
        state.update({
            "close_price": 100.3,
            "highest_profit_pct": 0.003,
            "max_profit_reached": 0.003,
            "_dyn_tp_base_distance": 0.5,
        })

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_not_called()

        asyncio.run(run())

    def test_ma_common_trailing_does_not_arm_below_point_six_percent(self):
        state = self._position_state(closed_price=100.3)
        state.update({
            "close_price": 100.3,
            "highest_profit_pct": 0.0043,
            "trailing_highest": 100.43,
            "trailing_activation_atr": 0.5,
            "trailing_distance_atr": 1.0,
        })

        update_trailing_stop(self.sym, 100.3, True)

        self.assertEqual(state["trailing_stop_price"], 0.0)
        self.assertFalse(state.get("is_breakeven_locked", False))
        self.assertFalse(state.get("soft_trailing_armed", False))

    def test_ma_common_trailing_removes_stale_profit_side_stop_below_threshold(self):
        state = self._position_state(closed_price=100.24)
        state.update({
            "close_price": 100.24,
            "highest_profit_pct": 0.0043,
            "trailing_highest": 100.43,
            "trailing_stop_price": 100.15,
            "stop_loss": 100.15,
            "is_breakeven_locked": True,
            "soft_trailing_armed": True,
            "soft_trailing_profit_floor": 100.15,
            "hard_stop_loss_pct": 0.02,
        })

        update_trailing_stop(self.sym, 100.24, True)

        self.assertAlmostEqual(state["trailing_stop_price"], 98.0)
        self.assertAlmostEqual(state["stop_loss"], 98.0)
        self.assertFalse(state.get("is_breakeven_locked", False))
        self.assertFalse(state.get("soft_trailing_armed", False))

    def test_ma_long_peak_lock_uses_mid_tier_fifteen_percent_giveback(self):
        state = self._position_state(closed_price=101.5)
        state["close_price"] = 101.5
        state["highest_profit_pct"] = 0.02

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA_Peak_Lock]")
                self.assertAlmostEqual(state["ma_peak_lock_price"], 101.70, places=6)

        asyncio.run(run())

    def test_ma_short_peak_lock_is_symmetric(self):
        state = self._position_state(closed_price=98.6, ma7=99.0, ma25=100.0)
        state.update({
            "qty": -1.0, "close_price": 98.6, "highest_profit_pct": 0.02,
            "prev_ma7": 99.1, "prev_ma25": 100.0,
        })

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA_Peak_Lock]")
                self.assertAlmostEqual(state["ma_peak_lock_price"], 98.30, places=6)

        asyncio.run(run())


    def test_ma_peak_lock_ratchets_up_without_exiting_above_lock(self):
        state = self._position_state(closed_price=101.7)
        state.update({"close_price": 101.7, "highest_profit_pct": 0.02})

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                first_lock = state["ma_peak_lock_price"]
                close_mock.assert_not_called()
                state["close_price"] = 103.0
                await check_exits(self.sym)
                close_mock.assert_not_called()
                self.assertGreater(state["ma_peak_lock_price"], first_lock)
                self.assertAlmostEqual(state["ma_peak_lock_price"], 102.70, places=6)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
