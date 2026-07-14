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
        from core import config
        original_mode = config.ENTRY_STRICTNESS_MODE
        config.ENTRY_STRICTNESS_MODE = "balanced"
        try:
            sym = "XRPUSDT"
            init_states([sym])
            s = STATES[sym]
            reset_coin_state(sym)
            s["ohlcv"] = [
                [0, 100, 101, 99, 100, 1000],
                [0, 100, 108, 95, 97, 1000],
            ]
            self.assertFalse(is_entry_pin_safe(sym, "buy"))
        finally:
            config.ENTRY_STRICTNESS_MODE = original_mode

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

    def test_extreme_reversal_rejects_falling_knife_long(self):
        sym = "SUIUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["close_price"] = 0.742
        s["current_vol"] = 1200.0
        s["vol_ma20"] = 1000.0
        s["current_atr"] = 0.001
        s["atr_history"] = [0.001] * 10
        s["current_rsi"] = 25.0
        s["macd_line"] = -0.0004
        s["macd_signal"] = -0.0003
        s["ohlcv"] = [
            [0, 0.750, 0.751, 0.746, 0.747, 1000],
            [0, 0.747, 0.748, 0.741, 0.742, 1200],
        ]
        self.assertFalse(is_entry_allowed(sym, "buy", route="Extreme_Reversal", strength=18.0))

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

    def test_short_entry_is_blocked_in_btc_bull_mid_rsi_zone(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)

        ctx.MARKET_WIND["allow_long"] = True
        ctx.MARKET_WIND["allow_short"] = True
        ctx.MARKET_WIND["btc_trend_4h"] = "BULL"
        ctx.MARKET_WIND["btc_trend_1h"] = None

        s["close_price"] = 1.0
        s["current_vol"] = 1200.0
        s["vol_ma20"] = 1000.0
        s["current_atr"] = 0.0018
        s["atr_history"] = [0.0010] * 20
        s["current_rsi"] = 61.0
        s["ema20"] = 0.99
        s["ema20_history"] = [1.01] * 3
        s["ema20_15m"] = 1.02
        s["ema50_15m"] = 1.00
        s["ema50_1h"] = 0.0
        s["sma200_15m"] = 0.0
        s["mtf_filter"] = False
        s["bb_up"] = 1.00
        s["bb_down"] = 0.99
        s["rsi_history"] = [61.0] * 10
        s["ohlcv"] = [
            [0, 0.98 + i * 0.0005, 0.99 + i * 0.0005, 0.97 + i * 0.0005, 0.985 + i * 0.0005, 1000]
            for i in range(19)
        ]
        s["ohlcv"].append([0, 0.995, 0.996, 0.992, 0.993, 1000])
        s["macd_line"] = 0.001
        s["macd_signal"] = 0.0
        s["prev_macd_line"] = 0.0005
        s["prev_macd_signal"] = 0.0
        s["prev_macd_hist"] = 0.0

        # BTC 4H 多頭且 RSI 只有 61（中段區間，未達 73 極端超買），逆勢空單應該被
        # BULL_DEFENSE 擋下——這條中段區間豁免已經因為實測拖累空單勝率而移除。
        self.assertFalse(is_entry_allowed(sym, "sell", route="a", strength=20.5))

    def test_direction_safety_blocks_weak_opposite_candle(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)

        ctx.MARKET_WIND["allow_long"] = True
        ctx.MARKET_WIND["allow_short"] = True
        ctx.MARKET_WIND["btc_trend_4h"] = None
        ctx.MARKET_WIND["btc_trend_1h"] = None

        s["close_price"] = 1.00
        s["current_vol"] = 1200.0
        s["vol_ma20"] = 1000.0
        s["current_atr"] = 0.001
        s["atr_history"] = [0.001] * 20
        s["current_rsi"] = 40.0
        s["ema20"] = 0.98
        s["ema20_history"] = [0.98] * 3
        s["ema20_15m"] = 0.0
        s["ema50_15m"] = 0.0
        s["ema50_1h"] = 0.0
        s["sma200_15m"] = 0.0
        s["mtf_filter"] = False
        s["bb_up"] = 1.02
        s["bb_down"] = 0.98
        s["macd_line"] = 0.001
        s["macd_signal"] = 0.0
        s["prev_macd_line"] = 0.0005
        s["prev_macd_signal"] = 0.0
        s["prev_macd_hist"] = 0.0
        s["rsi_history"] = [40.0] * 20
        s["ohlcv"] = [
            [0, 1.00, 1.01, 0.99, 1.00, 1000.0]
            for _ in range(18)
        ]
        s["ohlcv"].append([0, 1.00, 1.02, 0.99, 1.01, 1000.0])
        s["ohlcv"].append([0, 1.01, 1.02, 0.995, 0.99, 1000.0])

        self.assertFalse(is_entry_allowed(sym, "buy", route="a", strength=18.5))


    def test_narrow_band_does_not_turn_entire_band_into_support_or_resistance(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        ctx.MARKET_WIND.update({"allow_long": True, "allow_short": True, "btc_trend_4h": None, "btc_trend_1h": None})
        s.update({
            "close_price": 100.5, "bb_low": 100.0, "bb_up": 101.0,
            "current_vol": 1200.0, "vol_ma20": 1000.0, "current_atr": 0.2,
            "atr_history": [0.2] * 20, "current_rsi": 50.0,
            "ema20_15m": 0.0, "ema50_15m": 0.0, "mtf_filter": False,
            "ohlcv": [[0, 100.4, 100.6, 100.3, 100.5, 1200]] * 21,
        })
        self.assertFalse(is_entry_allowed(sym, "buy", route="a", strength=30.0))
        self.assertFalse(is_entry_allowed(sym, "sell", route="a", strength=30.0))

    def test_high_strength_cannot_short_from_lower_band(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        ctx.MARKET_WIND.update({"allow_long": True, "allow_short": True, "btc_trend_4h": None, "btc_trend_1h": None})
        s.update({
            "close_price": 102.5, "bb_low": 100.0, "bb_up": 110.0,
            "current_vol": 1200.0, "vol_ma20": 1000.0,
            "current_atr": 0.5, "atr_history": [0.5] * 20,
            "current_rsi": 41.9, "ema20_15m": 101.0, "ema50_15m": 100.0,
            "macd_line": -0.1, "macd_signal": 0.0,
            "mtf_filter": False, "ohlcv": [[0, 100, 104, 99, 102.5, 1200]] * 21,
        })
        self.assertFalse(is_entry_allowed(sym, "sell", route="a", strength=30.2))

    def test_high_strength_cannot_override_opposite_15m_trend(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        ctx.MARKET_WIND.update({"allow_long": True, "allow_short": True, "btc_trend_4h": None, "btc_trend_1h": None})
        s.update({
            "close_price": 109.0, "bb_low": 100.0, "bb_up": 110.0,
            "current_vol": 1200.0, "vol_ma20": 1000.0,
            "current_atr": 0.5, "atr_history": [0.5] * 20,
            "current_rsi": 42.0, "ema20_15m": 108.0, "ema50_15m": 105.0,
            "mtf_filter": True, "ohlcv": [[0, 109, 110, 108, 109, 1200]] * 21,
        })
        self.assertFalse(is_entry_allowed(sym, "sell", route="a", strength=30.2))


if __name__ == "__main__":
    unittest.main()
