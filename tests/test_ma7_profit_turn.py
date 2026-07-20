import time
import unittest
from unittest.mock import AsyncMock, patch

from core import ctx
from core.exits import _ma7_closed_turn, check_exits
from core.state_manager import build_symbol_state


def _candle(ts, open_price, close_price):
    return [
        ts,
        open_price,
        max(open_price, close_price) + 0.1,
        min(open_price, close_price) - 0.1,
        close_price,
        100.0,
    ]


class MA7ProfitTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sym = "MA7TURNTESTUSDT"
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

    def _long_turn_candles(self):
        completed = [
            _candle(index * 1000, close, close)
            for index, close in enumerate([100.0] * 7 + [102.0, 99.5])
        ]
        completed[-1] = _candle(8000, 101.0, 99.5)
        return completed + [_candle(9000, 99.5, 99.0)]

    def test_long_and_short_turns_use_completed_candles(self):
        long_triggered, long_data = _ma7_closed_turn(self._long_turn_candles(), True)
        self.assertTrue(long_triggered)
        self.assertGreater(long_data["previous_slope"], 0)
        self.assertLess(long_data["current_slope"], 0)
        self.assertEqual(long_data["candle_ts"], 8000)

        short_completed = [
            _candle(index * 1000, close, close)
            for index, close in enumerate([100.0] * 7 + [98.0, 100.5])
        ]
        short_completed[-1] = _candle(8000, 99.0, 100.5)
        short_triggered, short_data = _ma7_closed_turn(
            short_completed + [_candle(9000, 100.5, 101.0)], False
        )
        self.assertTrue(short_triggered)
        self.assertLess(short_data["previous_slope"], 0)
        self.assertGreater(short_data["current_slope"], 0)

    async def test_profitable_long_partials_then_exits_on_next_falling_ma7(self):
        state = ctx.STATES[self.sym]
        state.update({
            "qty": 10.0,
            "avg_price": 98.5,
            "close_price": 99.5,
            "current_atr": 0.5,
            "current_vol": 100.0,
            "vol_ma20": 100.0,
            "open_time": time.time() - 600,
            "entry_reason": "MA7_Simple",
            "ma7": 100.0,
            "ma25": 99.0,
            "prev_ma7": 100.1,
            "prev_ma25": 99.0,
            "ma_candle_ts": 8000,
            "ohlcv": self._long_turn_candles(),
        })

        async def partial_fill(*args, **kwargs):
            state["qty"] = 4.0

        with patch("core.orders.close_position", AsyncMock(side_effect=partial_fill)) as close_mock:
            await check_exits(self.sym)
            close_mock.assert_awaited_once()
            self.assertAlmostEqual(close_mock.call_args.args[2], 6.0)
            self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA7_Profit_Turn_Partial]")
            self.assertEqual(state["ma7_profit_turn_stage"], 1)
            self.assertEqual(state["ma7_profit_turn_signal_ts"], 8000)

        state["adjusted_this_tick"] = False
        state["close_price"] = 99.0
        state["ma_candle_ts"] = 9000
        state["ohlcv"].append(_candle(10000, 99.0, 98.9))
        with patch("core.orders.close_position", AsyncMock()) as close_mock:
            await check_exits(self.sym)
            close_mock.assert_awaited_once()
            self.assertAlmostEqual(close_mock.call_args.args[2], 4.0)

    async def test_giveback_past_half_of_peak_exits_immediately_without_waiting_for_turn(self):
        # 實測 BCHUSDT 案例：峰值墊到 0.44%，但 MA7_Profit_Turn_Partial 完全不
        # 參考峰值，每次都貼著成本價出場。這裡驗證浮盈已經回吐超過峰值一半時，
        # 不必等 MA7 收線轉彎確認，直接出清剩餘部位。
        state = ctx.STATES[self.sym]
        state.update({
            "qty": 10.0,
            "avg_price": 100.0,
            "close_price": 100.2,  # profit 0.2%, below half of the 0.6% peak
            "current_atr": 0.5,
            "current_vol": 100.0,
            "vol_ma20": 100.0,
            "open_time": time.time() - 600,
            "entry_reason": "MA7_Simple",
            "highest_profit_pct": 0.006,
            "ma7": 100.0,
            "ma25": 99.0,
            "prev_ma7": 100.1,
            "prev_ma25": 99.0,
            "ma_candle_ts": 8000,
            "ohlcv": self._long_turn_candles(),
        })

        with patch("core.orders.close_position", AsyncMock()) as close_mock:
            await check_exits(self.sym)

        close_mock.assert_awaited_once()
        self.assertAlmostEqual(close_mock.call_args.args[2], 10.0)
        self.assertEqual(close_mock.call_args.kwargs["reason"], "[MA7_Profit_Turn_Giveback]")

    async def test_giveback_within_half_of_peak_does_not_trigger_safety_net(self):
        state = ctx.STATES[self.sym]
        state.update({
            "qty": 10.0,
            "avg_price": 100.0,
            "close_price": 100.5,  # profit 0.5%, still above half of the 0.6% peak
            "current_atr": 0.5,
            "current_vol": 100.0,
            "vol_ma20": 100.0,
            "open_time": time.time() - 600,
            "entry_reason": "MA7_Simple",
            "highest_profit_pct": 0.006,
            "ma7": 100.0,
            "ma25": 99.0,
            "prev_ma7": 99.9,
            "prev_ma25": 99.0,
            "ma_candle_ts": 8000,
            "ohlcv": self._long_turn_candles(),
        })

        with patch("core.orders.close_position", AsyncMock()) as close_mock:
            await check_exits(self.sym)

        for call in close_mock.await_args_list:
            self.assertNotEqual(call.kwargs.get("reason"), "[MA7_Profit_Turn_Giveback]")
