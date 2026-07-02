import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.orders import should_recover_from_reversal


class ReversalRecoveryTests(unittest.TestCase):
    def test_reverse_signal_triggers_recovery(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 99.0
        s["open_time"] = 0.0
        s["current_atr"] = 0.5
        s["prev_macd_line"] = 0.2
        s["prev_macd_signal"] = 0.1
        s["macd_line"] = -0.3
        s["macd_signal"] = 0.1
        s["trade_signal_strength"] = 3.0
        s["trade_signal_reason"] = "即時大額成交"
        s["ohlcv"] = [
            [0, 100, 100.5, 99.5, 100.0, 1000],
            [1, 100, 100.0, 99.0, 99.0, 1000]
        ]
        s["prev_close"] = 100.0
        s["current_vol"] = 5000
        s["vol_ma20"] = 1000

        decision = should_recover_from_reversal(sym, True)

        self.assertTrue(decision)


if __name__ == "__main__":
    unittest.main()
