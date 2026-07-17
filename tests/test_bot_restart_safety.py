import unittest
from unittest.mock import patch

from services import bot_manager_service as manager


class BotRestartSafetyTests(unittest.TestCase):
    def setUp(self):
        self.original_running = manager.bot_status.get("is_running")
        self.original_processes = dict(manager.bot_processes)

    def tearDown(self):
        manager.bot_status["is_running"] = self.original_running
        manager.bot_processes.clear()
        manager.bot_processes.update(self.original_processes)

    def test_only_live_entry_orders_block_restart(self):
        orders = [
            {"id": "entry", "symbol": "SOLUSDT", "status": "open"},
            {
                "id": "stop",
                "symbol": "SOLUSDT",
                "status": "open",
                "reduceOnly": True,
            },
            {
                "id": "tp",
                "symbol": "SOLUSDT",
                "status": "NEW",
                "info": {"closePosition": "true"},
            },
            {"id": "done", "symbol": "SOLUSDT", "status": "closed"},
        ]
        blocking = manager._restart_blocking_entry_orders(orders)
        self.assertEqual([order["id"] for order in blocking], ["entry"])

    def test_running_bot_is_not_restarted_while_entry_order_is_open(self):
        manager.bot_status["is_running"] = True
        manager.bot_processes.clear()
        manager.bot_processes["__multi__"] = object()
        with patch.object(manager, "_restart_is_safe", return_value=False), \
             patch.object(manager, "kill_bot") as kill_mock:
            result = manager.start_bot(["SOLUSDT"], 100.0)
        self.assertFalse(result)
        kill_mock.assert_not_called()
        self.assertTrue(manager.bot_status["is_running"])

    def test_manual_stop_is_deferred_while_entry_order_is_open(self):
        manager.bot_status["is_running"] = True
        with patch.object(manager, "_restart_is_safe", return_value=False), \
             patch.object(manager, "kill_bot") as kill_mock, \
             patch.object(manager, "add_system_log"):
            result = manager.toggle_bot()
        self.assertTrue(result)
        kill_mock.assert_not_called()
        self.assertTrue(manager.bot_status["is_running"])


if __name__ == "__main__":
    unittest.main()
