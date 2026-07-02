import unittest
from unittest.mock import patch

from services import binance_service


class BinanceEntryPriceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
