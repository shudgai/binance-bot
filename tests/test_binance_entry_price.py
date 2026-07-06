import unittest
from unittest.mock import patch

from services import binance_service


class BinanceEntryPriceTests(unittest.TestCase):

    def setUp(self):
        binance_service._single_price_cache.clear()
        binance_service._kline_cache.clear()

    @patch("services.binance_service.client")
    def test_entry_price_prefers_mark_price_when_available(self, mock_client):
        mock_client.futures_symbol_ticker.return_value = {"price": "100.00"}
        mock_client.futures_mark_price.return_value = {"markPrice": "100.10"}
        mock_client.futures_order_book.return_value = {
            "asks": [["100.12", "1"]],
            "bids": [["100.08", "1"]],
        }

        price = binance_service._get_entry_price("BTCUSDT", "BUY")

        self.assertAlmostEqual(price, 100.10, places=2)

    @patch("services.binance_service.client")
    def test_entry_price_falls_back_to_last_price(self, mock_client):
        mock_client.futures_symbol_ticker.return_value = {"price": "100.00"}
        mock_client.futures_mark_price.side_effect = Exception("no mark")

        price = binance_service._get_entry_price("BTCUSDT", "SELL")

        self.assertEqual(price, 100.0)


    @patch("services.binance_service.client")
    def test_dashboard_price_is_cached(self, mock_client):
        mock_client.futures_symbol_ticker.return_value = {"price": "100.00", "time": 1}

        first = binance_service.get_price("BTCUSDT")
        second = binance_service.get_price("BTCUSDT")

        self.assertEqual(first, second)
        mock_client.futures_symbol_ticker.assert_called_once_with(symbol="BTCUSDT")

    @patch("services.binance_service.client")
    def test_dashboard_klines_are_cached(self, mock_client):
        mock_client.futures_klines.return_value = [
            [1000, "1", "2", "0.5", "1.5", "10", 2000]
        ]

        first = binance_service.get_klines("BTCUSDT", "1m", 80)
        second = binance_service.get_klines("BTCUSDT", "1m", 80)

        self.assertEqual(first, second)
        mock_client.futures_klines.assert_called_once_with(
            symbol="BTCUSDT", interval="1m", limit=80
        )


if __name__ == "__main__":
    unittest.main()
