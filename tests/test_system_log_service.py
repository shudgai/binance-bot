import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import system_log_service


class SystemLogHandlerTests(unittest.TestCase):
    def setUp(self):
        system_log_service.clear_system_logs()

    def tearDown(self):
        system_log_service.clear_system_logs()

    def test_file_backed_handler_writes_logs(self):
        handler = system_log_service.FileBackedSystemLogHandler()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello from handler",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

        logs = system_log_service.get_system_logs()
        self.assertTrue(any(item.get("text") == "hello from handler" for item in logs))

    def test_attach_to_root_logger_captures_child_logger_messages(self):
        root_logger = logging.getLogger()
        child_logger = logging.getLogger("core.test.child")
        child_logger.setLevel(logging.INFO)
        child_logger.propagate = True

        handler = system_log_service.attach_to_root_logger(root_logger)
        try:
            child_logger.info("hello from child logger")
            logs = system_log_service.get_system_logs()
            self.assertTrue(any(item.get("text") == "hello from child logger" for item in logs))
        finally:
            root_logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
