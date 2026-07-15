import unittest
import json
import os
import tempfile
import time

from unittest.mock import Mock, patch

from services.ai_manager import AIManager

from core import ctx
from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.entry_filter import (
    btc_macro_entry_guard,
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
        prev_ma7 = 99.8 if side == "buy" else 100.2
        prev_ma25 = 100.0
        STATES[sym].update({
            "status": "ACTIVE", "ma7": ma7, "ma25": ma25, "ma99": ma99,
            "prev_ma7": prev_ma7, "prev_ma25": prev_ma25,
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

    def test_cross_on_correct_ma99_side_allows_incomplete_long_stack(self):
        sym = self._state("buy")
        STATES[sym]["ma99"] = 100.5
        self.assertTrue(is_entry_allowed(sym, "buy", route="MA_Cross", strength=25.0))

    def test_pullback_still_rejects_incomplete_long_stack(self):
        sym = self._state("buy")
        STATES[sym]["ma99"] = 100.5
        self.assertFalse(is_entry_allowed(sym, "buy", route="MA25_Pullback", strength=25.0))

    def test_adverse_ma25_slope_is_rejected(self):
        sym = self._state("buy")
        STATES[sym]["prev_ma25"] = 100.2
        self.assertFalse(is_entry_allowed(sym, "buy", route="MA_Cross", strength=25.0))

    def test_insufficient_closed_volume_is_rejected(self):
        sym = self._state("buy", volume=500.0)
        self.assertFalse(is_entry_allowed(sym, "buy", route="MA_Cross", strength=25.0))

    def test_bad_opposing_wick_is_rejected(self):
        sym = self._state("buy")
        STATES[sym]["ohlcv"][-2] = [1, 100.5, 105.0, 100.2, 101.0, 1300.0]
        self.assertFalse(is_entry_pin_safe(sym, "buy"))


    def test_btc_dual_bull_allows_alt_long_and_blocks_alt_short(self):
        sym = self._state("buy")
        original = dict(ctx.MARKET_WIND)
        try:
            ctx.MARKET_WIND.update({"btc_trend_1h": "BULL", "btc_trend_4h": "BULL", "btc_macro_updated_at": time.time()})
            self.assertTrue(btc_macro_entry_guard(sym, "buy")[0])
            allowed, reason, mode = btc_macro_entry_guard(sym, "sell")
            self.assertFalse(allowed)
            self.assertEqual(mode, "BULL")
            self.assertIn("禁止", reason)
        finally:
            ctx.MARKET_WIND.clear()
            ctx.MARKET_WIND.update(original)

    def test_btc_dual_bear_allows_alt_short_and_blocks_alt_long(self):
        sym = self._state("sell")
        original = dict(ctx.MARKET_WIND)
        try:
            ctx.MARKET_WIND.update({"btc_trend_1h": "BEAR", "btc_trend_4h": "BEAR", "btc_macro_updated_at": time.time()})
            self.assertTrue(btc_macro_entry_guard(sym, "sell")[0])
            self.assertFalse(btc_macro_entry_guard(sym, "buy")[0])
        finally:
            ctx.MARKET_WIND.clear()
            ctx.MARKET_WIND.update(original)

    def test_btc_mixed_direction_requires_point_eight_rvol(self):
        sym = self._state("buy", volume=700.0)
        original = dict(ctx.MARKET_WIND)
        try:
            ctx.MARKET_WIND.update({"btc_trend_1h": "BULL", "btc_trend_4h": "NEUTRAL", "btc_macro_updated_at": time.time()})
            self.assertFalse(btc_macro_entry_guard(sym, "buy")[0])
            STATES[sym]["ohlcv"][-2][5] = 900.0
            allowed, _, mode = btc_macro_entry_guard(sym, "buy")
            self.assertTrue(allowed)
            self.assertEqual(mode, "MIXED")
        finally:
            ctx.MARKET_WIND.clear()
            ctx.MARKET_WIND.update(original)

    def test_stale_btc_macro_blocks_alt_but_not_btc_itself(self):
        sym = self._state("buy")
        original = dict(ctx.MARKET_WIND)
        try:
            ctx.MARKET_WIND["btc_macro_updated_at"] = time.time() - 181.0
            self.assertFalse(btc_macro_entry_guard(sym, "buy")[0])
            self.assertTrue(btc_macro_entry_guard("BTCUSDT", "buy")[0])
        finally:
            ctx.MARKET_WIND.clear()
            ctx.MARKET_WIND.update(original)


    def test_ai_candidate_adjustment_requires_minimum_sample(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = os.path.join(tmpdir, "history.json")
            with open(history_path, "w", encoding="utf-8") as handle:
                json.dump([
                    {"symbol": "XRPUSDT", "entry_reason": "MA_Cross", "profit_pct": 0.01}
                    for _ in range(4)
                ], handle)
            manager = AIManager()
            manager.history_path = history_path
            self.assertEqual(manager.get_candidate_quality_adjustment("XRPUSDT", "MA_Cross"), 0.0)

    def test_ai_candidate_adjustment_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = os.path.join(tmpdir, "history.json")
            with open(history_path, "w", encoding="utf-8") as handle:
                json.dump([
                    {"symbol": "XRPUSDT", "entry_reason": "MA_Cross", "profit_pct": 0.01}
                    for _ in range(5)
                ], handle)
            manager = AIManager()
            manager.history_path = history_path
            self.assertEqual(manager.get_candidate_quality_adjustment("XRPUSDT", "MA_Cross"), 2.0)

    def test_ai_never_auto_applies_updates(self):
        manager = AIManager()
        diagnoses = [{"symbol": "XRPUSDT", "suggested_params": {"leverage": 3}}]
        self.assertEqual(manager.apply_ai_updates(diagnoses), 0)

    def test_ai_rejects_unknown_parameters(self):
        manager = AIManager()
        result = manager.validate_suggestion(
            "XRPUSDT", {"suggested_params": {"leverage": 3, "entry_side": "sell"}}
        )
        self.assertEqual(result, {"leverage": 3})


    def test_auto_review_becomes_due_after_five_new_closed_trades(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = os.path.join(tmpdir, "report.json")
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump({"analyzed_history_count": 10}, handle)
            manager = AIManager()
            manager.report_path = report_path
            with patch("services.ai_manager.AI_EXTERNAL_REVIEW_ENABLED", True), \
                 patch("services.ai_manager.AI_AUTO_REVIEW_ENABLED", True), \
                 patch("services.ai_manager.AI_AUTO_REVIEW_EVERY_TRADES", 5):
                self.assertFalse(manager.is_auto_review_due(14))
                self.assertTrue(manager.is_auto_review_due(15))

    def test_openai_compatible_review_is_sanitized_and_key_optional(self):
        manager = AIManager()
        response = Mock(status_code=200, text="ok", headers={})
        response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "diagnoses": [
                    {
                        "symbol": "XRPUSDT", "confidence_score": 1.5,
                        "observed_pattern": "pattern", "risk_flags": ["risk"],
                        "review_note": "note", "suggested_params": {"leverage": 10},
                    },
                    {"symbol": "UNKNOWNUSDT", "confidence_score": 1.0},
                ]
            })}}]
        }
        with patch("services.ai_manager.AI_BASE_URL", "http://internal:8888/v1"), \
             patch("services.ai_manager.AI_API_KEY", None), \
             patch("services.ai_manager.requests.post", return_value=response) as post:
            result = manager._fetch_ai_diagnosis([
                {"symbol": "XRPUSDT", "entry_reason": "MA_Cross", "profit_pct": 0.01}
            ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["confidence_score"], 1.0)
        self.assertNotIn("suggested_params", result[0])
        kwargs = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0], "http://internal:8888/v1/chat/completions")
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertFalse(json.loads(kwargs["data"])["chat_template_kwargs"]["enable_thinking"])


if __name__ == "__main__":
    unittest.main()
