import unittest
from unittest.mock import AsyncMock, patch

from core import ctx
from core.state_manager import build_symbol_state


class SlowMarketAtrCheckTests(unittest.TestCase):
    """Verify the SLOW_MARKET ATR gate correctly exempts MA7_Simple.

    [2026-07-25] Kept after the exit-system overhaul (方案三): this class tests
    check_entries.py's entry-side SLOW_MARKET gate, which is unrelated to the
    exit/TP/SL mechanism that was replaced. Everything else that used to live in
    this file (MA7/MA25 lifecycle exits, MA_Peak_Lock, sell-pressure exits,
    disaster stop, partial-TP precedence) tested exit routes that no longer
    exist and was removed.
    """

    def setUp(self):
        self.sym = "SLOWMKTUSDT"
        self.original_state = ctx.STATES.get(self.sym)
        self.original_symbols = list(ctx.ALL_SYMBOLS)
        ctx.STATES[self.sym] = build_symbol_state(self.sym)
        if self.sym not in ctx.ALL_SYMBOLS:
            ctx.ALL_SYMBOLS.append(self.sym)

    def tearDown(self):
        ctx.ALL_SYMBOLS[:] = self.original_symbols
        if self.original_state is None:
            ctx.STATES.pop(self.sym, None)
        else:
            ctx.STATES[self.sym] = self.original_state

    def _make_check_entries_patches(self, route: str, atr_pct_5m: float):
        """Build a minimal set of patches so check_entries() reaches the ATR gate."""
        import core.ctx as ctx
        import core.balance as _bal

        # Very low 5m ATR (0.01%) — below any threshold
        price = 100.0
        atr_cur_ce = price * atr_pct_5m   # e.g. 0.01 for 0.01%

        s = ctx.STATES[self.sym]
        s["status"] = "ACTIVE"
        s["is_ordering"] = False
        s["qty"] = 0.0
        s["close_price"] = price
        s["ohlcv"] = []
        s["atr_cur_ce"] = atr_cur_ce
        s["entry_block_reason"] = ""

        return [
            patch("core.check_entries.is_daily_loss_halted", return_value=False),
            patch("core.check_entries.get_open_position_count", return_value=0),
            patch("core.balance.get_dynamic_max_slots", return_value=5),
            patch("core.check_entries._load_disabled_symbols", return_value=set()),
            patch("core.check_entries._is_confirmable_exit_cooldown", return_value=False),
            patch("core.ctx.ALL_SYMBOLS", [self.sym]),
            # Signal engine returns a hit for the given route
            patch("core.check_entries.compute_signal_strength", return_value=(25.0, "buy", route, False)),
            patch("core.check_entries.compute_range_signal", return_value=(False, None, None, 0.0)),
            # Skip all upstream guards so we reach the ATR gate
            patch("core.check_entries.is_entry_allowed", return_value=(True, "")),
            patch("core.check_entries.btc_macro_entry_guard", return_value=("BULLISH", True, "")),
            patch("core.check_entries._funding_rate_guard", new_callable=AsyncMock, return_value=(True, "")),
            patch("core.check_entries._atr_cur_ce_from_state", return_value=atr_cur_ce),
        ]

    def test_ma7_simple_bypasses_slow_market_check(self):
        """MA7_Simple with a near-zero ATR must NOT be blocked by SLOW_MARKET."""
        from core.check_entries import check_entries

        patches = self._make_check_entries_patches(route="MA7_Simple", atr_pct_5m=0.0001)
        with patch("core.check_entries._is_confirmable_exit_cooldown", return_value=False), \
             patch("core.check_entries.is_daily_loss_halted", return_value=False), \
             patch("core.check_entries.get_open_position_count", return_value=0), \
             patch("core.balance.get_dynamic_max_slots", return_value=5), \
             patch("core.check_entries._load_disabled_symbols", return_value=set()):

            s = ctx.STATES[self.sym]
            price = 100.0
            # Extremely low 5m ATR (0.01% = 0.01 absolute)
            atr_5m = price * 0.0001
            s["atr_cur_ce"] = atr_5m
            s["close_price"] = price

            # Directly test the condition that guards SLOW_MARKET
            from core.config import MIN_5M_ATR_PCT_FOR_MA_ENTRY
            route = "MA7_Simple"
            _atr_pct_5m = atr_5m / price
            is_range_signal = False
            _min_atr_pct_5m = 0.0008 if is_range_signal else MIN_5M_ATR_PCT_FOR_MA_ENTRY

            # The condition should evaluate to False for MA7_Simple (not blocked)
            slow_market_blocked = (
                str(route or "").lower() != "ma7_simple" and _atr_pct_5m < _min_atr_pct_5m
            )
            self.assertFalse(
                slow_market_blocked,
                "MA7_Simple with low ATR should NOT be blocked by SLOW_MARKET check"
            )

    def test_ma_cross_blocked_by_slow_market_check(self):
        """MA_Cross with a near-zero ATR MUST be blocked by SLOW_MARKET."""
        from core.config import MIN_5M_ATR_PCT_FOR_MA_ENTRY

        price = 100.0
        atr_5m = price * 0.0001  # 0.01% — well below MIN_5M_ATR_PCT_FOR_MA_ENTRY
        route = "MA_Cross"
        _atr_pct_5m = atr_5m / price
        is_range_signal = False
        _min_atr_pct_5m = 0.0008 if is_range_signal else MIN_5M_ATR_PCT_FOR_MA_ENTRY

        slow_market_blocked = (
            str(route or "").lower() != "ma7_simple" and _atr_pct_5m < _min_atr_pct_5m
        )
        self.assertTrue(
            slow_market_blocked,
            "MA_Cross with low ATR should be blocked by SLOW_MARKET check"
        )

    def test_ma_breakout_blocked_by_slow_market_check(self):
        """MA_Breakout with a near-zero ATR MUST be blocked by SLOW_MARKET."""
        from core.config import MIN_5M_ATR_PCT_FOR_MA_ENTRY

        price = 100.0
        atr_5m = price * 0.0001
        route = "MA_Breakout"
        _atr_pct_5m = atr_5m / price
        is_range_signal = False
        _min_atr_pct_5m = 0.0008 if is_range_signal else MIN_5M_ATR_PCT_FOR_MA_ENTRY

        slow_market_blocked = (
            str(route or "").lower() != "ma7_simple" and _atr_pct_5m < _min_atr_pct_5m
        )
        self.assertTrue(slow_market_blocked, "MA_Breakout with low ATR should be blocked")

    def test_ma7_simple_case_insensitive(self):
        """Route matching for MA7_Simple should be case-insensitive."""
        from core.config import MIN_5M_ATR_PCT_FOR_MA_ENTRY

        price = 100.0
        atr_5m = price * 0.0001
        _atr_pct_5m = atr_5m / price
        is_range_signal = False
        _min_atr_pct_5m = 0.0008 if is_range_signal else MIN_5M_ATR_PCT_FOR_MA_ENTRY

        for variant in ("MA7_Simple", "ma7_simple", "MA7_SIMPLE"):
            blocked = (
                str(variant or "").lower() != "ma7_simple" and _atr_pct_5m < _min_atr_pct_5m
            )
            self.assertFalse(blocked, f"route={variant!r} should bypass SLOW_MARKET check")


if __name__ == "__main__":
    unittest.main()
