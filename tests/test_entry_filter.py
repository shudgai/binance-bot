import unittest

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.entry_filter import (
    has_strong_local_momentum_override,
    is_entry_allowed,
    is_entry_pin_safe,
    is_last_closed_1m_aligned,
)


class EntryFilterTests(unittest.TestCase):
    def _state(self, side="buy", volume=1300.0):
        sym = "XRPUSDT"
        init_states([sym])
        reset_coin_state(sym)
        if side == "buy":
            ma7, ma25, ma99, price = 101.0, 100.0, 99.0, 101.5
            closed = [1, 100.5, 101.6, 100.2, price, volume]
        else:
            ma7, ma25, ma99, price = 99.0, 100.0, 101.0, 98.5
            closed = [1, 99.5, 99.7, 98.4, price, volume]
        STATES[sym].update({
            "status": "ACTIVE", "ma7": ma7, "ma25": ma25, "ma99": ma99,
            "close_price": price, "vol_ma20": 1000.0, "current_atr": 0.5,
            "ohlcv": [[0, 100.0, 100.2, 99.8, 100.0, 1000.0], closed,
                      [2, price, price, price, price, 1.0]],
        })
        return sym

    def test_short_term_direction_uses_closed_not_live_candle(self):
        state = {"ohlcv": [[1, 100, 101, 98, 99, 1000], [2, 99, 102, 98, 101, 100]]}
        self.assertTrue(is_last_closed_1m_aligned(state, "sell"))
        self.assertFalse(is_last_closed_1m_aligned(state, "buy"))

    def test_only_ma_routes_can_be_strong(self):
        self.assertTrue(has_strong_local_momentum_override("MA_Cross", 25.0))
        self.assertFalse(has_strong_local_momentum_override("legacy", 99.0))

    def test_non_ma_route_is_always_rejected(self):
        sym = self._state()
        self.assertFalse(is_entry_allowed(sym, "buy", route="legacy", strength=99.0))

    def test_valid_ma_long_is_allowed(self):
        sym = self._state("buy")
        self.assertTrue(is_entry_allowed(sym, "buy", route="MA_Cross", strength=25.0))

    def test_valid_ma_short_is_allowed(self):
        sym = self._state("sell")
        self.assertTrue(is_entry_allowed(sym, "sell", route="MA_Cross", strength=25.0))

    def test_wrong_ma99_side_is_rejected(self):
        sym = self._state("buy")
        STATES[sym]["ma99"] = 102.0
        self.assertFalse(is_entry_allowed(sym, "buy", route="MA_Cross", strength=25.0))

    def test_insufficient_closed_volume_is_rejected(self):
        sym = self._state("buy", volume=500.0)
        self.assertFalse(is_entry_allowed(sym, "buy", route="MA_Cross", strength=25.0))

    def test_bad_opposing_wick_is_rejected(self):
        sym = self._state("buy")
        STATES[sym]["ohlcv"][-2] = [1, 100.5, 105.0, 100.2, 101.0, 1300.0]
        self.assertFalse(is_entry_pin_safe(sym, "buy"))


if __name__ == "__main__":
    unittest.main()
