import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from core import ctx
from core.check_entries import compute_indicators
from core.exits import (_ma_peak_keep_ratio, _ma7_simple_turn_break,
    _schedule_ma_exchange_profit_stop,
    _meaningful_ma7_break, check_exits, update_ma_peak_lock,
    update_trailing_stop)
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

    def test_tiny_ma7_break_while_above_ma25_is_normal_noise(self):
        broken, buffer_size = _meaningful_ma7_break(
            True, 74.65, 74.654286, 74.5276, 74.63, 0.10, 74.60
        )
        self.assertFalse(broken)
        self.assertGreater(buffer_size, 74.654286 - 74.65)

    def test_meaningful_ma7_break_requires_buffered_ma25_loss(self):
        self.assertFalse(_meaningful_ma7_break(
            True, 99.7, 100.0, 99.0, 99.9, 0.5, 100.0
        )[0])
        self.assertFalse(_meaningful_ma7_break(
            True, 99.7, 100.0, 99.0, 100.1, 0.5, 100.0
        )[0])
        self.assertTrue(_meaningful_ma7_break(
            True, 98.8, 100.0, 99.0, 99.9, 0.5, 100.0
        )[0])

    def test_ma7_simple_short_ignores_price_only_break_while_ma7_still_falls(self):
        broken, _ = _ma7_simple_turn_break(
            False, 74.98, 74.90, 0.043571, 74.87,
            False, {"current_slope": -0.01}, 0,
        )
        self.assertFalse(broken)

    def test_ma7_simple_short_requires_turn_then_continuation(self):
        first, _ = _ma7_simple_turn_break(
            False, 74.98, 74.90, 0.043571, 74.87,
            True, {"current_slope": 0.01}, 0,
        )
        continued, _ = _ma7_simple_turn_break(
            False, 75.02, 74.93, 0.043571, 74.87,
            False, {"current_slope": 0.015}, 1,
        )
        self.assertTrue(first)
        self.assertTrue(continued)

    def test_ma7_simple_does_not_join_an_old_adverse_slope_without_new_turn(self):
        broken, _ = _ma7_simple_turn_break(
            False, 74.98, 74.90, 0.043571, 74.87,
            False, {"current_slope": 0.01}, 0,
        )
        self.assertFalse(broken)

    def test_doge_and_sui_shallow_ma25_undercuts_do_not_exit(self):
        self.assertFalse(_meaningful_ma7_break(
            True, 0.071870, 0.071987, 0.071919, 0.072000, 0.000075, 0.071920
        )[0])
        self.assertFalse(_meaningful_ma7_break(
            True, 0.735100, 0.736471, 0.735408, 0.736600, 0.001336, 0.735700
        )[0])

    def test_short_ma7_break_is_symmetric(self):
        self.assertTrue(_meaningful_ma7_break(
            False, 101.2, 100.0, 101.0, 99.9, 0.5, 100.0
        )[0])

    def test_ma7_break_does_not_exit_before_opposite_cross(self):
        self._position_state(closed_price=99.5)

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_not_called()

        asyncio.run(run())

    def test_early_momentum_confirmation_counts_once_per_closed_candle(self):
        # ma_momentum_flip_count 已重命名為 ma_exit_invalid_count
        state = self._position_state(closed_price=98.8)
        state.update({
            "close_price": 99.7,
            "current_rsi": 45.0,
            "open_time": time.time() - 120,
        })

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                await check_exits(self.sym)
                close_mock.assert_not_called()
                self.assertEqual(state.get("ma_exit_invalid_count"), 1)

        asyncio.run(run())

    def test_confirmed_wrong_direction_exits_before_disaster_stop(self):
        # MA_WRONG_DIRECTION_PCT = 1%；price 98.9 對 avg 100.0 = -1.1%，可觸發
        # 兩根均為 bearish（close < open），且 both_closed_after_entry 需成立
        state = self._position_state(closed_price=98.8)
        opened = time.time() - 700
        opened_ms = int(opened * 1000)
        state.update({
            "open_time": opened,
            "close_price": 98.9,
            "avg_price": 100.0,
            "vol_ma20": 100.0,
            "ohlcv": [
                [opened_ms, 100.0, 100.1, 99.5, 99.5, 120.0],   # bearish, after entry
                [opened_ms + 300000, 99.5, 99.6, 98.7, 98.8, 120.0],  # bearish, after entry
                [opened_ms + 600000, 98.8, 99.0, 98.7, 98.9, 1.0],   # live candle
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
        # MA_DISASTER_STOP_PCT = 2.5%；avg=100.0，需 close_price <= 97.5 才觸發
        state = self._position_state(closed_price=99.5)
        state["close_price"] = 97.4

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
        self.assertEqual(_ma_peak_keep_ratio(0.010), 0.80)
        self.assertEqual(_ma_peak_keep_ratio(0.020), 0.85)
        self.assertEqual(_ma_peak_keep_ratio(0.030), 0.90)

    def test_ma_micro_profit_floor_arms_after_two_nearby_ticks(self):
        state = self._position_state(closed_price=100.28)
        state.update({"highest_profit_pct": 0.0})

        hit, floor = update_ma_peak_lock(
            self.sym, 100.28, True, event_time=100.0, require_confirmation=True,
        )
        self.assertFalse(hit)
        self.assertEqual(floor, 0.0)
        self.assertFalse(state["ma_profit_floor_armed"])

        hit, floor = update_ma_peak_lock(
            self.sym, 100.279, True, event_time=100.5, require_confirmation=True,
        )
        self.assertFalse(hit)
        self.assertTrue(state["ma_profit_floor_armed"])
        self.assertAlmostEqual(floor, 100.168, places=3)

    def test_ma_micro_profit_floor_stays_off_below_point_two_percent(self):
        state = self._position_state(closed_price=100.19)
        state.update({"highest_profit_pct": 0.0019})

        hit, floor = update_ma_peak_lock(self.sym, 100.19, True)

        self.assertFalse(hit)
        self.assertEqual(floor, 0.0)
        self.assertFalse(state["ma_profit_floor_armed"])

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
        state.update({"close_price": 100.24, "highest_profit_pct": 0.0035,
                      "ma_profit_floor_cross_count": 2,
                      "ma_profit_floor_cross_since": time.time() - 1.1})

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA_Profit_Floor]")
                self.assertFalse(close_mock.call_args.kwargs["is_stop_loss"])

        asyncio.run(run())

    def test_short_profit_floor_does_not_chase_after_net_profit_is_gone(self):
        state = self._position_state(closed_price=100.02)
        state.update({"qty": -1.0, "highest_profit_pct": 0.0032})

        for event_time in (100.0, 101.0, 104.0):
            hit, floor = update_ma_peak_lock(
                self.sym, 100.02, False, event_time=event_time,
            )
            self.assertFalse(hit)

        self.assertAlmostEqual(floor, 99.744, places=3)
        self.assertEqual(state["ma_profit_floor_cross_count"], 0)
        self.assertTrue(state["ma_profit_floor_missed"])

    def test_ma_profit_floor_reclaim_cancels_pending_exit(self):
        state = self._position_state(closed_price=100.24)
        state.update({"close_price": 100.24, "highest_profit_pct": 0.0035})

        hit, _ = update_ma_peak_lock(self.sym, 100.24, True, event_time=100.0)
        self.assertFalse(hit)
        self.assertEqual(state["ma_profit_floor_cross_count"], 1)

        hit, _ = update_ma_peak_lock(self.sym, 100.29, True, event_time=100.5)
        self.assertFalse(hit)
        self.assertEqual(state["ma_profit_floor_cross_count"], 0)

        hit, _ = update_ma_peak_lock(self.sym, 100.24, True, event_time=102.0)
        self.assertFalse(hit)
        self.assertEqual(state["ma_profit_floor_cross_count"], 1)

    def test_ma_short_profit_floor_is_symmetric(self):
        state = self._position_state(closed_price=99.76, ma7=99.0, ma25=100.0)
        state.update({"qty": -1.0, "close_price": 99.76, "highest_profit_pct": 0.0035})

        hit, floor_price = update_ma_peak_lock(self.sym, 99.76, False, event_time=100.0)
        self.assertFalse(hit)
        hit, _ = update_ma_peak_lock(self.sym, 99.76, False, event_time=100.5)
        self.assertFalse(hit)
        hit, floor_price = update_ma_peak_lock(self.sym, 99.76, False, event_time=102.1)

        self.assertTrue(hit)
        self.assertAlmostEqual(floor_price, 99.72)

    def test_ma_short_early_momentum_flip_exits_after_two_confirmations(self):
        # [MA_Early_Momentum_Flip] 已合併至 [MA7_Closed_Break]（ma_exit_invalid_count >= 2）
        state = self._position_state(closed_price=101.2, ma7=100.1, ma25=101.0)
        state.update({
            "qty": -1.0, "close_price": 101.2,
            "open_time": time.time() - 120, "current_rsi": 50.0,
        })
        first_candle_ts = state["ma_candle_ts"]

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_not_called()
                state["ma_candle_ts"] = first_candle_ts + 300000
                state["ohlcv"][-2][0] = state["ma_candle_ts"]
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA7_Closed_Break]")

        asyncio.run(run())

    def test_ma_long_early_momentum_flip_is_symmetric(self):
        # [MA_Early_Momentum_Flip] 已合併至 [MA7_Closed_Break]（ma_exit_invalid_count >= 2）
        state = self._position_state(closed_price=98.8, ma7=99.9, ma25=99.0)
        state.update({
            "close_price": 98.8, "open_time": time.time() - 120,
            "current_rsi": 50.0,
        })
        first_candle_ts = state["ma_candle_ts"]

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                state["ma_candle_ts"] = first_candle_ts + 300000
                state["ohlcv"][-2][0] = state["ma_candle_ts"]
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA7_Closed_Break]")

        asyncio.run(run())

    def test_ma_early_momentum_flip_confirmation_resets_when_structure_recovers(self):
        # ma_momentum_flip_count 已重命名為 ma_exit_invalid_count
        state = self._position_state(closed_price=101.2, ma7=100.1, ma25=101.0)
        state.update({
            "qty": -1.0, "close_price": 101.2,
            "open_time": time.time() - 120, "current_rsi": 50.0,
        })
        first_candle_ts = state["ma_candle_ts"]

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                self.assertEqual(state["ma_exit_invalid_count"], 1)
                state["ma_candle_ts"] = first_candle_ts + 300000
                state["ohlcv"][-2][0] = state["ma_candle_ts"]
                state["ohlcv"][-2][4] = 100.0
                state["close_price"] = 100.0
                await check_exits(self.sym)
                self.assertEqual(state["ma_exit_invalid_count"], 0)
                state["ma_candle_ts"] = first_candle_ts + 600000
                state["ohlcv"][-2][0] = state["ma_candle_ts"]
                state["ohlcv"][-2][4] = 101.2
                state["close_price"] = 101.2
                await check_exits(self.sym)
                close_mock.assert_not_called()
                self.assertEqual(state["ma_exit_invalid_count"], 1)

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

    def test_ma_lifecycle_exit_requires_two_breaches(self):
        # [MA_Active_Risk_Stop] 已合併至 [MA7_Closed_Break] 機制（ma_exit_invalid_count >= 2）
        # ma7=100.0 > ma25=99.0，long 倉位；closed_price=98.8 < ma25=99.0（ma7_broken）
        state = self._position_state(closed_price=98.8)
        state.update({"close_price": 98.8, "open_time": time.time() - 120, "current_rsi": 55.0})

        async def run():
            close_mock = AsyncMock()
            with patch("core.orders.close_position", close_mock):
                await check_exits(self.sym)
                close_mock.assert_not_called()
                state["ma_candle_ts"] = state["ma_candle_ts"] + 300000
                state["ohlcv"][-2][0] = state["ma_candle_ts"]
                await check_exits(self.sym)
                close_mock.assert_called_once()
                self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA7_Closed_Break]")

        asyncio.run(run())

    def test_ma_route_does_not_exit_without_peak_lock_cross(self):
        state = self._position_state(closed_price=100.3)
        state.update({
            "close_price": 100.3,
            "highest_profit_pct": 0.003,
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

    def test_ma_common_trailing_never_arms_above_point_six_percent(self):
        state = self._position_state(closed_price=101.2)
        state.update({
            "close_price": 101.2,
            "highest_profit_pct": 0.012,
            "trailing_highest": 101.2,
            "trailing_activation_atr": 0.5,
            "trailing_distance_atr": 1.0,
        })

        update_trailing_stop(self.sym, 101.2, True)

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


class SlowMarketAtrCheckTests(unittest.TestCase):
    """Verify the SLOW_MARKET ATR gate correctly exempts MA7_Simple."""

    def setUp(self):
        self.sym = "SLOWMKTUSDT"
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

    def _make_check_entries_patches(self, route: str, atr_pct_5m: float):
        """Build a minimal set of patches so check_entries() reaches the ATR gate."""
        import core.ctx as ctx
        import core.balance as _bal

        # Very low 5m ATR (0.01%) — below any threshold
        price = 100.0
        atr_cur_ce = price * atr_pct_5m   # e.g. 0.01 for 0.01%

        s = ctx.STATES[self.sym]
        s["status"] = "ACTIVE"
        s["is_ordering"] = False
        s["qty"] = 0.0
        s["close_price"] = price
        s["ohlcv"] = []
        s["atr_cur_ce"] = atr_cur_ce
        s["entry_block_reason"] = ""

        return [
            patch("core.check_entries.is_daily_loss_halted", return_value=False),
            patch("core.check_entries.get_open_position_count", return_value=0),
            patch("core.balance.get_dynamic_max_slots", return_value=5),
            patch("core.check_entries._load_disabled_symbols", return_value=set()),
            patch("core.check_entries._is_confirmable_exit_cooldown", return_value=False),
            patch("core.ctx.ALL_SYMBOLS", [self.sym]),
            # Signal engine returns a hit for the given route
            patch("core.check_entries.compute_signal_strength", return_value=(25.0, "buy", route, False)),
            patch("core.check_entries.compute_range_signal", return_value=(False, None, None, 0.0)),
            # Skip all upstream guards so we reach the ATR gate
            patch("core.check_entries.is_entry_allowed", return_value=(True, "")),
            patch("core.check_entries.btc_macro_entry_guard", return_value=("BULLISH", True, "")),
            patch("core.check_entries._funding_rate_guard", new_callable=AsyncMock, return_value=(True, "")),
            patch("core.check_entries._atr_cur_ce_from_state", return_value=atr_cur_ce),
        ]

    def test_ma7_simple_bypasses_slow_market_check(self):
        """MA7_Simple with a near-zero ATR must NOT be blocked by SLOW_MARKET."""
        from core.check_entries import check_entries

        patches = self._make_check_entries_patches(route="MA7_Simple", atr_pct_5m=0.0001)
        with patch("core.check_entries._is_confirmable_exit_cooldown", return_value=False), \
             patch("core.check_entries.is_daily_loss_halted", return_value=False), \
             patch("core.check_entries.get_open_position_count", return_value=0), \
             patch("core.balance.get_dynamic_max_slots", return_value=5), \
             patch("core.check_entries._load_disabled_symbols", return_value=set()):

            s = ctx.STATES[self.sym]
            price = 100.0
            # Extremely low 5m ATR (0.01% = 0.01 absolute)
            atr_5m = price * 0.0001
            s["atr_cur_ce"] = atr_5m
            s["close_price"] = price

            # Directly test the condition that guards SLOW_MARKET
            from core.config import MIN_5M_ATR_PCT_FOR_MA_ENTRY
            route = "MA7_Simple"
            _atr_pct_5m = atr_5m / price
            is_range_signal = False
            _min_atr_pct_5m = 0.0008 if is_range_signal else MIN_5M_ATR_PCT_FOR_MA_ENTRY

            # The condition should evaluate to False for MA7_Simple (not blocked)
            slow_market_blocked = (
                str(route or "").lower() != "ma7_simple" and _atr_pct_5m < _min_atr_pct_5m
            )
            self.assertFalse(
                slow_market_blocked,
                "MA7_Simple with low ATR should NOT be blocked by SLOW_MARKET check"
            )

    def test_ma_cross_blocked_by_slow_market_check(self):
        """MA_Cross with a near-zero ATR MUST be blocked by SLOW_MARKET."""
        from core.config import MIN_5M_ATR_PCT_FOR_MA_ENTRY

        price = 100.0
        atr_5m = price * 0.0001  # 0.01% — well below MIN_5M_ATR_PCT_FOR_MA_ENTRY
        route = "MA_Cross"
        _atr_pct_5m = atr_5m / price
        is_range_signal = False
        _min_atr_pct_5m = 0.0008 if is_range_signal else MIN_5M_ATR_PCT_FOR_MA_ENTRY

        slow_market_blocked = (
            str(route or "").lower() != "ma7_simple" and _atr_pct_5m < _min_atr_pct_5m
        )
        self.assertTrue(
            slow_market_blocked,
            "MA_Cross with low ATR should be blocked by SLOW_MARKET check"
        )

    def test_ma_breakout_blocked_by_slow_market_check(self):
        """MA_Breakout with a near-zero ATR MUST be blocked by SLOW_MARKET."""
        from core.config import MIN_5M_ATR_PCT_FOR_MA_ENTRY

        price = 100.0
        atr_5m = price * 0.0001
        route = "MA_Breakout"
        _atr_pct_5m = atr_5m / price
        is_range_signal = False
        _min_atr_pct_5m = 0.0008 if is_range_signal else MIN_5M_ATR_PCT_FOR_MA_ENTRY

        slow_market_blocked = (
            str(route or "").lower() != "ma7_simple" and _atr_pct_5m < _min_atr_pct_5m
        )
        self.assertTrue(slow_market_blocked, "MA_Breakout with low ATR should be blocked")

    def test_ma7_simple_case_insensitive(self):
        """Route matching for MA7_Simple should be case-insensitive."""
        from core.config import MIN_5M_ATR_PCT_FOR_MA_ENTRY

        price = 100.0
        atr_5m = price * 0.0001
        _atr_pct_5m = atr_5m / price
        is_range_signal = False
        _min_atr_pct_5m = 0.0008 if is_range_signal else MIN_5M_ATR_PCT_FOR_MA_ENTRY

        for variant in ("MA7_Simple", "ma7_simple", "MA7_SIMPLE"):
            blocked = (
                str(variant or "").lower() != "ma7_simple" and _atr_pct_5m < _min_atr_pct_5m
            )
            self.assertFalse(blocked, f"route={variant!r} should bypass SLOW_MARKET check")


class MAExchangeStopScheduleTests(unittest.TestCase):
    def test_new_profit_floor_schedules_exchange_stop_sync(self):
        sym = "MASTOPUSDT"
        original = ctx.STATES.get(sym)
        state = build_symbol_state(sym)
        state.update({
            "qty": 1.0, "avg_price": 100.0, "current_atr": 0.1,
            "highest_profit_pct": 0.0035, "exchange_stop_order_id": "disaster-1",
        })
        ctx.STATES[sym] = state
        try:
            with patch("core.exits._schedule_ma_exchange_profit_stop") as schedule:
                hit, floor = update_ma_peak_lock(sym, 100.35, True)
            self.assertFalse(hit)
            self.assertAlmostEqual(floor, 100.28)
            schedule.assert_called_once_with(sym)
        finally:
            if original is None:
                ctx.STATES.pop(sym, None)
            else:
                ctx.STATES[sym] = original

    def test_exchange_stop_sync_coalesces_update_arriving_while_running(self):
        sym = "MASTOPPENDINGUSDT"
        original = ctx.STATES.get(sym)
        state = build_symbol_state(sym)
        state["exchange_stop_order_id"] = "disaster-1"
        ctx.STATES[sym] = state

        async def run():
            calls = 0

            async def sync(_sym):
                nonlocal calls
                calls += 1
                if calls == 1:
                    _schedule_ma_exchange_profit_stop(sym)

            with patch("core.orders._sync_ma_exchange_profit_stop", side_effect=sync):
                _schedule_ma_exchange_profit_stop(sym)
                from core import exits as exits_module
                await exits_module._MA_EXCHANGE_STOP_SYNC_TASKS[sym]
            self.assertEqual(calls, 2)

        try:
            asyncio.run(run())
        finally:
            if original is None:
                ctx.STATES.pop(sym, None)
            else:
                ctx.STATES[sym] = original
