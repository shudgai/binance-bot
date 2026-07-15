import unittest
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ctx
from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.signal_engine import compute_signal_strength
from core.check_entries import check_entries, _entry_structure_quality, _ma_candidate_quality


class TradeSignalTests(unittest.TestCase):
    def _setup_ma_signal_state(self, *, signal_open=100.0, signal_high=101.2,
                               signal_low=99.8, signal_close=101.0,
                               signal_volume=1200.0, vol_ma20=1000.0,
                               ma7=100.2, ma25=100.0, ma99=99.0,
                               prev_ma7=99.9, prev_ma25=100.0):
        sym = "XRPUSDT"
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
        })
        return sym

    def test_confirmed_golden_cross_opens_long_above_ma99(self):
        sym = self._setup_ma_signal_state()
        side, strength, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("buy", "MA_Cross"))
        self.assertGreaterEqual(strength, 25.0)

    def test_golden_cross_below_ma99_is_rejected(self):
        sym = self._setup_ma_signal_state(ma99=102.0)
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))
        self.assertIn("MA99 下方", STATES[sym]["entry_block_reason"])

    def test_confirmed_death_cross_opens_short_below_ma99(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=99.0, signal_low=98.8,
            ma7=99.7, ma25=100.0, ma99=101.0,
            prev_ma7=100.2, prev_ma25=100.0,
        )
        side, strength, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("sell", "MA_Cross"))
        self.assertGreaterEqual(strength, 25.0)

    def test_cross_without_volume_is_rejected(self):
        sym = self._setup_ma_signal_state(signal_volume=500.0)
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))
        self.assertIn("量能過低", STATES[sym]["entry_block_reason"])

    def test_ma25_pullback_enters_only_after_bullish_rejection(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.1, signal_close=100.3, signal_low=99.9,
            ma7=100.6, ma25=100.0, ma99=99.0,
            prev_ma7=100.4, prev_ma25=99.9,
            signal_volume=900.0,
        )
        side, _, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("buy", "MA25_Pullback"))

    def test_golden_cross_above_ma99_does_not_require_ma25_above_ma99_yet(self):
        sym = self._setup_ma_signal_state(ma99=100.1)
        side, _, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("buy", "MA_Cross"))

    def test_death_cross_below_ma99_does_not_require_ma25_below_ma99_yet(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=99.0, signal_low=98.8,
            ma7=99.7, ma25=100.0, ma99=99.5,
            prev_ma7=100.2, prev_ma25=100.0,
        )
        side, _, route = compute_signal_strength(sym)
        self.assertEqual((side, route), ("sell", "MA_Cross"))

    def test_flat_intertwined_ma_is_blocked(self):
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=100.0, signal_volume=900.0,
            ma7=100.02, ma25=100.0, ma99=99.0,
            prev_ma7=100.01, prev_ma25=100.0,
        )
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))
        self.assertIn("平走交織", STATES[sym]["entry_block_reason"])

    def _setup_liquidity_discount_state(self, vol_ma20):
        sym = self._setup_ma_signal_state(
            signal_open=100.0, signal_close=99.0, signal_low=98.8,
            signal_volume=vol_ma20 * 1.5, vol_ma20=vol_ma20,
            ma7=99.7, ma25=100.0, ma99=101.0,
            prev_ma7=100.2, prev_ma25=100.0,
        )
        ctx.ALL_SYMBOLS[:] = [sym]
        ctx.MARKET_WIND.update({
            "btc_trend_1h": "BEAR", "btc_trend_4h": "BEAR",
            "btc_macro_updated_at": time.time(),
        })
        s = STATES[sym]
        for candle in s["ohlcv"][:-2]:
            candle[3] = 98.0
        from core.symbol_profile import SYMBOL_PROFILES
        SYMBOL_PROFILES[sym] = {"_trade_eligible": True}
        s.update({
            "current_rsi": 50.0, "macd_hist": 0.0,
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
                 patch("core.balance.is_daily_loss_halted", return_value=False), \
                 patch("core.check_entries.is_entry_allowed", return_value=True), \
                 patch("core.config.ENTRY_STRICTNESS_MODE", "relaxed"):
                await check_entries()
                await asyncio.sleep(0.05)
                mock_exec.assert_called_once()
                allocation = mock_exec.call_args.args[3]
                self.assertLess(allocation, 0.5)

        asyncio.run(run_check())

    def test_ample_liquidity_does_not_discount_allocation(self):
        from unittest.mock import patch, AsyncMock
        import asyncio
        sym = self._setup_liquidity_discount_state(vol_ma20=200.0)  # h24 ≈ 5,760,000

        async def run_check():
            mock_exec = AsyncMock(return_value=None)
            with patch("core.orders.execute_order", mock_exec), \
                 patch("core.balance.is_daily_loss_halted", return_value=False), \
                 patch("core.check_entries.is_entry_allowed", return_value=True), \
                 patch("core.config.ENTRY_STRICTNESS_MODE", "relaxed"):
                await check_entries()
                await asyncio.sleep(0.05)
                mock_exec.assert_called_once()
                allocation = mock_exec.call_args.args[3]
                self.assertGreater(allocation, 0.6)

        asyncio.run(run_check())




if __name__ == "__main__":
    unittest.main()
