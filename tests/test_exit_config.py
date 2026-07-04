import unittest
from core import config


class ExitConfigTests(unittest.TestCase):
    def test_default_stop_loss_is_not_too_conservative(self):
        self.assertLessEqual(config.SL_ATR_MULTIPLIER, 1.6)
        self.assertLessEqual(config.HARD_STOP_LOSS_PCT, 0.025)
        self.assertLessEqual(config.COIN_PROFILE_CONFIG["NEARUSDT"]["sl_atr_multiplier"], 3.0)
        self.assertLessEqual(config.COIN_PROFILE_CONFIG["ADAUSDT"]["sl_atr_multiplier"], 1.8)


if __name__ == "__main__":
    unittest.main()
