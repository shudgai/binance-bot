import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from core import ctx
from core.state_manager import build_symbol_state
from core.check_entries import (
    check_entries,
    _radar_entry_block_reason,
    _radar_signal_block_message,
    _radar_direction_block_reason,
    _fast_radar_entry_confirmation,
)


class RadarEntryDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.sym = "RADARTESTUSDT"
        self.original_symbols = list(ctx.ALL_SYMBOLS)
        self.original_state = ctx.STATES.get(self.sym)
        ctx.ALL_SYMBOLS[:] = [self.sym]
        ctx.STATES[self.sym] = build_symbol_state(self.sym)
        ctx.STATES[self.sym].update({
            "status": "ACTIVE",
            "qty": 0.0,
            "is_ordering": False,
            "ohlcv": [],
        })

    def tearDown(self):
        ctx.ALL_SYMBOLS[:] = self.original_symbols
        if self.original_state is None:
            ctx.STATES.pop(self.sym, None)
        else:
            ctx.STATES[self.sym] = self.original_state

    def _reaches_guard_after_radar(self, route):
        with patch("core.check_entries.is_daily_loss_halted", return_value=False), \
             patch("core.check_entries.get_open_position_count", return_value=0), \
             patch("core.balance.get_dynamic_max_slots", return_value=3), \
             patch("core.check_entries._load_disabled_symbols", return_value=set()), \
             patch("core.check_entries._is_confirmable_exit_cooldown", return_value=False), \
             patch("core.check_entries.compute_signal_strength",
                   return_value=("buy", 25.0, route)), \
             patch("core.check_entries.btc_macro_entry_guard",
                   return_value=(False, "測試終止", "MIXED")) as macro_guard, \
             patch("core.check_entries.set_entry_diagnosis"), \
             patch("core.symbol_profile.SYMBOL_PROFILES", {
                 self.sym: {
                     "_trade_eligible": False,
                     "_trade_eligibility_reason": "雷達觀察中",
                 },
             }):
            asyncio.run(check_entries())
        return macro_guard.called

    def test_ma7_simple_bypasses_radar_block_and_continues(self):
        self.assertTrue(self._reaches_guard_after_radar("MA7_Simple"))

    def test_ma_cross_is_still_blocked_by_radar(self):
        self.assertFalse(self._reaches_guard_after_radar("MA_Cross"))

    def test_strong_fast_path_bypasses_only_observation_delay(self):
        profile = self._fast_profile()
        profile.update({
            "_trade_eligible": False,
            "_radar_observation_mature": False,
            "_radar_confirmations": 1,
            "_trade_eligibility_reason": "觀察中：等待第二次雷達確認",
        })
        fast_confirm = AsyncMock(return_value=(True, "快速確認完成"))
        with patch("core.check_entries.is_daily_loss_halted", return_value=False), \
             patch("core.check_entries.get_open_position_count", return_value=0), \
             patch("core.balance.get_dynamic_max_slots", return_value=3), \
             patch("core.check_entries._load_disabled_symbols", return_value=set()), \
             patch("core.check_entries._is_confirmable_exit_cooldown", return_value=False), \
             patch("core.check_entries.compute_signal_strength",
                   return_value=("buy", 25.0, "MA_Cross")), \
             patch("core.check_entries._fast_radar_entry_confirmation", fast_confirm), \
             patch("core.check_entries.btc_macro_entry_guard",
                   return_value=(False, "測試終止", "MIXED")) as macro_guard, \
             patch("core.check_entries.set_entry_diagnosis"), \
             patch("core.symbol_profile.SYMBOL_PROFILES", {self.sym: profile}):
            asyncio.run(check_entries())
        fast_confirm.assert_awaited_once()
        self.assertTrue(macro_guard.called)

    def test_fast_path_approval_survives_final_radar_recheck(self):
        profile = self._fast_profile()
        profile.update({
            "_radar_observation_mature": False,
            "_radar_confirmations": 1,
        })
        self.assertIn(
            "等待第二次雷達確認",
            _radar_entry_block_reason(profile, "MA_Cross"),
        )
        self.assertEqual(
            _radar_entry_block_reason(
                profile, "MA_Cross", observation_confirmed=True,
            ),
            "",
        )

    def test_fast_path_does_not_bypass_route_volatility_guard(self):
        profile = self._fast_profile()
        profile.update({
            "_radar_atr_pct": 12.0,
            "_radar_observation_mature": False,
        })
        self.assertIn(
            "MA ATR 12.00% 高於 8.00%",
            _radar_entry_block_reason(
                profile, "MA_Cross", observation_confirmed=True,
            ),
        )

    def test_mature_range_route_can_use_range_specific_atr_band(self):
        profile = {
            "_trade_eligible": False,
            "_radar_atr_pct": 12.0,
            "_radar_one_h_vol_pct": 2.0,
            "_radar_change_pct": 8.0,
            "_radar_observation_mature": True,
        }
        self.assertEqual(_radar_entry_block_reason(profile, "Range_Support_Long"), "")
        self.assertIn("MA ATR 12.00% 高於 8.00%", _radar_entry_block_reason(profile, "MA_Cross"))

    def test_range_bypasses_observation_but_not_range_volatility_limits(self):
        profile = {
            "_trade_eligible": False,
            "_trade_eligibility_reason": "觀察中：等待第二次雷達確認",
            "_radar_atr_pct": 12.0,
            "_radar_one_h_vol_pct": 2.0,
            "_radar_change_pct": 8.0,
            "_radar_observation_mature": False,
            "_radar_confirmations": 1,
        }
        self.assertEqual(_radar_entry_block_reason(profile, "Range_Support_Long"), "")

        profile["_radar_one_h_vol_pct"] = 5.0
        self.assertIn(
            "Range 1H 波動 5.00% 高於 3.50%",
            _radar_entry_block_reason(profile, "Range_Support_Long"),
        )

    def test_range_ignores_slower_trend_radar_direction(self):
        profile = {"_radar_entry_direction": "short", "_radar_entry_readiness": 0.90}
        self.assertEqual(
            _radar_direction_block_reason(profile, "buy", "Range_Support_Long"), "",
        )

    def test_xrp_range_cannot_trade_against_radar_direction(self):
        profile = {"_radar_entry_direction": "short", "_radar_entry_readiness": 0.55}
        self.assertIn(
            "與雷達 short 不一致",
            _radar_direction_block_reason(
                profile, "buy", "Range_Support_Long", "XRPUSDT"
            ),
        )
        self.assertEqual(
            _radar_direction_block_reason(
                profile, "buy", "Range_Support_Long", "ADAUSDT"
            ),
            "",
        )

    def test_actual_breakout_cannot_borrow_ma_classification(self):
        profile = {
            "_trade_eligible": True,
            "_radar_atr_pct": 7.0,
            "_radar_one_h_vol_pct": 3.0,
            "_radar_change_pct": 5.0,
            "_radar_observation_mature": True,
        }
        self.assertEqual(_radar_entry_block_reason(profile, "MA_Cross"), "")
        self.assertIn("Breakout ATR 7.00% 高於 5.00%", _radar_entry_block_reason(profile, "MA_Breakout"))

    def test_ma7_simple_uses_live_turn_instead_of_slower_radar_direction(self):
        profile = {"_radar_entry_direction": "short", "_radar_entry_readiness": 0.90}
        self.assertEqual(
            _radar_direction_block_reason(profile, "buy", "MA7_Simple"), "",
        )

    def test_other_routes_keep_strong_radar_direction_guard(self):
        profile = {"_radar_entry_direction": "short", "_radar_entry_readiness": 0.90}
        self.assertEqual(
            _radar_direction_block_reason(profile, "buy", "MA_Cross"),
            "訊號 buy 與雷達 short 不一致",
        )
        profile["_radar_entry_readiness"] = 0.79
        self.assertEqual(_radar_direction_block_reason(profile, "buy", "MA_Cross"), "")

    def _fast_profile(self, direction="long", readiness=0.75, first_seen=900.0):
        return {
            "_radar_strict_eligible": True,
            "_radar_atr_pct": 2.0,
            "_radar_one_h_vol_pct": 1.0,
            "_radar_change_pct": 1.0,
            "_radar_entry_direction": direction,
            "_radar_entry_readiness": readiness,
            "_radar_candidate_since": first_seen,
        }

    def test_strong_radar_fast_path_accepts_two_closed_1m_candles(self):
        state = ctx.STATES[self.sym]
        state.update({"ema20_15m": 101.0, "ema50_15m": 100.0})
        candles = [
            [900000, 100.0, 100.3, 99.9, 100.2, 10.0],
            [960000, 100.2, 100.6, 100.1, 100.5, 12.0],
            [1020000, 100.5, 100.7, 100.4, 100.6, 3.0],
        ]
        fetch = AsyncMock(return_value=candles)
        with patch("core.exchange_client.exchange_market_data.fetch_ohlcv", fetch):
            ok, reason = asyncio.run(_fast_radar_entry_confirmation(
                self.sym, "buy", "MA_Cross", self._fast_profile(), now=1100.0,
            ))
        self.assertTrue(ok)
        self.assertIn("兩根 1m", reason)
        fetch.assert_awaited_once_with(self.sym, "1m", limit=4)

    def test_strong_radar_fast_path_rejects_unaligned_1m_candles(self):
        state = ctx.STATES[self.sym]
        state.update({"ema20_15m": 101.0, "ema50_15m": 100.0})
        candles = [
            [900000, 100.2, 100.3, 99.9, 100.0, 10.0],
            [960000, 100.0, 100.4, 99.9, 100.3, 12.0],
            [1020000, 100.3, 100.4, 100.2, 100.3, 3.0],
        ]
        with patch(
            "core.exchange_client.exchange_market_data.fetch_ohlcv",
            AsyncMock(return_value=candles),
        ):
            ok, reason = asyncio.run(_fast_radar_entry_confirmation(
                self.sym, "buy", "MA_Cross", self._fast_profile(), now=1100.0,
            ))
        self.assertFalse(ok)
        self.assertIn("1m 連續方向", reason)

    def test_strong_radar_fast_path_keeps_15m_direction_guard(self):
        state = ctx.STATES[self.sym]
        state.update({"ema20_15m": 99.0, "ema50_15m": 100.0})
        fetch = AsyncMock()
        with patch("core.exchange_client.exchange_market_data.fetch_ohlcv", fetch):
            ok, reason = asyncio.run(_fast_radar_entry_confirmation(
                self.sym, "buy", "MA_Cross", self._fast_profile(), now=1100.0,
            ))
        self.assertFalse(ok)
        self.assertIn("15m 趨勢", reason)
        fetch.assert_not_awaited()

    def test_missing_profile_fails_closed(self):
        self.assertEqual(_radar_entry_block_reason({}), "尚無雷達交易資格")

    def test_observation_reason_is_preserved(self):
        profile = {
            "_trade_eligible": False,
            "_trade_eligibility_reason": "雷達確認 1/2，觀察中",
        }
        self.assertEqual(
            _radar_entry_block_reason(profile),
            "雷達確認 1/2，觀察中",
        )

    def test_eligible_profile_has_no_block(self):
        self.assertEqual(_radar_entry_block_reason({"_trade_eligible": True}), "")

    def test_signal_message_never_claims_order_is_ready(self):
        message = _radar_signal_block_message(
            "1000XECUSDT", "MA7_Simple", "雷達觀察中",
        )
        self.assertEqual(
            message,
            "1000XECUSDT: 偵測到 MA7_Simple 訊號，但雷達觀察中，不送單",
        )
        self.assertNotIn("準備送單", message)


if __name__ == "__main__":
    unittest.main()
