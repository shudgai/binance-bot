import unittest
import sys
import os
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ctx
from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.indicators import calculate_ema
from core.signal_engine import (compute_signal_strength, compute_range_signal,
    _find_horizontal_zones)
from core.check_entries import (check_entries, _entry_structure_quality,
    _ma_candidate_quality, _range_candidate_quality,
    _ma25_pullback_sample_bonus, _ma_cross_sample_bonus)


class TradeSignalTests(unittest.TestCase):
    def setUp(self):
        self._range_patcher = patch("core.config.RANGE_MODE_ENABLED", True)
        self._range_patcher.start()

    def tearDown(self):
        self._range_patcher.stop()

    def _setup_ma_signal_state(self, *, sym="XRPUSDT", signal_open=100.0, signal_high=101.2,
                               signal_low=99.8, signal_close=101.0,
                               signal_volume=1200.0, vol_ma20=1000.0,
                               ma7=100.2, ma25=100.0, ma99=99.0,
                               prev_ma7=99.9, prev_ma25=100.0, adx=25.0):
        """建立通用的 OHLCV/指標狀態，供不涉及 compute_signal_strength 路線判斷
        的測試使用（例如 _entry_structure_quality 這類獨立於進場路線的檢查）。"""
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
            "adx": adx, "prev_adx": adx,
        })
        return sym

    def _setup_breakout_state(self, *, sym="BREAKUSDT", direction="long",
                               decline_n=50, decline_step=0.05,
                               reversal_n=3, reversal_step=2.5, spike=3.0,
                               rsi=None, volume_ratio=1.5):
        """建立「先盤整/反向、最近幾根才剛急拉反轉」的 K 棒歷史 + 一根即時突破的
        live 蠟燭。刻意分兩段（長時間平緩 + 最近 reversal_n 根急拉）而不是單純
        單向直線，是因為 SuperTrend 新鮮度濾網要求方向必須在最近幾根已收盤K棒
        內才剛翻轉，單向直線走了 50 幾根的話 SuperTrend 早就翻過去、不算新鮮。
        用來取代舊版 MA_Cross/Breakout/Pullback/MA7_Simple 的測試資料。"""
        init_states([sym])
        reset_coin_state(sym)
        base = []
        price = 100.0
        i = 0
        for _ in range(decline_n):
            price += -decline_step if direction == "long" else decline_step
            base.append([i, price + 0.1, price + 0.3, price - 0.3, price, 1000.0])
            i += 1
        for _ in range(reversal_n):
            price += reversal_step if direction == "long" else -reversal_step
            base.append([i, price - 0.1, price + 0.3, price - 0.3, price, 1000.0])
            i += 1
        last_close = base[-1][4]
        spike_close = last_close + spike if direction == "long" else last_close - spike
        live = [i, last_close, max(last_close, spike_close) + 0.5,
                min(last_close, spike_close) - 0.5, spike_close, 1200.0]
        ohlcv = base + [live]
        closes_completed = [c[4] for c in base]
        default_rsi = 60.0 if direction == "long" else 40.0
        vol_ma20 = 1000.0
        STATES[sym].update({
            "status": "ACTIVE", "ohlcv": ohlcv, "close_price": spike_close,
            "current_rsi": rsi if rsi is not None else default_rsi,
            "ema20": calculate_ema(closes_completed, 20),
            "ema50": calculate_ema(closes_completed, 50),
            "vol_ma20": vol_ma20, "current_vol": vol_ma20 * volume_ratio,
        })
        return sym

    def test_long_keltner_supertrend_breakout_triggers(self):
        sym = self._setup_breakout_state(direction="long")
        side, strength, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("buy", "Keltner_SuperTrend"))
        self.assertGreaterEqual(strength, 25.0)

    def test_short_keltner_supertrend_breakout_triggers(self):
        sym = self._setup_breakout_state(direction="short")
        side, strength, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("sell", "Keltner_SuperTrend"))
        self.assertGreaterEqual(strength, 25.0)

    def test_no_breakout_produces_no_signal(self):
        sym = "FLATUSDT"
        init_states([sym])
        reset_coin_state(sym)
        flat_base = [[i, 100.0, 100.3, 99.7, 100.0, 1000.0] for i in range(55)]
        STATES[sym].update({"status": "ACTIVE", "ohlcv": flat_base, "current_rsi": 50.0})
        closes_flat = [c[4] for c in flat_base[:-1]]
        STATES[sym]["ema20"] = calculate_ema(closes_flat, 20)
        STATES[sym]["ema50"] = calculate_ema(closes_flat, 50)
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))
        self.assertIn("等待 Keltner 突破", STATES[sym]["entry_block_reason"])

    def test_long_breakout_rejected_when_ema20_below_ema50(self):
        # 價格突破 Keltner 上軌 + SuperTrend 轉多，但 EMA20 < EMA50（波段趨勢仍偏空）
        # 時不該放行——這是使用者要求新增的「動態波段過濾」。
        sym = self._setup_breakout_state(direction="long")
        STATES[sym]["ema50"] = STATES[sym]["ema20"] + 5.0
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))

    def test_long_breakout_rejected_when_rsi_below_45(self):
        # RSI 動能防守：即使通道與 SuperTrend 都符合，RSI 太低代表上漲動能不足。
        sym = self._setup_breakout_state(direction="long", rsi=30.0)
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))

    def test_short_breakout_rejected_when_rsi_above_55(self):
        sym = self._setup_breakout_state(direction="short", rsi=70.0)
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))

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
        sym = self._setup_breakout_state(sym="LIQUSDT", direction="long")
        ctx.ALL_SYMBOLS[:] = [sym]
        ctx.MARKET_WIND.update({
            "btc_trend_1h": "BULL", "btc_trend_4h": "BULL",
            "btc_macro_updated_at": time.time(),
        })
        s = STATES[sym]
        s.update({"adx": 30.0, "prev_adx": 28.0})
        from core.symbol_profile import SYMBOL_PROFILES
        SYMBOL_PROFILES[sym] = {"_trade_eligible": True}
        s.update({
            "macd_hist": 0.0, "ema50_1h": 101.0, "current_atr": 0.4,
            "sma200_15m": 101.0, "bb_low": 95.0, "bb_up": 105.0,
            "current_vol": vol_ma20 * 1.5, "vol_ma20": vol_ma20,
            "atr_history": [0.4] * 10,
            "qty": 0.0, "pending_side": None,
        })
        return sym

    def test_marginal_liquidity_discounts_allocation(self):
        # 使用者要求：流動性檢查現有的門檻是二選一（過 1,000,000 全額進場、沒過整筆
        # 擋掉），但「剛好壓線過關」風險比「流動性充裕」高很多，不該用同樣的倉位。
        # 這裡驗證估算 24H 交易額剛過門檻（約 1,150,000）時，分配到的資金比例會被
        # 打折到約 5 折附近，而不是跟流動性充裕時一樣的滿額。
        from unittest.mock import patch, AsyncMock
        import asyncio
        sym = self._setup_liquidity_discount_state(vol_ma20=32.0)  # h24 ≈ 1,150,000 (cp=125)

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

    def test_long_entry_too_close_to_resistance_is_rejected(self):
        sym = self._setup_ma_signal_state(signal_close=100.45)
        state = STATES[sym]
        state["current_atr"] = 0.2
        ok, reason, _ = _entry_structure_quality(sym, "buy", "Keltner_SuperTrend", 100.45)
        self.assertFalse(ok)
        self.assertIn("阻力", reason)

    def test_long_entry_with_resistance_room_is_allowed(self):
        sym = self._setup_ma_signal_state(signal_close=99.0)
        state = STATES[sym]
        state["current_atr"] = 0.2
        ok, _, score = _entry_structure_quality(sym, "buy", "Keltner_SuperTrend", 99.0)
        self.assertTrue(ok)
        self.assertGreater(score, 0.0)


if __name__ == "__main__":
    unittest.main()
