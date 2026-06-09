import unittest
import multi_coin_bot


class ReversalRecoveryTests(unittest.TestCase):
    def test_reverse_signal_triggers_recovery(self):
        sym = "XRPUSDT"
        s = multi_coin_bot.STATES[sym]
        multi_coin_bot.reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 99.0
        s["open_time"] = 0.0
        s["current_atr"] = 0.5
        s["prev_macd_line"] = 0.2
        s["prev_macd_signal"] = 0.1
        s["macd_line"] = -0.3
        s["macd_signal"] = 0.1
        s["trade_signal_strength"] = 1.8
        s["trade_signal_reason"] = "即時大額成交"
        s["ohlcv"] = [[0, 100, 100, 99, 100, 1000]]
        s["prev_close"] = 100.0
        s["current_vol"] = 5000
        s["vol_ma20"] = 1000

        decision = multi_coin_bot.should_recover_from_reversal(sym, True)

        self.assertTrue(decision)


if __name__ == "__main__":
    unittest.main()
