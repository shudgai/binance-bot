import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ctx
from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.entry_filter import is_entry_pin_safe, get_entry_strictness_profile, is_entry_allowed


class EntryFilterTests(unittest.TestCase):
    def test_bad_pinbar_rejects_long_entry(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["ohlcv"] = [
            [0, 100, 101, 99, 100, 1000],
            [0, 100, 108, 95, 97, 1000],
        ]
        self.assertFalse(is_entry_pin_safe(sym, "buy"))

    def test_entry_strictness_profile_switches_between_modes(self):
        relaxed = get_entry_strictness_profile("relaxed")
        balanced = get_entry_strictness_profile("balanced")
        strict = get_entry_strictness_profile("strict")

        self.assertLess(relaxed["volume_ratio"], balanced["volume_ratio"])
        self.assertLess(balanced["volume_ratio"], strict["volume_ratio"])
        self.assertGreater(relaxed["pin_threshold"], strict["pin_threshold"])

    def test_relaxed_profile_allows_more_loose_entry_conditions(self):
        profile = get_entry_strictness_profile("relaxed")
        self.assertLess(profile["volume_ratio"], 0.5)
        self.assertGreater(profile["pin_threshold"], 2.5)

    def test_extreme_reversal_bypasses_strict_structure_gate(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)

        s["close_price"] = 1.0
        s["current_vol"] = 1200.0
        s["vol_ma20"] = 1000.0
        s["current_atr"] = 0.01
        s["atr_history"] = [0.01] * 10
        s["current_rsi"] = 80.0
        s["ema20_15m"] = 0.0
        s["ema50_15m"] = 0.0
        s["rsi_history"] = [80.0] * 10
        s["ohlcv"] = [
            [0, 1.00, 1.02, 0.98, 1.00, 1000],
            [0, 1.00, 1.01, 0.99, 0.995, 1000],
            [0, 0.99, 1.00, 0.985, 0.989, 1000],
        ]

        self.assertTrue(is_entry_allowed(sym, "buy", route="Extreme_Reversal", strength=16.6))

    def test_strong_signal_with_mild_atr_spike_is_allowed(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)

        ctx.MARKET_WIND["allow_long"] = True
        ctx.MARKET_WIND["allow_short"] = True
        ctx.MARKET_WIND["btc_trend_4h"] = None
        ctx.MARKET_WIND["btc_trend_1h"] = None

        s["close_price"] = 1.0
        s["current_vol"] = 1200.0
        s["vol_ma20"] = 1000.0
        s["current_atr"] = 0.00195
        s["atr_history"] = [0.00096] * 20
        s["current_rsi"] = 80.0
        s["ema20"] = 0.99
        s["ema20_history"] = [0.99] * 3
        s["ema20_15m"] = 0.0
        s["ema50_15m"] = 0.0
        s["ema50_1h"] = 0.0
        s["sma200_15m"] = 0.0
        s["mtf_filter"] = False
        s["bb_up"] = 1.01
        s["bb_down"] = 0.99
        s["rsi_history"] = [80.0] * 10
        s["ohlcv"] = [
            [0, 0.98 + i * 0.0005, 0.99 + i * 0.0005, 0.97 + i * 0.0005, 0.985 + i * 0.0005, 1000]
            for i in range(20)
        ]
        s["macd_line"] = 0.001
        s["macd_signal"] = 0.0
        s["prev_macd_line"] = 0.0005
        s["prev_macd_signal"] = 0.0
        s["prev_macd_hist"] = 0.0

        self.assertTrue(is_entry_allowed(sym, "buy", route="a", strength=27.4))


if __name__ == "__main__":
    unittest.main()
