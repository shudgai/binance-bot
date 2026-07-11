import unittest
from unittest.mock import patch

from core import ctx, exchange_client
from services import api


class ApiRateLimitTests(unittest.TestCase):
    def tearDown(self):
        ctx.api_cooldown_until = 0.0

    def test_high_market_data_weight_activates_global_cooldown(self):
        with patch.object(
            exchange_client.exchange_futures,
            "last_response_headers",
            {"x-mbx-used-weight-1m": "500"},
        ), patch.object(
            exchange_client.exchange_market_data,
            "last_response_headers",
            {"x-mbx-used-weight-1m": "1900"},
        ):
            cooldown = exchange_client.check_binance_weight()

        self.assertEqual(cooldown, 60.0)
        self.assertGreater(ctx.api_cooldown_until, 0.0)

    def test_all_trades_uses_local_history_instead_of_per_symbol_api(self):
        local_trade = {
            "symbol": "XRP:USDT",
            "is_close": True,
            "time": 1,
        }
        with patch("services.api.is_paper_trading", return_value=False), \
             patch("services.api._get_real_trades", return_value=[local_trade]), \
             patch("services.api.get_all_positions", return_value={}), \
             patch("services.api.get_trades") as remote_trades:
            result = api.api_get_trades("ALL")

        self.assertEqual(result[0]["symbol"], local_trade["symbol"])
        self.assertEqual(result[0]["net_pnl"], 0.0)
        remote_trades.assert_not_called()


    def test_round_trip_fee_includes_entry_and_exit(self):
        trades = [
            {"symbol": "XRP:USDT", "qty": 2, "time": 1, "isBuyer": True, "is_close": False, "fee": 0.1, "realized_pnl": 0},
            {"symbol": "XRP:USDT", "qty": 2, "time": 2, "isBuyer": False, "is_close": True, "fee": 0.1, "realized_pnl": 1.0},
        ]
        result = api._attach_round_trip_fees(trades)
        self.assertAlmostEqual(result[1]["entry_fee"], 0.1)
        self.assertAlmostEqual(result[1]["total_fee"], 0.2)
        self.assertAlmostEqual(result[1]["net_pnl"], 0.8)


    def test_radar_profile_caps_tp_and_keeps_usable_leverage(self):
        from services.radar_service import _compute_dynamic_profile
        profile = _compute_dynamic_profile("AAVEUSDT", 5.8, 95.0, 1, 8)
        self.assertLessEqual(profile["tp_atr_multiplier"], 8.0)
        self.assertEqual(profile["leverage"], 3)
        self.assertTrue(profile["disable_rescue_dca"])


if __name__ == "__main__":
    unittest.main()
