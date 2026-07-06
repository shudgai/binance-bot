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

        self.assertEqual(result, [local_trade])
        remote_trades.assert_not_called()


if __name__ == "__main__":
    unittest.main()
