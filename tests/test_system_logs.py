import importlib
import unittest
from unittest.mock import patch

from services import api
from services import system_log_service as log_service
from services.system_log_service import add_system_log, clear_system_logs
from services.bot_manager_service import classify_bot_log_level


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

    def test_logs_persist_across_module_reload(self):
        clear_system_logs()
        add_system_log("persisted log", "info")

        reloaded = importlib.reload(log_service)

        self.assertEqual(reloaded.get_system_logs()[-1]["text"], "persisted log")

    def test_routine_kline_refresh_is_info(self):
        self.assertEqual(classify_bot_log_level("🔄 [KLines] 已更新市場行情資料"), "info")

    def test_real_warning_and_error_levels_are_preserved(self):
        self.assertEqual(classify_bot_log_level("🛡️ 進入冷卻"), "warning")
        self.assertEqual(classify_bot_log_level("⚠️ API 失敗"), "danger")


if __name__ == "__main__":
    unittest.main()
