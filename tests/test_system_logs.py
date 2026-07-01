import unittest
from unittest.mock import patch

from services import api
from services.system_log_service import add_system_log, clear_system_logs


class SystemLogTests(unittest.TestCase):
    def setUp(self):
        clear_system_logs()

    def test_daily_reset_keeps_logs_on_normal_startup(self):
        add_system_log("seed log", "info")

        with patch("services.api.kill_bot") as mock_kill, \
             patch("services.api.auto_radar_switch") as mock_radar, \
             patch("services.api.clear_system_logs") as mock_clear:
            api.daily_market_clean_and_reset(is_manual=False)

        self.assertEqual(mock_clear.call_count, 0)
        mock_kill.assert_called_once()
        mock_radar.assert_called_once_with(force_start=True)


if __name__ == "__main__":
    unittest.main()
