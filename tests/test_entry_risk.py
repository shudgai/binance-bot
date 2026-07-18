import asyncio
import unittest
import sys
import os
import time
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state, repair_invalid_states, get_open_position_count
from core import ctx
from core import exchange_client
from core.check_entries import (
    _is_confirmable_exit_cooldown, _rapid_reconfirm_cooldown_entry, _range_exit_prices,
)
from core.orders import (execute_order, _enforce_bracket_rr, _pending_entry_setup_valid,
    _entry_signal_chase_guard, _pending_entry_reprice_needed,
    _translated_pending_limit_price, _ma_cross_anti_chase_plan, _is_dynamic_pending_entry,
    _reanchor_rejected_passive_price, _entry_price_guard,
    _ma25_confirmed_pullback_price, _range_exit_bracket,
    _ma_exchange_stop_target, _pending_entry_time_expired)


class EntryRiskTests(unittest.TestCase):
    def test_settling_contract_is_not_openable(self):
        openable, reason = exchange_client._market_openability({
            "active": False,
            "contract": True,
            "linear": True,
            "info": {"status": "SETTLING"},
        })
        self.assertFalse(openable)
        self.assertIn("SETTLING", reason)

    def test_trading_linear_contract_is_openable(self):
        openable, reason = exchange_client._market_openability({
            "active": True,
            "contract": True,
            "linear": True,
            "info": {"status": "TRADING"},
        })
        self.assertTrue(openable)
        self.assertIn("TRADING", reason)

    def test_cooldown_only_allows_confirmed_opposite_side_early_reentry(self):
        state = {"status": "COOLDOWN", "next_status_time": 2000.0,
                 "status_reason": "冷卻中 (30分鐘) - [微利] [MA_Peak_Lock]",
                 "last_exit_direction": "buy"}
        self.assertFalse(_is_confirmable_exit_cooldown(state, now=1000.0, side="buy"))
        self.assertTrue(_is_confirmable_exit_cooldown(state, now=1000.0, side="sell"))
        state["status"] = "BANNED"
        self.assertFalse(_is_confirmable_exit_cooldown(state, now=1000.0, side="sell"))

    def test_legacy_cooldown_without_exit_direction_cannot_release_early(self):
        state = {"status": "COOLDOWN", "next_status_time": 2000.0}
        self.assertFalse(_is_confirmable_exit_cooldown(state, now=1000.0, side="buy"))

    def test_cooldown_reentry_rapidly_rechecks_ma7_ma25_ma99_twice(self):
        sym = "XRPUSDT"
        init_states([sym])
        reset_coin_state(sym)
        STATES[sym].update({
            "status": "COOLDOWN", "next_status_time": time.time() + 600,
            "last_exit_direction": "sell",
            "qty": 0.0, "close_price": 100.0, "current_atr": 1.0,
            "_expected_funding_cost_pct": 0.0,
        })
        with patch("core.check_entries.compute_signal_strength",
                   return_value=("buy", 25.0, "MA_Cross")) as signal_mock:
            with patch("core.check_entries.btc_macro_entry_guard",
                       return_value=(True, "ok", "BULL")):
                with patch("core.check_entries._funding_rate_guard",
                           new=AsyncMock(return_value=(True, "ok"))):
                    with patch("core.check_entries.is_entry_candidate_still_valid",
                               return_value=(True, "MA7/MA25/MA99 aligned")) as ma_mock:
                        with patch("core.check_entries.is_entry_allowed", return_value=True):
                            with patch("core.check_entries._calc_sl_tp",
                                       return_value=(1.0, 1.0, 1.0, 2.0)):
                                with patch("core.check_entries._ma_candidate_quality",
                                           return_value=(True, "ok", 5.0)):
                                    with patch("core.symbol_profile.SYMBOL_PROFILES", {
                                        sym: {"_trade_eligible": True},
                                    }):
                                        result = asyncio.run(_rapid_reconfirm_cooldown_entry(
                                            sym, "buy", "MA_Cross", 25.0,
                                            checks=2, interval=0,
                                        ))
        self.assertEqual(result, (True, "three confirmations passed"))
        self.assertEqual(signal_mock.call_count, 2)
        self.assertEqual(ma_mock.call_count, 2)

    def test_cooldown_reentry_fails_closed_without_radar_profile(self):
        sym = "XRPUSDT"
        init_states([sym])
        reset_coin_state(sym)
        STATES[sym].update({
            "status": "COOLDOWN", "next_status_time": time.time() + 600,
            "last_exit_direction": "sell", "qty": 0.0,
        })
        with patch("core.check_entries.compute_signal_strength",
                   return_value=("buy", 25.0, "MA_Cross")), \
             patch("core.symbol_profile.SYMBOL_PROFILES", {}):
            confirmed, reason = asyncio.run(_rapid_reconfirm_cooldown_entry(
                sym, "buy", "MA_Cross", 25.0, checks=1, interval=0,
            ))
        self.assertFalse(confirmed)
        self.assertIn("尚無雷達交易資格", reason)

    def test_cooldown_reentry_stops_when_direction_changes(self):
        sym = "XRPUSDT"
        init_states([sym])
        reset_coin_state(sym)
        STATES[sym].update({
            "status": "COOLDOWN", "next_status_time": time.time() + 600,
            "last_exit_direction": "sell",
            "qty": 0.0, "close_price": 100.0,
        })
        with patch("core.check_entries.compute_signal_strength",
                   return_value=("sell", 25.0, "MA_Cross")):
            confirmed, reason = asyncio.run(_rapid_reconfirm_cooldown_entry(
                sym, "buy", "MA_Cross", 25.0, checks=1, interval=0,
            ))
        self.assertFalse(confirmed)
        self.assertIn("signal changed", reason)

    def test_confirmed_ma25_pullback_does_not_wait_at_deep_structure_again(self):
        buy_price = _ma25_confirmed_pullback_price("buy", 99.0, 100.0, 0.4)
        sell_price = _ma25_confirmed_pullback_price("sell", 101.0, 100.0, 0.4)
        self.assertAlmostEqual(buy_price, 99.8)
        self.assertAlmostEqual(sell_price, 100.2)

    def test_confirmed_ma25_pullback_keeps_nearby_passive_structure(self):
        self.assertAlmostEqual(
            _ma25_confirmed_pullback_price("buy", 99.94, 100.0, 0.4), 99.94
        )
        self.assertAlmostEqual(
            _ma25_confirmed_pullback_price("sell", 100.06, 100.0, 0.4), 100.06
        )

    def test_stale_structure_price_is_reanchored_inside_existing_guard(self):
        sym = "XRPUSDT"
        init_states([sym])
        STATES[sym]["current_atr"] = 0.2
        refreshed, changed = _reanchor_rejected_passive_price(
            sym, "buy", 99.1, 100.0, mode="pullback"
        )
        self.assertTrue(changed)
        self.assertGreater(refreshed, 99.1)
        self.assertLess(refreshed, 100.0)
        self.assertTrue(_entry_price_guard(
            sym, "buy", refreshed, 100.0, mode="pullback"
        )[0])

    def test_pending_ma_order_does_not_expire_by_clock(self):
        info = {
            "sym": "XRPUSDT", "side": "buy", "entry_route": "MA_Cross",
            "signal_strength": 25.0, "signal_price": 100.0,
            "signal_candle_ts": int((time.time() - 601.0) * 1000),
        }
        revalidate = unittest.mock.Mock(return_value=(True, "ok"))
        self.assertEqual(_pending_entry_setup_valid(info, validator=revalidate), (True, "ok"))

    def test_pending_ma_order_reprices_after_material_market_drift(self):
        sym = "XRPUSDT"
        init_states([sym])
        STATES[sym]["current_atr"] = 1.0
        info = {
            "sym": sym, "side": "buy", "entry_route": "MA25_Pullback",
            "signal_price": 100.0, "market_reference_price": 100.0,
            "price": 99.0, "timestamp": 1000.0,
        }
        needed, reason = _pending_entry_reprice_needed(sym, info, 100.7, now=1030.0)
        self.assertTrue(needed)
        self.assertIn("market drift", reason)
        self.assertAlmostEqual(_translated_pending_limit_price(info, 100.7), 99.7)

    def test_pending_ma_order_reprice_has_short_anti_churn_delay(self):
        sym = "XRPUSDT"
        init_states([sym])
        STATES[sym]["current_atr"] = 1.0
        info = {
            "sym": sym, "side": "buy", "entry_route": "MA25_Pullback",
            "signal_price": 100.0, "market_reference_price": 100.0,
            "price": 99.0, "last_reprice_at": 1000.0,
        }
        self.assertEqual(
            _pending_entry_reprice_needed(sym, info, 100.7, now=1004.0),
            (False, "reprice_cooldown"),
        )
        needed, _ = _pending_entry_reprice_needed(sym, info, 100.7, now=1006.0)
        self.assertTrue(needed)

    def test_waiting_buy_is_rejected_after_rsi_flips_bearish(self):
        from core.check_entries import is_entry_candidate_still_valid
        sym = "XRPUSDT"
        init_states([sym])
        reset_coin_state(sym)
        STATES[sym].update({"close_price": 100.0, "current_atr": 1.0, "current_rsi": 49.0})
        with patch("core.entry_filter.is_ma_direction_aligned", return_value=True):
            allowed, reason = is_entry_candidate_still_valid(sym, "buy", "MA_Cross", 25.0, 100.0)
        self.assertFalse(allowed)
        self.assertIn("RSI below long threshold", reason)

    def test_waiting_sell_is_rejected_after_rsi_leaves_short_threshold(self):
        from core.check_entries import is_entry_candidate_still_valid
        sym = "XRPUSDT"
        init_states([sym])
        reset_coin_state(sym)
        STATES[sym].update({"close_price": 100.0, "current_atr": 1.0, "current_rsi": 50.0})
        with patch("core.entry_filter.is_ma_direction_aligned", return_value=True):
            allowed, reason = is_entry_candidate_still_valid(sym, "sell", "MA_Cross", 25.0, 100.0)
        self.assertFalse(allowed)
        self.assertIn("RSI above short threshold", reason)

    def test_waiting_entry_accepts_exact_rsi_boundaries(self):
        from core.check_entries import is_entry_candidate_still_valid
        sym = "XRPUSDT"
        init_states([sym])
        reset_coin_state(sym)
        state = STATES[sym]
        state.update({"close_price": 100.0, "current_atr": 1.0, "current_rsi": 51.0})
        with patch("core.entry_filter.is_ma_direction_aligned", return_value=True):
            self.assertTrue(is_entry_candidate_still_valid(sym, "buy", "MA_Cross", 25.0, 100.0)[0])
            state["current_rsi"] = 49.0
            self.assertTrue(is_entry_candidate_still_valid(sym, "sell", "MA_Cross", 25.0, 100.0)[0])

    def test_extended_ma_cross_long_waits_at_ma7_instead_of_chasing(self):
        sym = "XRPUSDT"
        init_states([sym])
        STATES[sym].update({"ma7": 100.0, "current_atr": 0.4})
        wait, anchor, reason = _ma_cross_anti_chase_plan(
            sym, "buy", 100.6, "MA_Cross",
        )
        self.assertTrue(wait)
        self.assertAlmostEqual(anchor, 100.03)
        self.assertIn("extension", reason)

    def test_extended_ma_cross_short_waits_at_ma7_instead_of_chasing(self):
        sym = "XRPUSDT"
        init_states([sym])
        STATES[sym].update({"ma7": 100.0, "current_atr": 0.4})
        wait, anchor, _ = _ma_cross_anti_chase_plan(
            sym, "sell", 99.4, "MA_Cross",
        )
        self.assertTrue(wait)
        self.assertAlmostEqual(anchor, 99.97)

    def test_ma_cross_near_ma7_keeps_normal_chase_mode(self):
        sym = "XRPUSDT"
        init_states([sym])
        STATES[sym].update({"ma7": 100.0, "current_atr": 1.0})
        wait, _, reason = _ma_cross_anti_chase_plan(
            sym, "buy", 100.4, "MA_Cross",
        )
        self.assertFalse(wait)
        self.assertEqual(reason, "price_near_ma7")

    def test_ma_cross_pending_quote_reanchors_when_ma7_moves(self):
        sym = "XRPUSDT"
        init_states([sym])
        STATES[sym].update({"ma7": 100.2, "current_atr": 1.0})
        info = {
            "sym": sym, "side": "buy", "entry_route": "MA_Cross",
            "signal_price": 101.5, "market_reference_price": 101.5,
            "price": 100.03, "last_reprice_at": 1000.0,
            "ma_cross_anti_chase": True,
        }
        needed, reason = _pending_entry_reprice_needed(sym, info, 102.0, now=1006.0)
        self.assertTrue(needed)
        self.assertEqual(reason, "MA7 anchor moved")
        self.assertAlmostEqual(_translated_pending_limit_price(info, 102.0), 100.23006)

    def test_pending_range_order_stays_live_and_reanchors_to_support(self):
        sym = "XRPUSDT"
        init_states([sym])
        reset_coin_state(sym)
        STATES[sym].update({
            "close_price": 99.2, "current_atr": 1.0, "adx": 10.0,
            "range_support_level": 99.0, "range_resistance_level": 103.0,
        })
        info = {
            "sym": sym, "side": "buy", "entry_route": "Range_Support_Long",
            "signal_strength": 18.0, "signal_price": 99.2,
            "market_reference_price": 99.2, "price": 98.5,
            "last_reprice_at": 1000.0,
        }
        self.assertTrue(_is_dynamic_pending_entry(info))
        self.assertEqual(_pending_entry_setup_valid(info), (True, "range setup valid"))
        needed, reason = _pending_entry_reprice_needed(sym, info, 99.2, now=1006.0)
        self.assertTrue(needed)
        self.assertEqual(reason, "range anchor moved")
        self.assertAlmostEqual(_translated_pending_limit_price(info, 99.2), 99.0198)

    def test_pending_range_order_is_cancelled_after_leaving_boundary(self):
        sym = "XRPUSDT"
        init_states([sym])
        reset_coin_state(sym)
        STATES[sym].update({
            "close_price": 101.5, "current_atr": 0.5, "adx": 10.0,
            "range_support_level": 99.0, "range_resistance_level": 103.0,
        })
        info = {
            "sym": sym, "side": "buy", "entry_route": "Range_Support_Long",
            "signal_strength": 18.0, "signal_price": 101.5,
        }
        allowed, reason = _pending_entry_setup_valid(info)
        self.assertFalse(allowed)
        self.assertIn("離開支撐邊界", reason)

    def test_float_symbol_state_is_repaired_before_position_count(self):
        sym = "BROKENUSDT"
        original_symbols = list(ctx.ALL_SYMBOLS)
        original_state = ctx.STATES.get(sym)
        try:
            if sym not in ctx.ALL_SYMBOLS:
                ctx.ALL_SYMBOLS.append(sym)
            ctx.STATES[sym] = 12.34

            self.assertEqual(repair_invalid_states(), [sym])
            self.assertIsInstance(ctx.STATES[sym], dict)
            self.assertEqual(ctx.STATES[sym]["qty"], 0.0)
            self.assertIsInstance(get_open_position_count(), int)
        finally:
            ctx.ALL_SYMBOLS[:] = original_symbols
            if original_state is None:
                ctx.STATES.pop(sym, None)
            else:
                ctx.STATES[sym] = original_state

    def test_pending_first_entry_revalidates_latest_signal(self):
        info = {
            "sym": "AAVEUSDT", "side": "sell", "entry_route": "b",
            "signal_strength": 30.6, "signal_price": 96.06,
            "is_rescue_dca": False,
        }
        revalidate = unittest.mock.Mock(return_value=(False, "bullish divergence"))
        self.assertEqual(
            _pending_entry_setup_valid(info, validator=revalidate),
            (False, "bullish divergence"),
        )
        revalidate.assert_called_once_with("AAVEUSDT", "sell", "b", 30.6, 96.06)

    def test_pending_rescue_order_keeps_separate_risk_path(self):
        self.assertEqual(
            _pending_entry_setup_valid({"is_rescue_dca": True}),
            (True, "rescue_dca"),
        )

    def test_additional_entry_updates_average_price_safely(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["last_entry_price"] = 100.0
        s["entry_count"] = 1
        s["last_entry_time"] = 0.0
        s["close_price"] = 110.0
        s["current_atr"] = 1.0
        s["current_vol"] = 1000.0
        s["vol_ma20"] = 100.0
        s["macd_line"] = 2.0
        s["macd_signal"] = 1.0
        s["prev_macd_line"] = 1.0
        s["prev_macd_signal"] = 0.8
        s["ohlcv"] = [
            [0, 100, 105, 95, 100, 1000],
            [0, 100, 105, 95, 105, 1000],
            [0, 105, 110, 100, 110, 1000]
        ]

        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker.return_value = {"last": 110.0}
        mock_exchange.fetch_order_book.return_value = {"bids": [[110.0, 1000.0]], "asks": [[110.1, 100.0]]}
        mock_exchange.fetch_balance.return_value = {"USDT": {"total": 10000.0, "free": 10000.0}}
        mock_exchange.fetch_order.return_value = {"status": "closed", "filled": 4.5, "average": 110.0, "price": 110.0}
        mock_exchange.fetch_positions.return_value = []
        mock_exchange.create_order.return_value = {"id": "12345"}
        
        with patch("core.orders.compute_per_coin_margin", return_value=3000.0), \
             patch("core.orders.get_balance", return_value=10000.0), \
             patch("core.entry_filter.is_entry_allowed", return_value=True), \
             patch("core.entry_filter.is_entry_volume_confirmed", return_value=True), \
             patch("core.orders.sanitize_order_qty", side_effect=lambda sym, q: 4.5), \
             patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_futures", mock_exchange):
            asyncio.run(execute_order(sym, "buy", 110.0))

        self.assertEqual(s["entry_count"], 1)
        self.assertEqual(s["avg_price"], 100.0)

    def test_losing_position_skips_additional_entry(self):
        sym = "XRPUSDT"
        init_states([sym])
        s = STATES[sym]
        reset_coin_state(sym)
        s["qty"] = 1.0
        s["avg_price"] = 100.0
        s["close_price"] = 95.0
        s["entry_count"] = 1
        s["last_entry_time"] = 0.0

        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker.return_value = {"last": 95.0}
        mock_exchange.fetch_order_book.return_value = {"bids": [[95.0, 1000.0]], "asks": [[95.1, 100.0]]}
        with patch("core.orders.compute_per_coin_margin", return_value=3000.0), \
             patch("core.orders.get_balance", return_value=10000.0), \
             patch("core.orders.PAPER_TRADING", False), \
             patch("core.orders.exchange_futures", mock_exchange):
            asyncio.run(execute_order(sym, "buy", 95.0))

        self.assertEqual(s["entry_count"], 1)


    def test_first_short_entry_rejects_velvet_style_low_price_chase(self):
        allowed, reason = _entry_signal_chase_guard("sell", 0.5324, 0.5304)
        self.assertFalse(allowed)
        self.assertIn("signal chase", reason)

    def test_pullback_to_better_price_is_not_adverse_chase(self):
        allowed, _ = _entry_signal_chase_guard("buy", 100.0, 99.0)
        self.assertTrue(allowed)

    def test_range_long_exchange_bracket_keeps_opposite_boundary_target(self):
        state = {"range_tp_price": 102.95, "range_sl_price": 98.5}
        self.assertEqual(_range_exit_bracket(state, 99.02, True, 0.01), (98.5, 102.95))

    def test_range_short_exchange_bracket_keeps_opposite_boundary_target(self):
        state = {"range_tp_price": 97.05, "range_sl_price": 101.5}
        self.assertEqual(_range_exit_bracket(state, 100.98, False, 0.01), (101.5, 97.05))

    def test_btc_range_short_stop_keeps_room_from_actual_entry(self):
        take_profit, stop = _range_exit_prices(
            63275.0, 62693.85, 63250.357143, 200.885714, "sell"
        )
        self.assertAlmostEqual(take_profit, 62725.4875)
        self.assertGreaterEqual((stop - 63275.0) / 63275.0, 0.003 - 1e-12)
        self.assertGreater((63275.0 - take_profit) / (stop - 63275.0), 1.0)

    def test_range_exchange_bracket_rejects_wrong_side_prices(self):
        state = {"range_tp_price": 98.0, "range_sl_price": 102.0}
        self.assertIsNone(_range_exit_bracket(state, 100.0, True, 0.01))

    def test_long_bracket_enforces_minimum_reward_over_risk(self):
        stop, take_profit = _enforce_bracket_rr(100.0, 97.0, 102.0, True, 0.1, min_rr=1.5)
        self.assertEqual(stop, 97.0)
        self.assertGreaterEqual(take_profit - 100.0, (100.0 - stop) * 1.5)

    def test_short_bracket_enforces_minimum_reward_over_risk(self):
        stop, take_profit = _enforce_bracket_rr(100.0, 103.0, 98.0, False, 0.1, min_rr=1.5)
        self.assertEqual(stop, 103.0)
        self.assertGreaterEqual(100.0 - take_profit, (stop - 100.0) * 1.5)


    def test_ma7_simple_pending_order_expires_after_twenty_seconds(self):
        info = {"entry_route": "MA7_Simple", "strategy_type": "ma"}
        self.assertFalse(_pending_entry_time_expired(info, 20.0, 120.0))
        self.assertTrue(_pending_entry_time_expired(info, 20.01, 120.0))

    def test_structural_ma_pending_order_keeps_dynamic_lifetime(self):
        info = {"entry_route": "MA25_Pullback", "strategy_type": "ma"}
        self.assertFalse(_pending_entry_time_expired(info, 300.0, 120.0))

    def test_ma_exchange_stop_promotes_confirmed_profit_floor(self):
        state = {
            "ma_profit_floor_armed": True,
            "ma_profit_floor_price": 100.25,
            "ma_peak_lock_armed": False,
        }
        self.assertEqual(
            _ma_exchange_stop_target(state, 100.0, True, 98.5, current_price=100.4),
            100.25,
        )
        self.assertEqual(
            _ma_exchange_stop_target(state, 100.0, True, 98.5, current_price=100.2),
            98.5,
        )

    def test_ma_exchange_stop_is_symmetric_for_short(self):
        state = {
            "ma_profit_floor_armed": True,
            "ma_profit_floor_price": 99.75,
            "ma_peak_lock_armed": False,
        }
        self.assertEqual(
            _ma_exchange_stop_target(state, 100.0, False, 101.5, current_price=99.6),
            99.75,
        )


if __name__ == "__main__":
    unittest.main()
