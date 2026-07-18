import importlib
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from services import api
from services import system_log_service as log_service
from services.system_log_service import add_system_log, clear_system_logs
from services.bot_manager_service import (
    _prune_entry_diagnoses,
    _should_emit_bot_web_log,
    _summarize_entry_diagnosis,
    bot_status,
    classify_bot_log_level,
    read_bot_output,
    set_entry_diagnosis,
)


class SystemLogTests(unittest.TestCase):
    def setUp(self):
        clear_system_logs()
        from services import bot_manager_service as manager
        manager._web_log_throttle.clear()

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

    def test_identical_ma_wait_log_is_throttled_for_one_minute(self):
        text = "⏳ DOTUSDT [MA_Strategy] 等待 MA7／MA25 收線交叉、MA25 回調或帶量突破"
        self.assertTrue(_should_emit_bot_web_log(text, now=100.0))
        self.assertFalse(_should_emit_bot_web_log(text, now=159.0))
        self.assertTrue(_should_emit_bot_web_log(text, now=160.0))

    def test_changed_ma_wait_diagnostic_is_not_hidden(self):
        first = "⏳ DOTUSDT [MA_Strategy] 量能不足（RVOL=0.30x）"
        changed = "⏳ DOTUSDT [MA_Strategy] 量能不足（RVOL=0.31x）"
        self.assertTrue(_should_emit_bot_web_log(first, now=200.0))
        self.assertTrue(_should_emit_bot_web_log(changed, now=201.0))

    def test_real_warning_and_error_levels_are_preserved(self):
        self.assertEqual(classify_bot_log_level("🛡️ 進入冷卻"), "warning")
        self.assertEqual(classify_bot_log_level("⚠️ API 失敗"), "danger")

    def test_entry_diagnosis_is_emitted_for_parent_process(self):
        output = io.StringIO()
        with redirect_stdout(output):
            set_entry_diagnosis("HYPEUSDT: MACD空頭擴張未通過")

        self.assertEqual(
            output.getvalue().strip(),
            "@@ENTRY_DIAG@@HYPEUSDT: MACD空頭擴張未通過",
        )

    def test_parent_process_receives_entry_diagnosis_marker(self):
        class FakeProc:
            def __init__(self):
                self.stdout = io.StringIO("@@ENTRY_DIAG@@ETHUSDT: EMA50空頭未通過\n")
                self.returncode = 0

            def wait(self):
                return self.returncode

        original = bot_status.get("entry_diagnosis")
        original_map = dict(bot_status.get("entry_diagnoses", {}))
        try:
            read_bot_output(FakeProc(), "__multi__")
            self.assertEqual(
                bot_status["entry_diagnoses"]["ETHUSDT"]["message"],
                "ETHUSDT: EMA50空頭未通過",
            )
        finally:
            bot_status["entry_diagnosis"] = original
            bot_status["entry_diagnoses"] = original_map

    def test_status_summary_prefers_actionable_reason_over_warmup(self):
        original_map = dict(bot_status.get("entry_diagnoses", {}))
        try:
            bot_status["entry_diagnoses"] = {
                "ETHUSDT": {
                    "message": "ETHUSDT: MACD空頭擴張、EMA50空頭未通過",
                    "updated_at": 100.0,
                },
                "TAOUSDT": {
                    "message": "TAOUSDT: K 線資料不足（至少需要 20 根）",
                    "updated_at": 101.0,
                },
            }
            eligibility = {
                "ETHUSDT": {"eligible": True},
                "TAOUSDT": {"eligible": True},
            }

            summary = _summarize_entry_diagnosis(eligibility, now=102.0)

            self.assertIn("ETHUSDT", summary)
            self.assertNotIn("K 線資料不足", summary)
        finally:
            bot_status["entry_diagnoses"] = original_map


    def test_entry_diagnoses_are_pruned_to_active_pool(self):
        original_map = dict(bot_status.get("entry_diagnoses", {}))
        try:
            bot_status["entry_diagnoses"] = {
                "BTCUSDT": {"message": "current"},
                "REUSDT": {"message": "stale"},
            }
            _prune_entry_diagnoses(["BTCUSDT"])
            self.assertEqual(set(bot_status["entry_diagnoses"]), {"BTCUSDT"})
        finally:
            bot_status["entry_diagnoses"] = original_map


if __name__ == "__main__":
    unittest.main()
