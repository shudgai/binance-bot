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

    async def test_first_breach_does_not_close_immediately(self):
        # 停利線第一次被穿越只累積確認筆數，不等整根 K 棒收線，但也不能單筆
        # 雜訊就出場——見 RANGE_TRAILING_CONFIRM_TICKS/SEC。空單的停利線在
        # avg 下方，「穿越」代表現價回升到 >= 停利線（回吐），不是跌破。
        state = self._short_state()
        state["close_price"] = 99.6  # crossed back above the 99.5 stop
        with patch("core.exits.update_trailing_stop", return_value=(False, 99.5)), \
             patch("core.orders.close_position", AsyncMock()) as close_mock:
            await check_exits(self.sym)

        close_mock.assert_not_awaited()
        self.assertEqual(state["range_trailing_cross_count"], 1)

    async def test_wick_that_recovers_resets_confirmation(self):
        # 盤中曾穿越、確認筆數還在累積中，但下一筆成交流已經回到停利線有利側
        # （現價 < 停利線），應該立刻重置確認，不留殘值誤觸下一次穿越。
        state = self._short_state()
        state.update({
            "close_price": 99.3,  # back on the profitable side of 99.5
            "range_trailing_cross_count": 2,
            "range_trailing_cross_since": time.time() - 0.3,
        })
        with patch("core.exits.update_trailing_stop", return_value=(False, 99.5)), \
             patch("core.orders.close_position", AsyncMock()) as close_mock:
            await check_exits(self.sym)

        close_mock.assert_not_awaited()
        self.assertEqual(state["range_trailing_cross_count"], 0)

    async def test_persistent_breach_across_confirm_window_exits_range_short(self):
        # 已經連續確認 2 筆、且第一筆確認距今已超過確認秒數：這一筆價格仍在
        # 穿越狀態，應該直接確認出場，不必等整根 K 棒收線。
        state = self._short_state()
        state.update({
            "close_price": 99.6,
            "range_trailing_cross_count": 2,
            "range_trailing_cross_since": time.time() - 1.1,
        })
        with patch("core.exits.update_trailing_stop", return_value=(False, 99.5)), \
             patch("core.orders.close_position", AsyncMock()) as close_mock:
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
