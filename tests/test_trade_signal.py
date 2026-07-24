import unittest
import sys
import os
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ctx
from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.signal_engine import (compute_signal_strength, compute_range_signal,
    _find_horizontal_zones)
from core.check_entries import (check_entries, _entry_structure_quality,
    _ma_candidate_quality, _range_candidate_quality,
    _ma25_pullback_sample_bonus, _ma_cross_sample_bonus)


class TradeSignalTests(unittest.TestCase):
    def _setup_ma_signal_state(self, *, sym="XRPUSDT", signal_open=100.0, signal_high=101.2,
                               signal_low=99.8, signal_close=101.0,
                               signal_volume=1200.0, vol_ma20=1000.0,
                               ma7=100.2, ma25=100.0, ma99=99.0,
                               prev_ma7=99.9, prev_ma25=100.0, adx=25.0):
        init_states([sym])
        reset_coin_state(sym)
        base = [[i, 100.0, 100.5, 99.5, 100.0, vol_ma20] for i in range(20)]
        signal = [20, signal_open, signal_high, signal_low, signal_close, signal_volume]
        live = [21, signal_close, signal_close, signal_close, signal_close, 1.0]
        STATES[sym].update({
            "status": "ACTIVE", "ohlcv": base + [signal, live],
            "closes": [100.0] * 22, "close_price": signal_close,
            "ma7": ma7, "ma25": ma25, "ma99": ma99,
            "prev_ma7": prev_ma7, "prev_ma25": prev_ma25,
            "vol_ma20": vol_ma20, "current_atr": 0.4,
            "current_rsi": 55.0 if ma7 >= ma25 else 45.0,
            # 預設給一個明確有趨勢的 ADX，避免跟 MIN_TREND_ADX 門檻打架；
            # 需要測試低 ADX 行為的案例（暴衝過濾等）再個別覆寫。
            "adx": adx, "prev_adx": adx,
        })
        return sym

    def test_completed_candle_volume_is_used_even_when_live_surge_is_zero(self):
        sym = self._setup_ma_signal_state(signal_volume=1200.0, vol_ma20=1000.0)
        STATES[sym].update({"current_rsi": 55.0, "vol_surge": 0.0})
        side, _, route = compute_signal_strength(sym)
        self.assertNotEqual(route, "MA_Cross")

    def test_live_surge_does_not_replace_missing_completed_volume(self):
        sym = self._setup_ma_signal_state(signal_volume=300.0, vol_ma20=1000.0)
        STATES[sym].update({"current_rsi": 55.0, "vol_surge": 2.0})
        self.assertEqual(compute_signal_strength(sym, realtime_trigger=True), (None, 0, None))
        self.assertIn("量能不足", STATES[sym]["entry_block_reason"])

    def test_flat_adx_blocks_all_ma_routes(self):
        # 實測 DOTUSDT（ADX=0.0）、BCHUSDT（ADX=1.4）、AVAXUSDT（ADX=2.7）三筆
        # 案例：完全沒有趨勢的盤整行情下，MA_Cross/MA7_Simple 一樣會觸發訊號，
        # 進場後幾十秒內就整段反轉。MIN_TREND_ADX 門檻對所有 MA 路線一視同仁。
        sym = self._setup_ma_signal_state(adx=2.7)
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))
        self.assertIn("盤整無趨勢", STATES[sym]["entry_block_reason"])

    def test_confirmed_golden_cross_is_disabled_when_disable_ma_cross_is_true(self):
        sym = self._setup_ma_signal_state()
        side, strength, route = compute_signal_strength(sym)
        self.assertNotEqual(route, "MA_Cross")

    def test_signal_engine_keeps_cross_candidate_below_ma99_for_final_guard(self):
        sym = self._setup_ma_signal_state(ma99=102.0)
        side, _, route = compute_signal_strength(sym)
        self.assertNotEqual(route, "MA_Cross")

    def test_confirmed_death_cross_is_disabled_when_disable_ma_cross_is_true(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=99.0, signal_low=98.8,
            ma7=99.7, ma25=100.0, ma99=101.0,
            prev_ma7=100.2, prev_ma25=100.0,
        )
        side, strength, route = compute_signal_strength(sym)
        self.assertNotEqual(route, "MA_Cross")

    def test_cross_without_volume_is_rejected(self):
        sym = self._setup_ma_signal_state(signal_volume=400.0)
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))
        self.assertIn("量能不足", STATES[sym]["entry_block_reason"])

    def test_clean_ma99_aligned_cross_accepts_eth_like_half_rvol(self):
        sym = self._setup_ma_signal_state(
            signal_volume=520.0, vol_ma20=1000.0, ma99=99.0,
        )
        side, _, route = compute_signal_strength(sym)
        self.assertNotEqual(route, "MA_Cross")

    def test_half_rvol_cross_on_wrong_ma99_side_remains_blocked(self):
        sym = self._setup_ma_signal_state(
            signal_volume=520.0, vol_ma20=1000.0, ma99=102.0,
        )
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))

    def test_thin_golden_cross_is_rejected_for_insufficient_direction_confirmation(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.2, signal_close=100.4, signal_low=100.3,
            signal_volume=800.0, ma7=100.003, ma25=100.0, ma99=99.0,
            prev_ma7=99.9, prev_ma25=100.0,
        )
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))
        self.assertIn("方向確認不足", STATES[sym]["entry_block_reason"])

    def test_thin_death_cross_is_rejected_for_insufficient_direction_confirmation(self):
        sym = self._setup_ma_signal_state(
            signal_open=99.8, signal_close=99.6, signal_high=99.7,
            signal_volume=800.0, ma7=99.997, ma25=100.0, ma99=101.0,
            prev_ma7=100.1, prev_ma25=100.0,
        )
        # MA7 勾頭向下但 ADX < 22 時被過濾
        STATES[sym]["adx"] = 15.0
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))

    def test_ma25_pullback_enters_only_after_bullish_rejection(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.05, signal_high=100.20, signal_close=100.14, signal_low=99.9,
            ma7=100.6, ma25=100.0, ma99=99.0,
            prev_ma7=100.4, prev_ma25=99.9,
            signal_volume=900.0,
        )
        side, _, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("buy", "MA25_Pullback"))

    def test_ma25_pullback_rejects_rebound_at_signal_high(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.1, signal_high=100.35, signal_close=100.3, signal_low=99.9,
            ma7=100.6, ma25=100.0, ma99=99.0,
            prev_ma7=100.4, prev_ma25=99.9, signal_volume=900.0,
        )
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))

    def test_non_eth_xrp_restores_legacy_ma25_rebound_entry(self):
        sym = self._setup_ma_signal_state(
            sym="SOLUSDT", signal_open=100.1, signal_high=100.35, signal_close=100.3, signal_low=99.9,
            ma7=100.6, ma25=100.0, ma99=99.0,
            prev_ma7=100.4, prev_ma25=99.9, signal_volume=900.0,
        )
        side, _, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("buy", "MA25_Pullback"))

    def test_golden_cross_above_ma99_does_not_require_ma25_above_ma99_yet(self):
        sym = self._setup_ma_signal_state(ma99=100.1)
        side, _, route = compute_signal_strength(sym)
        self.assertNotEqual(route, "MA_Cross")

    def test_death_cross_below_ma99_does_not_require_ma25_below_ma99_yet(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=99.0, signal_low=98.8,
            ma7=99.7, ma25=100.0, ma99=99.5,
            prev_ma7=100.2, prev_ma25=100.0,
        )
        side, _, route = compute_signal_strength(sym)
        self.assertNotEqual(route, "MA_Cross")

    def test_flat_intertwined_ma_is_blocked(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=100.0, signal_volume=900.0,
            ma7=100.02, ma25=100.0, ma99=99.0,
            prev_ma7=99.99, prev_ma25=100.0,
        )
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))
        self.assertIn("平走交織", STATES[sym]["entry_block_reason"])

    def test_ma7_simple_long_trigger(self):
        # We need golden_cross/death_cross etc. to be false to fall into MA7_Simple.
        # e.g., ma7 > ma25 and prev_ma7 > prev_ma25 (so golden_cross is false).
        # We also need long_spreading = False to prevent pullback_long.
        # long_spreading = ma7 > ma25 and ma7 > prev_ma7 and gap > max(prev_gap, 0.0)
        # gap = ma7 - ma25 = 101.5 - 101.4 = 0.1
        # prev_gap = prev_ma7 - prev_ma25 = 101.0 - 100.8 = 0.2
        # Here gap (0.1) is NOT > max(prev_gap, 0.0) (0.2), so long_spreading is False.
        # Turn up: prev_slope = prev_ma7 - prev_ma7_2 <= 0 and curr_slope = ma7 - prev_ma7 > 0
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=101.5, signal_volume=1000.0, vol_ma20=1000.0,
            ma7=101.5, ma25=101.4, prev_ma7=101.0, prev_ma25=100.8
        )
        STATES[sym].update({
            "prev_ma7_2": 101.2,  # prev_slope = 101.0 - 101.2 = -0.2 (<= 0)
                                  # curr_slope = 101.5 - 101.0 = +0.5 (> 0)
            "current_rsi": 60.0   # < 75.0
        })
        side, strength, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("buy", "MA7_Simple"))

    def test_ma7_simple_minimum_volume_is_ranked_below_strong_signal(self):
        # 量能門檻已拉齊到現行非核心幣的 0.80x（見 signal_engine.py 說明），
        # 這裡用 0.80x（現行門檻）驗證「能過但排序較低」的行為還在。
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=101.5, signal_volume=800.0, vol_ma20=1000.0,
            ma7=101.5, ma25=101.4, prev_ma7=101.0, prev_ma25=100.8,
        )
        STATES[sym].update({"prev_ma7_2": 101.2, "current_rsi": 60.0})
        weak_side, weak_strength, weak_route = compute_signal_strength(sym)

        STATES[sym]["ohlcv"][-2][5] = 1000.0
        strong_side, strong_strength, strong_route = compute_signal_strength(sym)

        self.assertEqual((weak_side, weak_route), ("buy", "MA7_Simple"))
        self.assertEqual((strong_side, strong_route), ("buy", "MA7_Simple"))
        self.assertAlmostEqual(weak_strength, 25.0)
        self.assertAlmostEqual(strong_strength, 26.0)
        self.assertGreater(strong_strength, weak_strength)

    def test_ma7_simple_below_point_eight_volume_is_rejected(self):
        # 原本 MA7_Simple 量能門檻 0.5x 比其他路線都寬鬆；拉齊到 0.80x 後，
        # 0.5x 這種低於平均量的轉折應該直接被拒絕，不再是「排序較低但仍放行」。
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=100.5, signal_volume=500.0, vol_ma20=1000.0,
            ma7=101.5, ma25=101.4, prev_ma7=101.0, prev_ma25=100.8,
        )
        STATES[sym].update({"prev_ma7_2": 101.2, "current_rsi": 60.0})
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))

    def test_ma7_simple_long_rejected_when_ma25_falling(self):
        # MA7 單根蠟燭翻頭向上，但 MA25 中期趨勢還在跌：不該放行，
        # 這正是拖垮 MA7_Simple 勝率的主因（20% 勝率、單路線吃掉過半虧損）。
        # ma25 下跌同時讓 long_stack 為 False，所以也不會誤落到 Pullback/Breakout。
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=100.5, signal_volume=1000.0, vol_ma20=1000.0,
            ma7=101.7, ma25=101.4, prev_ma7=101.6, prev_ma25=101.5,
        )
        STATES[sym].update({"prev_ma7_2": 101.65, "current_rsi": 60.0})
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))

    def test_hbar_ma7_simple_long_rejected_when_far_above_ma25(self):
        sym = self._setup_ma_signal_state(
            sym="HBARUSDT", signal_open=0.07419, signal_high=0.07438,
            signal_low=0.07418, signal_close=0.07434,
            signal_volume=2032842.0, vol_ma20=1658564.0,
            ma7=0.074297142857, ma25=0.0738664, ma99=0.073276868687,
            prev_ma7=0.074277142857, prev_ma25=0.0738304, adx=59.29,
        )
        STATES[sym].update({
            "prev_ma7_2": 0.074278571429, "current_atr": 0.000182142857,
            "current_rsi": 55.56, "rsi_15m": 55.0, "prev_adx": 59.29,
        })

        self.assertEqual(compute_signal_strength(sym), (None, 0, None))
        self.assertIn("反彈末端不追多", STATES[sym]["entry_block_reason"])

    def test_ma7_simple_long_requires_15m_rsi_above_midline(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=100.4, signal_volume=1000.0,
            vol_ma20=1000.0, ma7=100.3, ma25=100.1,
            prev_ma7=100.2, prev_ma25=100.0,
        )
        STATES[sym].update({
            "prev_ma7_2": 100.25, "current_rsi": 55.0,
            "rsi_15m": 48.3, "current_atr": 0.4,
        })

        self.assertEqual(compute_signal_strength(sym), (None, 0, None))
        self.assertIn("多週期仍偏空不做多", STATES[sym]["entry_block_reason"])

    def test_ma7_simple_rejected_when_adx_just_spiked(self):
        # 實測 LINKUSDT 案例：ADX 15 秒內從 7.7 暴衝到 35.4，同一輪掃描就冒出
        # MA7_Simple 訊號，其實只是單根尖刺行情，不是真正累積出來的趨勢。
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=100.5, signal_volume=1000.0, vol_ma20=1000.0,
            ma7=101.5, ma25=101.4, prev_ma7=101.0, prev_ma25=100.8,
        )
        STATES[sym].update({
            "prev_ma7_2": 101.2, "current_rsi": 60.0,
            "adx": 35.4, "prev_adx": 7.7,
        })
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))

    def test_ma7_simple_allowed_when_adx_builds_up_gradually(self):
        # 對照組：ADX 是緩慢累積上來的（相鄰兩輪掃描差距小），不該被誤擋。
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=101.5, signal_volume=1000.0, vol_ma20=1000.0,
            ma7=101.5, ma25=101.4, prev_ma7=101.0, prev_ma25=100.8,
        )
        STATES[sym].update({
            "prev_ma7_2": 101.2, "current_rsi": 60.0,
            "adx": 30.4, "prev_adx": 27.3,
        })
        side, _, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("buy", "MA7_Simple"))

    def test_ma7_simple_short_trigger(self):
        # Turn down: prev_slope = prev_ma7 - prev_ma7_2 >= 0 and curr_slope = ma7 - prev_ma7 < 0
        # To prevent pullback_short: short_spreading = False
        # short_spreading = ma7 < ma25 and ma7 < prev_ma7 and gap < min(prev_gap, 0.0)
        # gap = ma7 - ma25 = 99.0 - 100.0 = -1.0
        # prev_gap = prev_ma7 - prev_ma25 = 99.5 - 101.0 = -1.5
        # gap (-1.0) is NOT < min(prev_gap, 0.0) (-1.5), so short_spreading is False.
        sym = self._setup_ma_signal_state(
            signal_open=100.5, signal_close=100.0, signal_volume=1000.0, vol_ma20=1000.0,
            ma7=99.0, ma25=100.0, prev_ma7=99.5, prev_ma25=101.0
        )
        STATES[sym].update({
            "prev_ma7_2": 99.2,   # prev_slope = 99.5 - 99.2 = +0.3 (>= 0)
                                  # curr_slope = 99.0 - 99.5 = -0.5 (< 0)
            "current_rsi": 40.0   # > 25.0
        })
        side, strength, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("sell", "MA7_Simple"))

    def _setup_range_signal_state(self, signal):
        sym = "XRPUSDT"
        init_states([sym])
        reset_coin_state(sym)
        base = [[i, 100.0, 100.4, 99.6, 100.0, 1000.0] for i in range(20)]
        confirmation = [21, signal[4], signal[4] + 0.3, signal[3] + 0.1, signal[4] + 0.2, 1000.0]
        live = [22, confirmation[4], confirmation[4], confirmation[4], confirmation[4], 1.0]
        STATES[sym].update({
            "status": "ACTIVE", "ohlcv": base + [signal, confirmation, live],
            "close_price": confirmation[4], "current_atr": 1.0,
            "adx": 10.0, "current_rsi": 50.0, "vol_ma20": 1000.0,
            "ema20_15m": 101.0, "ema50_15m": 100.0,
        })
        return sym

    def test_range_zone_picker_uses_nearest_levels_around_price(self):
        candles = [
            [0, 100, 101.00, 95.00, 100, 1],
            [1, 100, 101.02, 95.02, 100, 1],
            [2, 100, 105.00, 99.00, 100, 1],
            [3, 100, 105.02, 99.02, 100, 1],
        ]
        support, resistance = _find_horizontal_zones(
            candles, 0.1, 40, 2, 0.3, current_price=100.0,
        )
        self.assertAlmostEqual(support, 99.01)
        self.assertAlmostEqual(resistance, 101.01)

    def test_range_zone_picker_skips_nearby_noise_for_tradeable_pair(self):
        candles = [
            [0, 100.0, 100.05, 99.95, 100.0, 1],
            [1, 100.0, 100.06, 99.96, 100.0, 1],
            [2, 100.0, 101.01, 99.00, 100.0, 1],
            [3, 100.0, 101.02, 99.01, 100.0, 1],
        ]
        support, resistance = _find_horizontal_zones(
            candles, 0.1, 40, 2, 0.3,
            current_price=100.0, min_width_pct=0.009,
        )
        self.assertIsNotNone(support)
        self.assertIsNotNone(resistance)
        self.assertGreaterEqual((resistance - support) / support, 0.009)

    def test_range_signal_requires_both_sides_of_range(self):
        sym = self._setup_range_signal_state([20, 99.0, 99.3, 98.9, 99.2, 1000.0])
        with patch("core.signal_engine._find_horizontal_zones", return_value=(99.0, None)):
            self.assertEqual(compute_range_signal(sym), (None, 0, None))
        self.assertIn("同時找到", STATES[sym]["entry_block_reason"])

    def test_range_long_requires_rejection_at_support(self):
        sym = self._setup_range_signal_state([20, 99.5, 99.6, 98.9, 99.1, 1000.0])
        with patch("core.signal_engine._find_horizontal_zones", return_value=(99.0, 103.0)):
            self.assertEqual(compute_range_signal(sym), (None, 0, None))

    def test_range_long_opens_after_bullish_support_rejection(self):
        sym = self._setup_range_signal_state([20, 99.0, 99.4, 98.9, 99.2, 1000.0])
        STATES[sym]["current_rsi"] = 49.0
        with patch("core.signal_engine._find_horizontal_zones", return_value=(99.0, 103.0)):
            side, strength, route = compute_range_signal(sym)
        self.assertEqual((side, route), ("buy", "Range_Support_Long"))
        self.assertGreaterEqual(strength, 18.0)
        self.assertEqual(STATES[sym]["range_support_level"], 99.0)
        self.assertEqual(STATES[sym]["range_resistance_level"], 103.0)

    def test_range_signal_rejects_high_adx_before_becoming_candidate(self):
        sym = self._setup_range_signal_state([20, 99.0, 99.4, 98.9, 99.2, 1000.0])
        STATES[sym]["adx"] = 50.0  # 高於放寬後的 RANGE_ADX_THRESHOLD (45.0)
        with patch("core.signal_engine._find_horizontal_zones", return_value=(99.0, 103.0)):
            self.assertEqual(compute_range_signal(sym), (None, 0, None))
        self.assertIn("改由 MA", STATES[sym]["entry_block_reason"])

    def test_core_liquid_symbols_use_slightly_lower_ma_volume_floor(self):
        from core.signal_engine import _ma_base_volume_limit
        self.assertEqual(_ma_base_volume_limit("ETHUSDT", 0.2), 0.70)
        self.assertEqual(_ma_base_volume_limit("SOLUSDT", 0.2), 0.80)
        self.assertEqual(_ma_base_volume_limit("ETHUSDT", 6.0), 1.0)

    def test_range_long_rejected_when_rsi_still_falling_fast(self):
        # 實測 ADAUSDT 案例：支撐反彈訊號觸發當下 RSI=50，但不到一分鐘內連續
        # 幾輪掃描 RSI 一路殺到 29.4，代表賣壓根本沒停，進場後直接跌破支撐，
        # 從未反彈過（峰值 0%）。
        sym = self._setup_range_signal_state([20, 99.0, 99.4, 98.9, 99.2, 1000.0])
        STATES[sym].update({"current_rsi": 29.4, "prev_rsi": 50.0})
        with patch("core.signal_engine._find_horizontal_zones", return_value=(99.0, 103.0)):
            self.assertEqual(compute_range_signal(sym), (None, 0, None))

    def test_range_long_allowed_when_rsi_stable(self):
        sym = self._setup_range_signal_state([20, 99.0, 99.4, 98.9, 99.2, 1000.0])
        STATES[sym].update({"current_rsi": 48.0, "prev_rsi": 50.0})
        with patch("core.signal_engine._find_horizontal_zones", return_value=(99.0, 103.0)):
            side, _, route = compute_range_signal(sym)
        self.assertEqual((side, route), ("buy", "Range_Support_Long"))

    def test_strict_range_long_rejects_countertrend_15m_bounce(self):
        sym = self._setup_range_signal_state([20, 99.0, 99.4, 98.9, 99.2, 1000.0])
        STATES[sym].update({
            "current_rsi": 48.0, "prev_rsi": 49.0,
            "ema20_15m": 99.0, "ema50_15m": 100.0,
        })
        with patch("core.signal_engine._find_horizontal_zones", return_value=(99.0, 103.0)):
            self.assertEqual(compute_range_signal(sym), (None, 0, None))
        self.assertIn("15m 趨勢同向", STATES[sym]["entry_block_reason"])

    def test_strict_range_long_requires_second_closed_candle_confirmation(self):
        sym = self._setup_range_signal_state([20, 99.0, 99.4, 98.9, 99.2, 1000.0])
        STATES[sym].update({"current_rsi": 48.0, "prev_rsi": 49.0})
        STATES[sym]["ohlcv"][-2] = [21, 99.2, 99.3, 98.8, 99.1, 1000.0]
        with patch("core.signal_engine._find_horizontal_zones", return_value=(99.0, 103.0)):
            self.assertEqual(compute_range_signal(sym), (None, 0, None))
        self.assertIn("第二根收線", STATES[sym]["entry_block_reason"])

    def test_kaito_like_range_setup_gets_priority_without_becoming_a_gate(self):
        sym = self._setup_range_signal_state([20, 99.0, 99.4, 98.9, 99.2, 750.0])
        state = STATES[sym]
        state.update({"adx": 10.6, "current_rsi": 41.6})

        preferred = _range_candidate_quality(state, "buy", 20.8, 2.99, 0.0095)
        self.assertAlmostEqual(preferred, 27.8)
        self.assertAlmostEqual(state["_range_sample_bonus"], 7.0)

        state.update({"adx": 25.0, "current_rsi": 54.0})
        state["ohlcv"][-2][5] = 600.0
        ordinary = _range_candidate_quality(state, "buy", 20.8, 1.2, 0.0081)
        self.assertAlmostEqual(ordinary, 20.8)
        self.assertGreater(preferred, ordinary)

    def test_t_like_ma25_pullback_gets_priority_without_becoming_a_gate(self):
        state = {
            "adx": 73.3, "current_rsi": 59.2, "ma99": 0.004140,
        }
        preferred = _ma25_pullback_sample_bonus(state, "buy", 30.0, 0.004210, 11.42)
        self.assertAlmostEqual(preferred, 6.5)
        self.assertAlmostEqual(state["_ma25_sample_bonus"], 6.5)

        state.update({"adx": 20.0, "current_rsi": 50.0})
        ordinary = _ma25_pullback_sample_bonus(state, "buy", 25.0, 0.004100, 0.6)
        self.assertAlmostEqual(ordinary, 0.0)
        self.assertGreater(preferred, ordinary)

    def test_strong_ma_cross_sample_is_ranked_above_ordinary_cross(self):
        state = {"adx": 45.0, "current_rsi": 40.0, "ma99": 101.0}
        preferred = _ma_cross_sample_bonus(state, "sell", 30.0, 99.0, 1.81)
        self.assertAlmostEqual(preferred, 6.5)
        self.assertAlmostEqual(state["_ma_cross_sample_bonus"], 6.5)

        state.update({"adx": 20.0, "current_rsi": 50.0})
        ordinary = _ma_cross_sample_bonus(state, "sell", 25.0, 102.0, 0.6)
        self.assertAlmostEqual(ordinary, 0.5)
        self.assertGreater(preferred, ordinary)


    def _setup_liquidity_discount_state(self, vol_ma20):
        sym = self._setup_ma_signal_state(
            signal_open=100.05, signal_high=100.20, signal_close=100.14, signal_low=99.9,
            signal_volume=vol_ma20 * 1.5, vol_ma20=vol_ma20,
            ma7=100.6, ma25=100.0, ma99=99.0,
            prev_ma7=100.4, prev_ma25=99.9,
        )
        ctx.ALL_SYMBOLS[:] = [sym]
        ctx.MARKET_WIND.update({
            "btc_trend_1h": "BULL", "btc_trend_4h": "BULL",
            "btc_macro_updated_at": time.time(),
        })
        s = STATES[sym]
        s.update({"adx": 30.0, "prev_adx": 28.0})
        for candle in s["ohlcv"][:-2]:
            candle[3] = 98.0
        from core.symbol_profile import SYMBOL_PROFILES
        SYMBOL_PROFILES[sym] = {"_trade_eligible": True}
        s.update({
            "current_rsi": 45.0, "macd_hist": 0.0,
            "ema20": 100.0, "ema50": 100.5, "ema50_1h": 101.0,
            "sma200_15m": 101.0, "bb_low": 95.0, "bb_up": 105.0,
            "current_vol": vol_ma20 * 1.5, "atr_history": [0.4] * 10,
            "qty": 0.0, "pending_side": None,
        })
        return sym

    def test_long_entry_too_close_to_resistance_is_rejected(self):
        sym = self._setup_ma_signal_state(signal_close=100.45)
        state = STATES[sym]
        state["current_atr"] = 0.2
        ok, reason, _ = _entry_structure_quality(sym, "buy", "MA_Cross", 100.45)
        self.assertFalse(ok)
        self.assertIn("阻力", reason)

    def test_long_entry_with_resistance_room_is_allowed(self):
        sym = self._setup_ma_signal_state(signal_close=99.0)
        state = STATES[sym]
        state["current_atr"] = 0.2
        ok, _, score = _entry_structure_quality(sym, "buy", "MA25_Pullback", 99.0)
        self.assertTrue(ok)
        self.assertGreater(score, 0.0)

    def test_marginal_liquidity_discounts_allocation(self):
        # 使用者要求：流動性檢查現有的門檻是二選一（過 1,000,000 全額進場、沒過整筆
        # 擋掉），但「剛好壓線過關」風險比「流動性充裕」高很多，不該用同樣的倉位。
        # 這裡驗證估算 24H 交易額剛過門檻（約 1,150,000）時，分配到的資金比例會被
        # 打折到約 5 折附近，而不是跟流動性充裕時一樣的滿額。
        from unittest.mock import patch, AsyncMock
        import asyncio
        sym = self._setup_liquidity_discount_state(vol_ma20=40.0)  # h24 ≈ 1,150,000

        async def run_check():
            mock_exec = AsyncMock(return_value=None)
            with patch("core.orders.execute_order", mock_exec), \
                 patch("core.check_entries.is_daily_loss_halted", return_value=False), \
                 patch("core.check_entries.get_open_position_count", return_value=0), \
                 patch("core.check_entries.get_last_same_side_loss_time", return_value=0.0), \
                 patch("core.check_entries._load_disabled_symbols", return_value=set()), \
                 patch("core.idle_tracker.idle_tracker.get_orphaned_positions", return_value=[]), \
                 patch("core.check_entries.is_entry_allowed", return_value=True), \
                 patch("core.check_entries.is_entry_candidate_still_valid", return_value=(True, "ok")), \
                 patch("core.check_entries._calc_sl_tp", return_value=(0.4, 1.0, 2.0, 2.0)), \
                 patch("core.check_entries._ma_candidate_quality", return_value=(True, "ok", 30.0)), \
                 patch("core.config.ENTRY_STRICTNESS_MODE", "relaxed"):
                await check_entries()
                await asyncio.sleep(0.05)
                mock_exec.assert_called_once()
                allocation = mock_exec.call_args.args[3]
                self.assertLessEqual(allocation, 0.55)
                self.assertIsNone(STATES[sym].get("entry_reason"))
                self.assertNotIn("_pending_entry_route", STATES[sym])

        asyncio.run(run_check())

    def test_ample_liquidity_does_not_discount_allocation(self):
        from unittest.mock import patch, AsyncMock
        import asyncio
        sym = self._setup_liquidity_discount_state(vol_ma20=200.0)  # h24 ≈ 5,760,000

        async def run_check():
            mock_exec = AsyncMock(return_value=None)
            with patch("core.orders.execute_order", mock_exec), \
                 patch("core.check_entries.is_daily_loss_halted", return_value=False), \
                 patch("core.check_entries.get_open_position_count", return_value=0), \
                 patch("core.check_entries.get_last_same_side_loss_time", return_value=0.0), \
                 patch("core.check_entries._load_disabled_symbols", return_value=set()), \
                 patch("core.idle_tracker.idle_tracker.get_orphaned_positions", return_value=[]), \
                 patch("core.check_entries.is_entry_allowed", return_value=True), \
                 patch("core.check_entries.is_entry_candidate_still_valid", return_value=(True, "ok")), \
                 patch("core.check_entries._calc_sl_tp", return_value=(0.4, 1.0, 2.0, 2.0)), \
                 patch("core.check_entries._ma_candidate_quality", return_value=(True, "ok", 30.0)), \
                 patch("core.config.ENTRY_STRICTNESS_MODE", "relaxed"):
                await check_entries()
                await asyncio.sleep(0.05)
                mock_exec.assert_called_once()
                allocation = mock_exec.call_args.args[3]
                self.assertGreater(allocation, 0.6)

        asyncio.run(run_check())




if __name__ == "__main__":
    unittest.main()
