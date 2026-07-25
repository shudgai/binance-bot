import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from core import ctx
from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.exits import update_trailing_stop, check_exits
from core.config import (
    EXIT_SL_ATR_MULTIPLIER, EXIT_TP_ATR_MULTIPLIER, EXIT_BREAKEVEN_ATR_MULTIPLIER,
    EXIT_TRAIL_LOCK_RATIO, EXIT_TP_EXTEND_ATR_MULTIPLIER, EXIT_MAX_HOLD_SEC,
)


def _flat_ohlcv(n, price=100.0, candle_range=1.0, start=0):
    """n 根範圍固定 candle_range 的平盤 K 棒（open=close=price），讓 ATR(period)
    可以精確預測（每根 True Range 都剛好等於 candle_range）。"""
    half = candle_range / 2.0
    return [
        [start + i, price, price + half, price - half, price, 1000.0]
        for i in range(n)
    ]


class ExitSystemTests(unittest.TestCase):
    """[2026-07-25] 方案三（動態追蹤止利／趨勢獵人模式）：固定 ATR 停損/停利 +
    保本鎖定 + 峰值追蹤延展 + 防插針保護。取代整套舊版移動停損/MA_Peak_Lock/
    Range trailing/DynamicExitManager/Stagnation_Timeout 測試。"""

    def _setup(self, sym, direction="long", avg=100.0, candle_range=1.0, n=15):
        init_states([sym])
        reset_coin_state(sym)
        s = STATES[sym]
        s.update({
            "status": "ACTIVE",
            "ohlcv": _flat_ohlcv(n, price=avg, candle_range=candle_range) + [[n, avg, avg, avg, avg, 1.0]],
            "avg_price": avg,
            "qty": 1.0 if direction == "long" else -1.0,
            "close_price": avg,
            "open_time": time.time(),
        })
        return s

    # ── 開倉初始化 ──────────────────────────────────────────────────────────
    def test_entry_initializes_fixed_sl_tp_long(self):
        s = self._setup("EXITL1USDT", "long", avg=100.0, candle_range=1.0)
        update_trailing_stop("EXITL1USDT", 100.0, True)
        self.assertAlmostEqual(s["sl_price"], 100.0 - EXIT_SL_ATR_MULTIPLIER * 1.0)
        self.assertAlmostEqual(s["tp_price"], 100.0 + EXIT_TP_ATR_MULTIPLIER * 1.0)
        self.assertEqual(s["highest_price"], 100.0)
        self.assertEqual(s["lowest_price"], 100.0)
        self.assertFalse(s["is_breakeven_moved"])

    def test_entry_initializes_fixed_sl_tp_short(self):
        s = self._setup("EXITS1USDT", "short", avg=100.0, candle_range=1.0)
        update_trailing_stop("EXITS1USDT", 100.0, False)
        self.assertAlmostEqual(s["sl_price"], 100.0 + EXIT_SL_ATR_MULTIPLIER * 1.0)
        self.assertAlmostEqual(s["tp_price"], 100.0 - EXIT_TP_ATR_MULTIPLIER * 1.0)

    # ── 保本鎖定 ────────────────────────────────────────────────────────────
    def test_breakeven_not_triggered_before_threshold(self):
        sym = "EXITL2USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)  # init
        update_trailing_stop(sym, 100.0 + EXIT_BREAKEVEN_ATR_MULTIPLIER * 0.5, True)
        self.assertFalse(s["is_breakeven_moved"])
        self.assertAlmostEqual(s["sl_price"], 100.0 - EXIT_SL_ATR_MULTIPLIER * 1.0)

    def test_breakeven_triggers_at_threshold_long(self):
        # [2026-07-25] 修正 XMRUSDT 實單案例：觸發保本的這個價位本身就是目前峰值，
        # 同一次呼叫必須立刻用它計算 75% 鎖利，不能只鎖平的保本價（否則價格一觸發
        # 保本就馬上回落，整段已經到手的漲幅完全沒鎖到，出場價幾乎等於保本/沒獲利）。
        sym = "EXITL3USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)  # init
        peak = 100.0 + EXIT_BREAKEVEN_ATR_MULTIPLIER * 1.001
        update_trailing_stop(sym, peak, True)
        self.assertTrue(s["is_breakeven_moved"])
        expected_sl = 100.0 + (peak - 100.0) * EXIT_TRAIL_LOCK_RATIO
        self.assertAlmostEqual(s["sl_price"], expected_sl)
        self.assertGreater(s["sl_price"], 100.0)  # 鎖到的一定比純保本多

    def test_breakeven_triggers_at_threshold_short(self):
        sym = "EXITS3USDT"
        s = self._setup(sym, "short", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, False)  # init
        trough = 100.0 - EXIT_BREAKEVEN_ATR_MULTIPLIER * 1.001
        update_trailing_stop(sym, trough, False)
        self.assertTrue(s["is_breakeven_moved"])
        expected_sl = 100.0 - (100.0 - trough) * EXIT_TRAIL_LOCK_RATIO
        self.assertAlmostEqual(s["sl_price"], expected_sl)
        self.assertLess(s["sl_price"], 100.0)

    # ── 峰值追蹤延展 ────────────────────────────────────────────────────────
    def test_trailing_locks_seventy_five_pct_and_extends_tp_long(self):
        sym = "EXITL4USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)  # init
        update_trailing_stop(sym, 101.0, True)  # breakeven armed (>= 0.8)
        update_trailing_stop(sym, 105.0, True)  # new high, 75% lock
        self.assertAlmostEqual(s["sl_price"], 100.0 + (105.0 - 100.0) * EXIT_TRAIL_LOCK_RATIO)
        self.assertAlmostEqual(s["tp_price"], 105.0 + EXIT_TP_EXTEND_ATR_MULTIPLIER * 1.0)

    def test_trailing_locks_seventy_five_pct_and_extends_tp_short(self):
        sym = "EXITS4USDT"
        s = self._setup(sym, "short", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, False)  # init
        update_trailing_stop(sym, 99.0, False)   # breakeven armed
        update_trailing_stop(sym, 95.0, False)   # new low, 75% lock
        self.assertAlmostEqual(s["sl_price"], 100.0 - (100.0 - 95.0) * EXIT_TRAIL_LOCK_RATIO)
        self.assertAlmostEqual(s["tp_price"], 95.0 - EXIT_TP_EXTEND_ATR_MULTIPLIER * 1.0)

    def test_sl_never_loosens_on_pullback(self):
        sym = "EXITL5USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)
        update_trailing_stop(sym, 101.0, True)
        update_trailing_stop(sym, 105.0, True)
        locked_sl = s["sl_price"]
        update_trailing_stop(sym, 102.0, True)  # pulls back, still above sl
        self.assertEqual(s["sl_price"], locked_sl)

    def test_tp_never_retracts_on_pullback(self):
        sym = "EXITL6USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)
        update_trailing_stop(sym, 101.0, True)
        update_trailing_stop(sym, 105.0, True)
        extended_tp = s["tp_price"]
        update_trailing_stop(sym, 102.0, True)
        self.assertEqual(s["tp_price"], extended_tp)

    # ── check_exits()：停利／停損／防插針／時間強制出場 ───────────────────────
    def test_take_profit_closes_position(self):
        # check_exits() 用防插針確認價 (close_price_spike_filtered) 跑
        # update_trailing_stop 峰值追蹤，但用即時 close_price 比對停利，兩者故意
        # 分開：如果都用同一個價格，只要現價一到 tp_price，peak-tracking 會在
        # 同一次呼叫先把 tp_price 往外延展，導致 tp_price 永遠追不上、TP 打不到。
        # 這裡把確認價留在原地（不觸發保本/延展），只讓即時價格衝上原始 tp_price。
        sym = "EXITTP1USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)
        s["close_price_spike_filtered"] = 100.5  # 遠低於保本門檻(100.8)，peak不延展
        s["close_price"] = s["tp_price"]

        async def run():
            mock_close = AsyncMock(return_value=None)
            with patch("core.orders.close_position", mock_close):
                await check_exits(sym)
            mock_close.assert_called_once()
            self.assertEqual(mock_close.call_args.kwargs.get("reason"), "[Take_Profit]")
            self.assertFalse(mock_close.call_args.kwargs.get("is_stop_loss"))

        asyncio.run(run())

    def test_stop_loss_closes_position_without_spike(self):
        sym = "EXITSL1USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)
        s["close_price"] = s["sl_price"]

        async def run():
            mock_close = AsyncMock(return_value=None)
            with patch("core.orders.close_position", mock_close):
                await check_exits(sym)
            mock_close.assert_called_once()
            self.assertEqual(mock_close.call_args.kwargs.get("reason"), "[Stop_Loss]")
            self.assertTrue(mock_close.call_args.kwargs.get("is_stop_loss"))

        asyncio.run(run())

    def test_breakeven_stop_uses_breakeven_reason_tag(self):
        sym = "EXITBE1USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)
        update_trailing_stop(sym, 101.0, True)  # breakeven armed, sl_price == avg
        s["close_price"] = s["sl_price"]

        async def run():
            mock_close = AsyncMock(return_value=None)
            with patch("core.orders.close_position", mock_close):
                await check_exits(sym)
            mock_close.assert_called_once()
            self.assertEqual(mock_close.call_args.kwargs.get("reason"), "[Breakeven_Stop]")

        asyncio.run(run())

    def test_spike_candle_suspends_stop_loss_trigger(self):
        sym = "EXITSPIKE1USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0, n=15)
        update_trailing_stop(sym, 100.0, True)
        # 在最新一根「已收盤」K棒（ohlcv[-2]）製造一根振幅遠超 5xATR 的插針。
        s["ohlcv"][-2] = [len(s["ohlcv"]) - 2, 100.0, 130.0, 100.0, 100.0, 1000.0]
        s["close_price"] = s["sl_price"]  # 觸及止損價

        async def run():
            mock_close = AsyncMock(return_value=None)
            with patch("core.orders.close_position", mock_close):
                await check_exits(sym)
            mock_close.assert_not_called()

        asyncio.run(run())

    def test_spike_candle_does_not_suspend_take_profit(self):
        sym = "EXITSPIKE2USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0, n=15)
        update_trailing_stop(sym, 100.0, True)
        s["ohlcv"][-2] = [len(s["ohlcv"]) - 2, 100.0, 130.0, 100.0, 100.0, 1000.0]
        s["close_price"] = s["tp_price"]  # 觸及止利價

        async def run():
            mock_close = AsyncMock(return_value=None)
            with patch("core.orders.close_position", mock_close):
                await check_exits(sym)
            mock_close.assert_called_once()
            self.assertEqual(mock_close.call_args.kwargs.get("reason"), "[Take_Profit]")

        asyncio.run(run())

    def test_max_hold_timeout_forces_close(self):
        sym = "EXITTIMEOUT1USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)
        # 價格停在 SL/TP 之間，不會被停利/停損觸發，只測試時間強制出場。
        s["close_price"] = 100.1
        s["open_time"] = time.time() - EXIT_MAX_HOLD_SEC - 10

        async def run():
            mock_close = AsyncMock(return_value=None)
            with patch("core.orders.close_position", mock_close):
                await check_exits(sym)
            mock_close.assert_called_once()
            self.assertEqual(mock_close.call_args.kwargs.get("reason"), "[Max_Hold_Timeout]")

        asyncio.run(run())

    def test_no_close_when_price_between_sl_and_tp(self):
        sym = "EXITHOLD1USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)
        s["close_price"] = 100.1  # 介於 SL/TP 之間，未滿24小時

        async def run():
            mock_close = AsyncMock(return_value=None)
            with patch("core.orders.close_position", mock_close):
                await check_exits(sym)
            mock_close.assert_not_called()

        asyncio.run(run())

    # ── 回歸測試：XMRUSDT 實單案例 ──────────────────────────────────────────
    def test_breakeven_touch_then_immediate_reversal_still_locks_partial_profit(self):
        """[2026-07-25] 實單案例：XMRUSDT 07:42 進場，08:28 觸發保本後 2 秒內
        反轉，最終在接近保本處出場（獲利 -0.34%），完全沒鎖到已經走到的漲幅。
        根因：保本觸發那一次呼叫直接 return，沒有用觸發當下的價格順便算 75%
        鎖利，要等「下一筆更高的價格」才會補算——如果價格觸發保本後立刻回落，
        永遠等不到那一筆。修正後：保本觸發跟 75% 鎖利在同一次呼叫內完成。"""
        sym = "EXITREG1USDT"
        s = self._setup(sym, "long", avg=100.0, candle_range=1.0)
        update_trailing_stop(sym, 100.0, True)  # init: sl=98.5, tp=103.0

        # 價格觸及保本門檻 (0.8xATR = 100.8) 之上一點，然後立刻反轉回落。
        touched_peak = 100.85
        update_trailing_stop(sym, touched_peak, True)
        self.assertTrue(s["is_breakeven_moved"])
        locked_sl = s["sl_price"]
        # 修正前：locked_sl 會剛好等於 100.0（純保本，沒鎖到任何漲幅）。
        # 修正後：至少鎖住峰值的 75%。
        self.assertGreater(locked_sl, 100.0, "應鎖住部分已到手的漲幅，不能只回到純保本")
        self.assertAlmostEqual(locked_sl, 100.0 + (touched_peak - 100.0) * EXIT_TRAIL_LOCK_RATIO)

        # 立刻反轉回落，觸及剛剛鎖定的止損價。
        s["close_price"] = locked_sl
        s["close_price_spike_filtered"] = locked_sl

        async def run():
            mock_close = AsyncMock(return_value=None)
            with patch("core.orders.close_position", mock_close):
                await check_exits(sym)
            mock_close.assert_called_once()
            # 出場價已經是「峰值的75%」而不是純保本，實際獲利應為正值。
            exit_price = mock_close.call_args.args[3]
            self.assertGreater(exit_price, 100.0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
