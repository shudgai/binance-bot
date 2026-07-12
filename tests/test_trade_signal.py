import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.exits import detect_market_regime
from core.signal_engine import compute_signal_strength
from core.check_entries import check_entries


class TradeSignalTests(unittest.TestCase):
    def _setup_ema20_pullback_state(self, rsi, macd_line, macd_signal, prev_macd_line, prev_macd_signal):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s.update({
            "closes": [100.0] * 20,
            "close_price": 100.0,
            "prev_close": 100.2,
            "current_rsi": rsi,
            "rsi_extreme_low": 20, "rsi_extreme_high": 75,
            "ema20": 100.5, "ema50": 101.0,
            "sma200_15m": 105.0,
            "bb_low": 90.0, "bb_up": 110.0,
            "macd_line": macd_line, "macd_signal": macd_signal,
            "prev_macd_line": prev_macd_line, "prev_macd_signal": prev_macd_signal,
            "vol_ma10": 0.0, "current_vol": 0.0,
            "vol_ma20": 1000.0,
            "current_atr": 0.3,
            "atr_history": [0.3] * 10,
            "ohlcv": [
                [0, 101.0, 101.5, 99.0, 100.5, 1000],
                [0, 100.5, 100.8, 99.5, 100.0, 1000],
                [0, 100.0, 100.3, 99.7, 100.1, 1000],
            ],
            "rsi_history": [rsi, rsi],
        })
        return sym

    def test_ema20_pullback_short_rejects_borderline_rsi_and_fresh_macd_cross(self):
        # ADAUSDT/SUIUSDT 實測案例：RSI 才 55~58（中性偏多，不是超買）、MACD 柱狀圖
        # 只有 -0.0002（剛翻負一點點，前一根還是正的，等於是剛穿越）。兩筆單都在做空
        # 進場沒多久就被推回原方向，小虧收場。這種「RSI 沒逆勢就好、MACD 剛翻負就算數」
        # 的訊號品質太邊緣，不該進場——Route A 跟 Route B 共用同一組 macd_ok_short/
        # rsi_ok_short，所以兩條路線都要一起擋，不能只改 Route B（不然只是換個
        # route_tag，訊號一樣會從 Route A 溜出去）。
        sym = self._setup_ema20_pullback_state(
            rsi=58.5, macd_line=-0.0014, macd_signal=-0.0012,
            prev_macd_line=-0.0010, prev_macd_signal=-0.0011,
        )
        self.assertEqual(compute_signal_strength(sym), (None, 0, None))

    def test_ema20_pullback_short_allows_confirmed_overbought_and_sustained_macd(self):
        sym = self._setup_ema20_pullback_state(
            rsi=62.0, macd_line=-0.006, macd_signal=-0.003,
            prev_macd_line=-0.004, prev_macd_signal=-0.003,
        )
        side, strength, route = compute_signal_strength(sym)
        self.assertEqual(side, "sell")
        self.assertGreater(strength, 0)

    def test_trade_signal_triggers_breakout_reversal(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["ohlcv"] = [
            [0, 100, 100, 99, 100, 1200],
            [0, 101, 101, 100, 101, 1300],
            [0, 102, 102, 101, 102, 1400],
            [0, 103, 103, 102, 103, 1500],
            [0, 104, 104, 103, 104, 1600],
            [0, 105, 105, 104, 105, 1700],
            [0, 106, 106, 105, 106, 1800],
            [0, 107, 107, 106, 107, 1900],
            [0, 108, 108, 107, 108, 2000],
            [0, 109, 109, 108, 109, 2100],
            [0, 110, 110, 109, 110, 2200],
            [0, 111, 111, 110, 111, 2300],
            [0, 112, 112, 111, 112, 2400],
            [0, 113, 113, 112, 113, 2500],
            [0, 114, 114, 113, 114, 2600],
            [0, 115, 115, 114, 115, 2700],
            [0, 116, 116, 115, 116, 2800],
            [0, 117, 117, 116, 117, 2900],
            [0, 118, 118, 117, 118, 3000],
            [0, 119, 119, 118, 119, 3100],
        ]
        s["current_atr"] = 0.5
        s["current_vol"] = 3000
        s["vol_ma20"] = 1000
        s["prev_close"] = 119
        s["trade_signal_strength"] = 3.0
        s["trade_signal_reason"] = "即時成交異常"

        decision, reason = detect_market_regime(sym, 121.0, 120.0, False)

        self.assertEqual(decision, "BREAKOUT_REVERSAL")
        self.assertIn("即時大額成交", reason)

    def test_compute_signal_strength_rejects_counter_trend_signal(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["close_price"] = 100.0
        s["prev_close"] = 99.0
        s["current_rsi"] = 35.0
        s["bb_low"] = 99.0
        s["bb_up"] = 101.0
        s["ema20"] = 102.0
        s["ema50"] = 105.0
        s["macd_line"] = 0.2
        s["macd_signal"] = 0.1
        s["prev_macd_line"] = 0.05
        s["prev_macd_signal"] = 0.1

        res = compute_signal_strength(sym)
        side, strength = res[0], res[1]

        self.assertIsNone(side)
        self.assertEqual(strength, 0)

    def test_check_entries_handles_missing_macd_tiny_threshold(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["status"] = "ACTIVE"
        s["close_price"] = 100.0
        s["current_rsi"] = 40.0
        s["macd_hist"] = 0.0
        s["vol_ma20"] = 1000.0
        s["current_vol"] = 1100.0
        s["sma200_15m"] = 99.0
        s["ema50_1h"] = 99.0
        s["atr_history"] = [0.1, 0.1, 0.1]
        s["current_atr"] = 0.1
        s["ohlcv"] = [
            [0, 90, 95, 90, 95, 1000],
            [0, 91, 96, 91, 96, 1100],
            [0, 92, 97, 92, 97, 1200],
        ]
        s["qty"] = 0.0
        s["pending_side"] = None
        s["pending_reverse"] = None

        try:
            import asyncio
            asyncio.run(check_entries())
        except Exception as exc:
            self.fail(f"check_entries should not crash: {exc}")


if __name__ == "__main__":
    unittest.main()
