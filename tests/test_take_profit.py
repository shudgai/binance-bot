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
    def test_static_profile_is_applied_when_radar_profile_is_missing(self):
        from core.symbol_profile import apply_symbol_profile
        sym = "HYPEUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.pop("trailing_activation_atr", None)
        apply_symbol_profile(sym, {})
        self.assertEqual(s["profile_type"], "High_Beta_Momentum")
        self.assertEqual(s["trailing_activation_atr"], 0.8)
        self.assertEqual(s["trailing_distance_atr"], 0.7)

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
        self.assertGreaterEqual(new_tp, 100.25)  # 費用 0.10% + 至少鎖定 0.15% 淨空間

    def test_low_atr_move_does_not_trigger_tiny_profit_trailing(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": 1.0, "avg_price": 100.0, "current_atr": 0.1,
                  "trailing_stop_price": 99.0, "trailing_highest": 100.0,
                  "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7})
        _, stop = update_trailing_stop(sym, 100.14, True)
        self.assertLessEqual(stop, 100.0)

    def test_core_soft_trailing_activates_between_point_fifteen_and_point_six(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": 1.0, "avg_price": 100.0, "current_atr": 0.1,
                  "trailing_stop_price": 99.0, "trailing_highest": 100.0})
        # [2026-07-14 再校準] soft_trailing 啟動門檻拉高至 0.45%，故測試價格用 100.48% 落在軟停利區間
        update_trailing_stop(sym, 100.48, True)
        self.assertGreater(s["trailing_stop_price"], 100.0)
        self.assertTrue(s["soft_trailing_armed"])

    def test_soft_trailing_does_not_activate_below_point_fifteen(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": 1.0, "avg_price": 100.0, "current_atr": 0.1,
                  "trailing_stop_price": 99.0, "trailing_highest": 100.0})
        update_trailing_stop(sym, 100.14, True)
        self.assertLessEqual(s["trailing_stop_price"], 100.0)

    def test_core_soft_trailing_creates_fee_safe_profit_side_stop(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": 1.0, "avg_price": 100.0, "current_atr": 0.25,
                  "trailing_stop_price": 0.0, "trailing_highest": 0.0})
        update_trailing_stop(sym, 100.48, True)  # 峰值 0.48%，落在軟停利區間
        self.assertGreater(s["trailing_stop_price"], s["avg_price"])
        self.assertAlmostEqual(s["soft_trailing_profit_floor"], 100.15, places=6)

    def test_soft_trailing_activates_early_without_instant_spurious_close(self):
        # 使用者要求「每個利潤都要能入袋」，啟動門檻從 0.3% 下修到 0.2%，後續拉高至 0.45%。
        # 門檻不能低於來回費用緩衝本身（0.15%）。這裡驗證峰值剛好卡在新門檻 0.45% 時，停利線嚴格低於當下價格（不會瞬間誤砍）。
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": 1.0, "avg_price": 100.0, "current_atr": 0.1,
                  "trailing_stop_price": 0.0, "trailing_highest": 0.0})
        current_price = 100.45  # 峰值剛好 0.45%，新門檻的邊界
        update_trailing_stop(sym, current_price, True)
        self.assertTrue(s.get("soft_trailing_armed", False))
        self.assertLess(s["trailing_stop_price"], current_price)

    def test_soft_trailing_locks_in_small_profit_and_follows_new_highs_tightly(self):
        # 回吐容忍度收緊，價格一創新高，停利線幾乎貼著峰值一起往上。
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": 1.0, "avg_price": 100.0, "current_atr": 0.1,
                  "trailing_stop_price": 0.0, "trailing_highest": 0.0})
        update_trailing_stop(sym, 100.48, True)  # 峰值 0.48%
        first_stop = s["trailing_stop_price"]
        # 停利線應緊貼峰值（容忍度收緊為 0.08%），這裡驗證它在 0.09% 內。
        self.assertGreater(first_stop, 100.48 * (1 - 0.0009))
        update_trailing_stop(sym, 100.68, True)  # 價格創新高到 0.68%
        second_stop = s["trailing_stop_price"]
        # 停利線必須跟著新高一起往上移動（棘輪只升停降）。
        self.assertGreater(second_stop, first_stop)
        self.assertGreater(second_stop, 100.68 * (1 - 0.0009))

    def test_soft_trailing_widens_tolerance_while_macd_momentum_still_climbing(self):
        # MACD 柱狀圖還在往有利方向擴張時（真的在噴出），停利線應該放寬，不要一根雜訊就洗出場；
        # 柱狀圖不再擴張（盤整/停滯）時應收緊。
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": 1.0, "avg_price": 100.0, "current_atr": 0.1,
                  "trailing_stop_price": 0.0, "trailing_highest": 0.0,
                  "macd_line": 0.02, "macd_signal": 0.01,       # macd_hist = 0.01
                  "prev_macd_line": 0.005, "prev_macd_signal": 0.005})  # prev_hist = 0.0 -> 擴張中
        update_trailing_stop(sym, 100.48, True)
        widened_stop = s["trailing_stop_price"]
        self.assertAlmostEqual(widened_stop, 100.33, places=2)

        reset_coin_state(sym)
        s.update({"qty": 1.0, "avg_price": 100.0, "current_atr": 0.1,
                  "trailing_stop_price": 0.0, "trailing_highest": 0.0,
                  "macd_line": 0.01, "macd_signal": 0.005,      # macd_hist = 0.005
                  "prev_macd_line": 0.02, "prev_macd_signal": 0.01})   # prev_hist = 0.01 -> 動能停滯
        update_trailing_stop(sym, 100.48, True)
        tightened_stop = s["trailing_stop_price"]
        self.assertGreater(tightened_stop, 100.35)
        self.assertAlmostEqual(tightened_stop, 100.40, places=2)

    def test_short_breakeven_lock_actually_engages(self):
        # trailing_stop_price 預設是 0.0（不是缺項）。空單保本鎖若誤把 0.0 當成
        # 「已存在的停損價」去跟新算出的保本價取 min()，會恆等於 0.0、鎖不上——
        # 空單達 0.6% 保本門檻後，必須正確鎖住獲利。
        # 價出場（扣兩邊手續費淨虧）。這裡驗證空單過了保本門檻後，
        # trailing_stop_price 必須被鎖在成本價以下（對空單來說代表鎖住利潤）。
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": -1.0, "avg_price": 100.0, "current_atr": 0.05,
                  "trailing_stop_price": 0.0, "trailing_lowest": float("inf")})
        update_trailing_stop(sym, 99.3, False)  # profit_pct = 0.7% > 0.6% 門檻
        self.assertGreater(s["trailing_stop_price"], 0.0)
        self.assertLess(s["trailing_stop_price"], 100.0)

    def test_peak_giveback_does_not_create_tiny_stop_below_entry(self):
        # 小幅盤中峰值後轉負，不能繞過 ATR/硬停損另造超窄停損。
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({
            "qty": 1.0, "avg_price": 100.0, "close_price": 99.5,
            "open_time": time.time() - 300,
            "last_entry_time": time.time() - 300,
            "last_entry_price": 100.0,
            "current_atr": 0.3,
            "atr_history": [0.3] * 10,
            "highest_profit_pct": 0.0025,
            "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7,
            "trailing_highest": 100.25,
            "macd_line": -0.01, "macd_signal": 0.0,
            "prev_macd_line": -0.005, "prev_macd_signal": 0.0,
            "current_rsi": 45.0, "prev_rsi": 47.0,
            "current_vol": 1000.0, "vol_ma20": 1000.0,
            "pnl_history": [],
            "ohlcv": [],
        })

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                for call in mock_close.await_args_list:
                    self.assertNotEqual(call.kwargs.get("reason"), "[Peak_Giveback]")

        asyncio.run(run_check())

    def test_small_noise_peak_does_not_bypass_atr_stop(self):
        # 即使小峰值後回落 0.20%，也應繼續交給正式風控判斷。
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({
            "qty": 1.0, "avg_price": 100.0, "close_price": 99.8,  # profit_pct = -0.20%
            "open_time": time.time() - 300,
            "last_entry_time": time.time() - 300,
            "last_entry_price": 100.0,
            "current_atr": 0.3,
            "atr_history": [0.3] * 10,
            "highest_profit_pct": 0.002,
            "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7,
            "trailing_highest": 100.2,
            "macd_line": -0.01, "macd_signal": 0.0,
            "prev_macd_line": -0.005, "prev_macd_signal": 0.0,
            "current_rsi": 45.0, "prev_rsi": 47.0,
            "current_vol": 1000.0, "vol_ma20": 1000.0,
            "pnl_history": [],
            "ohlcv": [],
        })

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                for call in mock_close.await_args_list:
                    self.assertNotEqual(call.kwargs.get("reason"), "[Peak_Giveback]")

        asyncio.run(run_check())

    def test_peak_giveback_does_not_fire_when_momentum_recovers(self):
        # 同樣曾有峰值、現在轉虧，但 MACD 動能已經在往有利方向改善（不是持續惡化）
        # ——這種情況不該被 Peak_Giveback 提早停損，要繼續給它機會。
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({
            "qty": 1.0, "avg_price": 100.0, "close_price": 99.5,
            "open_time": time.time() - 300,
            "last_entry_time": time.time() - 300,
            "last_entry_price": 100.0,
            "current_atr": 0.3,
            "atr_history": [0.3] * 10,
            "highest_profit_pct": 0.0025,
            "trailing_activation_atr": 0.8, "trailing_distance_atr": 0.7,
            "trailing_highest": 100.25,
            "macd_line": -0.005, "macd_signal": 0.0,
            "prev_macd_line": -0.01, "prev_macd_signal": 0.0,
            "current_rsi": 45.0, "prev_rsi": 47.0,
            "current_vol": 1000.0, "vol_ma20": 1000.0,
            "pnl_history": [],
            "ohlcv": [],
        })

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                for call in mock_close.await_args_list:
                    self.assertNotEqual(call.kwargs.get("reason"), "[Peak_Giveback]")

        asyncio.run(run_check())

    def test_liquidation_safety_floor_never_exceeds_entry_price(self):
        # safe_min_sl 是「搶在交易所強平前自己先出場」的安全下限，理應落在
        # liq_price 跟 avg_price 之間。舊公式直接對 liq_price 乘 1.2，8 倍槓桿時
        # liq_price=avg*0.8785，*1.2=avg*1.054，安全下限反而超過進場價——等於
        # 一開倉、還沒虧錢，就被這條「安全線」自己強制停損（XLMUSDT 實測案例）。
        sym = "XLMUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": 1657.0, "avg_price": 0.1908, "current_atr": 0.00033,
                  "trailing_stop_price": 0.0, "trailing_highest": 0.0,
                  "leverage": 8, "trailing_activation_atr": 1.0,
                  "trailing_distance_atr": 0.8})
        update_trailing_stop(sym, 0.1906, True)  # 現價小虧，尚未達任何鎖利門檻
        self.assertLess(s["trailing_stop_price"], s["avg_price"])

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
        # 此測試只驗證剩餘倉位的 PeakLock；分批停利另有獨立測試。
        s["has_partial_closed"] = True

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
            [int(time.time() * 1000), 100.0, 100.60, 99.8, 100.37, 500],
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
                self.assertEqual(s["trailing_highest"], 100.37)
                self.assertGreaterEqual(s["highest_profit_pct"], 0.006)
                self.assertLessEqual(mock_close.await_count, 1)

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
        s["close_price"] = 98.0  # 逆勢 4.0 ATR，大於 3.5 ATR 新門檻
        s["open_time"] = time.time() - 240
        s["last_entry_time"] = time.time() - 120
        s["last_entry_price"] = 100.0
        s["current_atr"] = 0.5
        s["current_rsi"] = 45.0
        s["prev_rsi"] = 47.0
        s["prev_macd_line"] = 0.01
        s["prev_macd_signal"] = 0.0
        # 讓 MACD 同向 (macd_hist > 0) 避開 Early_Direction_Invalid
        s["macd_line"] = 0.02
        s["macd_signal"] = 0.0
        s["ema20"] = 100.5
        s["current_vol"] = 2000.0
        s["vol_ma20"] = 1000.0
        s["ohlcv"] = [[0, 100.0, 100.5, 99.0, 99.2, 1200], [0, 100.2, 100.6, 99.1, 99.5, 1100]] # 讓最後收盤價比前一個高，使 _candle_opposite 為 False
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.0
        s["pnl_history"] = []

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Rapid_Reversal]")
                self.assertEqual(s.get("pending_reverse"), "sell")

        asyncio.run(run_check())

    def test_confirmed_early_short_invalidation_exits_and_rechecks_long(self):
        from unittest.mock import patch, AsyncMock
        sym = "INJUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({
            "qty": -33.9, "avg_price": 4.825, "close_price": 4.852,
            "open_time": time.time() - 480, "last_entry_time": time.time() - 480,
            "last_entry_price": 4.825, "current_atr": 0.01814,
            "atr_history": [0.01814] * 20, "current_rsi": 59.8,
            "macd_line": -0.0130, "macd_signal": -0.0135,
            "current_vol": 1000.0, "vol_ma20": 1000.0,
            "ohlcv": [[0, 4.82, 4.84, 4.81, 4.825, 1000], [1, 4.825, 4.86, 4.82, 4.852, 1100]],
            "pnl_history": [],
        })

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_not_called()
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Early_Direction_Invalid]")
                self.assertEqual(s.get("pending_reverse"), "buy")

        asyncio.run(run_check())

    def test_confirmed_early_invalidation_exits_at_one_atr(self):
        from unittest.mock import patch, AsyncMock
        sym = "INJUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({
            "qty": -10.0, "avg_price": 100.0, "close_price": 100.30,
            "open_time": time.time() - 300, "last_entry_time": time.time() - 300,
            "last_entry_price": 100.0, "current_atr": 0.28,
            "atr_history": [0.28] * 20, "current_rsi": 58.0,
            "macd_line": 0.01, "macd_signal": 0.0,
            "current_vol": 1000.0, "vol_ma20": 1000.0,
            "ohlcv": [[0, 100.0, 100.1, 99.9, 100.0, 1000],
                      [1, 100.0, 100.4, 99.9, 100.3, 1100]],
            "pnl_history": [],
        })

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_not_called()
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Early_Direction_Invalid]")

        asyncio.run(run_check())

    def test_small_profit_does_not_take_a_partial_exit(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({
            "qty": 2.0, "avg_price": 100.0, "close_price": 100.32,
            "open_time": time.time() - 300, "last_entry_time": time.time() - 300,
            "last_entry_price": 100.0, "current_atr": 0.2,
            "atr_history": [0.2] * 20, "current_rsi": 55.0,
            "macd_line": 0.01, "macd_signal": 0.0,
            "prev_macd_line": 0.005, "prev_macd_signal": 0.0,
            "current_vol": 1000.0, "vol_ma20": 1000.0,
            "ohlcv": [], "pnl_history": [], "profile_type": "Core_Trend",
        })

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_not_called()

        asyncio.run(run_check())

    def test_peak_volume_contraction_closes_the_entire_position(self):
        from unittest.mock import patch, AsyncMock
        sym = "SOLUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        candles = []
        for i in range(20):
            candles.append([i * 300000, 100.0, 100.2, 99.8, 100.0, 1000.0])
        candles.append([20 * 300000, 100.0, 100.2, 99.4, 99.5, 500.0])
        candles.append([21 * 300000, 99.5, 99.6, 99.45, 99.5, 50.0])
        s.update({
            "qty": -2.0, "avg_price": 100.0, "close_price": 99.5,
            "open_time": time.time() - 900, "last_entry_time": time.time() - 900,
            "last_entry_price": 100.0, "current_atr": 0.2,
            "atr_history": [0.2] * 20, "current_rsi": 45.0,
            "macd_line": -0.02, "macd_signal": -0.01,
            "prev_macd_line": -0.015, "prev_macd_signal": -0.01,
            "current_vol": 50.0, "vol_ma20": 1000.0,
            "ohlcv": candles, "pnl_history": [],
            "highest_profit_pct": 0.0055,
        })

        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                mock_close.assert_not_called()
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Peak_Volume_Contraction]")
                self.assertAlmostEqual(mock_close.await_args.args[2], 2.0)

        asyncio.run(run_check())

    def test_single_early_opposite_sample_does_not_exit(self):
        from unittest.mock import patch, AsyncMock
        sym = "INJUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({
            "qty": -1.0, "avg_price": 4.825, "close_price": 4.852,
            "open_time": time.time() - 480, "last_entry_time": time.time() - 480,
            "last_entry_price": 4.825, "current_atr": 0.01814,
            "atr_history": [0.01814] * 20, "current_rsi": 59.8,
            "macd_line": -0.0130, "macd_signal": -0.0135,
            "current_vol": 1000.0, "vol_ma20": 1000.0,
            "ohlcv": [[0, 4.82, 4.84, 4.81, 4.825, 1000], [1, 4.825, 4.86, 4.82, 4.852, 1100]],
            "pnl_history": [],
        })
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
        # 設定 has_partial_closed 為 True，防止 XRPUSDT 超過 0.2% 獲利時先被分批停利攔截
        s["has_partial_closed"] = True
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

    def test_breakeven_does_not_lock_tiny_profit_0_35_percent(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 100.15  # 0.15% profit (低於分批停利 0.2%)
        s["open_time"] = time.time() - 120
        s["current_atr"] = 0.5
        s["current_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0
        s["macd_signal"] = 0.0
        s["ohlcv"] = [[0, 100, 100, 99, 100, 1000]]
        s["prev_close"] = 100.0
        s["highest_profit_pct"] = 0.0015  # 0.15% peak profit (低於新的 0.2% 分批停利與 0.25% 保本鎖門檻)
        s["pnl_history"] = []
        s["vol_ma20"] = 1.0
        s["current_vol"] = 1.0

        import asyncio
        async def run_check():
            with patch("core.orders.close_position", AsyncMock()) as mock_close:
                await check_exits(sym)
                self.assertFalse(s.get("is_breakeven_locked", False))
                mock_close.assert_not_called()

        asyncio.run(run_check())

    def test_speculative_profile_waits_until_one_percent_to_lock(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": 1.0, "avg_price": 100.0, "current_atr": 0.2,
                  "trailing_stop_price": 0.0, "trailing_highest": 100.0,
                  "profile_type": "Speculative_Risk"})
        # [2026-07-14 再校準] Speculative 屬性保本門檻從 1.0% 調降至 0.6%。
        # 當價格上漲到 0.5% (100.5) 時，應未達到 0.6% 保本鎖，但已進入 Soft Trailing。
        update_trailing_stop(sym, 100.5, True)
        self.assertFalse(s.get("is_breakeven_locked", False))
        self.assertTrue(s.get("soft_trailing_armed", False))
        self.assertGreater(s["trailing_stop_price"], s["avg_price"])

        update_trailing_stop(sym, 101.1, True)
        self.assertTrue(s.get("is_breakeven_locked", False))
        self.assertGreater(s["trailing_stop_price"], s["avg_price"])

    def test_soft_trailing_only_moves_up_with_new_high(self):
        sym = "INJUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({"qty": 1.0, "avg_price": 100.0, "current_atr": 0.2,
                  "trailing_stop_price": 0.0, "trailing_highest": 100.0,
                  "profile_type": "High_Beta_Momentum"})
        update_trailing_stop(sym, 100.2, True)
        first_stop = s["trailing_stop_price"]
        update_trailing_stop(sym, 100.4, True)
        raised_stop = s["trailing_stop_price"]
        update_trailing_stop(sym, 100.3, True)
        self.assertGreater(raised_stop, first_stop)
        self.assertEqual(s["trailing_stop_price"], raised_stop)

    def test_hard_stop_loss_still_triggers_during_initial_cooldown(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 95.0
        # 已離開 60 秒盲區，但仍在一般 90 秒觀察期內。
        s["open_time"] = time.time() - 70
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
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Hard_Stop_Loss]")
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

    def test_high_point_stagnation_exit(self):
        from unittest.mock import patch, AsyncMock
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 100.35  # 0.35% profit
        # 現行弱動能且曾有峰值的停滯期限為 7200 秒。
        s["open_time"] = time.time() - 7300
        s["peak_time"] = time.time() - 7200
        s["current_atr"] = 0.5
        s["current_rsi"] = 50.0
        s["prev_macd_line"] = 0.0
        s["prev_macd_signal"] = 0.0
        s["macd_line"] = 0.0  # MACD not expanding
        s["macd_signal"] = 0.0
        s["ohlcv"] = [[0, 100.0, 100.35, 99.0, 100.35, 1000]]
        s["prev_close"] = 100.35
        s["highest_profit_pct"] = 0.0035
        s["pnl_history"] = []
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 100.0

        async def run_check():
            # 建立一個會實際扣減 s["qty"] 的 mock 函數，解決 Mock 測試下同時觸發多重平倉的問題
            async def side_effect_close(sym, cs, qty, p, avg, reason=None, is_stop_loss=False):
                s["qty"] = max(0.0, s["qty"] - qty)
            
            with patch("core.orders.close_position", AsyncMock(side_effect=side_effect_close)) as mock_close:
                await check_exits(sym)
                mock_close.assert_called_once()
                self.assertEqual(mock_close.await_args.kwargs["reason"], "[Stagnation_Timeout]")

        asyncio.run(run_check())


if __name__ == "__main__":
    unittest.main()
