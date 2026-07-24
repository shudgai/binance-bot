import unittest
import numpy as np
from core.indicators import get_market_regime_config, _calc_sl_tp


class TestMarketRegimeDetection(unittest.TestCase):
    def test_market_regime_thresholds(self):
        # Ultra-Low: ratio < 0.7
        res_low = get_market_regime_config(current_atr=60.0, avg_atr=100.0)
        self.assertEqual(res_low["regime"], "Ultra-Low")
        self.assertEqual(res_low["min_rr"], 0.2)
        self.assertAlmostEqual(res_low["vol_ratio"], 0.6)

        # Normal: 0.7 <= ratio <= 1.3
        res_normal = get_market_regime_config(current_atr=100.0, avg_atr=100.0)
        self.assertEqual(res_normal["regime"], "Normal")
        self.assertEqual(res_normal["min_rr"], 0.5)
        self.assertAlmostEqual(res_normal["vol_ratio"], 1.0)

        # High: ratio > 1.3
        res_high = get_market_regime_config(current_atr=150.0, avg_atr=100.0)
        self.assertEqual(res_high["regime"], "High")
        self.assertEqual(res_high["min_rr"], 0.6)
        self.assertAlmostEqual(res_high["vol_ratio"], 1.5)

    def test_calc_sl_tp_regime_integration(self):
        state = {
            "current_atr": 0.5,
            "atr_history": [1.0] * 24, # avg_atr = 1.0 -> ratio = 0.5 (Ultra-Low)
            "close_price": 100.0
        }
        atr_val, sl_dist, tp_dist, expected_rr = _calc_sl_tp("BTCUSDT", "buy", state, 100.0)
        self.assertEqual(state.get("market_regime"), "Ultra-Low")
        self.assertEqual(state.get("regime_min_rr"), 0.2)
        self.assertAlmostEqual(state.get("vol_ratio"), 0.5)
        self.assertGreaterEqual(expected_rr, 0.2)


if __name__ == "__main__":
    unittest.main()
