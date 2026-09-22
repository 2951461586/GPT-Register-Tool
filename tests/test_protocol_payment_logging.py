"""Tests for services/protocol-payment/common/logging_setup.py.

Three properties matter, in decreasing order of "how bad is it if broken":

1. stdout is NEVER touched — the desktop host parses the child's stdout as an
   IPC channel, so any logging that writes there corrupts the protocol.
2. A failed rotation degrades to appending, never to silent data loss — the
   exact failure that permanently stopped sms_tool.jsonl on 2026-09-13.
3. Two loggers in the same process are independent — ``logging.getLogger``
   caches by name, so a naive implementation would make the second extractor
   write into the first extractor's file.
"""

import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services" / "protocol-payment" / "common" / "logging_setup.py"
)
SPEC = importlib.util.spec_from_file_location("protocol_payment_logging_setup", MODULE_PATH)
LOGSETUP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = LOGSETUP
SPEC.loader.exec_module(LOGSETUP)


class StdoutIsolationTests(unittest.TestCase):
    @staticmethod
    def _close(logger):
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    def test_logging_writes_no_byte_to_stdout(self):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            logger = LOGSETUP.make_file_logger("probe", Path(tmp))
            try:
                captured = io.StringIO()
                with redirect_stdout(captured):
                    logger.info("hello stdout isolation")
                    logger.warning("warn line")
                self.assertEqual(captured.getvalue(), "")
            finally:
                self._close(logger)

    def test_logger_does_not_propagate_to_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = LOGSETUP.make_file_logger("probe", Path(tmp))
            try:
                self.assertFalse(logger.propagate)
                for handler in logger.handlers:
                    # FileHandler subclasses StreamHandler, so the discriminator
                    # is the *target*: a rotating file, never a console stream.
                    self.assertIsInstance(
                        handler, LOGSETUP.ResilientRotatingFileHandler
                    )
                    self.assertTrue(handler.baseFilename.startswith(tmp))
            finally:
                self._close(logger)


class ResilientRotationTests(unittest.TestCase):
    def _handler(self, path):
        return LOGSETUP.ResilientRotatingFileHandler(
            path, maxBytes=1, backupCount=1, encoding="utf-8", delay=True
        )

    def test_failed_rotation_degrades_to_appending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.log"
            handler = self._handler(path)
            # ``RotatingFileHandler.doRollover`` calls ``self.rotate`` (os.rename
            # by default); a held-open file is what makes it fail on Windows.
            handler.rotate = Mock(side_effect=PermissionError(13, "file is in use"))
            for index in range(5):
                record = logging.LogRecord(
                    "t", logging.INFO, __file__, 10, f"payload-{index}", (), None
                )
                handler.emit(record)
            handler.close()
            text = path.read_text(encoding="utf-8")
            for index in range(5):
                self.assertIn(f"payload-{index}", text)

    def test_the_first_failure_is_announced_through_logging(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.log"
            handler = self._handler(path)
            handler.rotate = Mock(side_effect=PermissionError(13, "file is in use"))
            records = []

            class Capture(logging.Handler):
                def emit(self, record):
                    records.append(record.getMessage())

            target = logging.getLogger(LOGSETUP.__name__)
            capture = Capture()
            old_level = target.level
            target.addHandler(capture)
            target.setLevel(logging.WARNING)
            try:
                for _ in range(3):
                    record = logging.LogRecord(
                        "t", logging.INFO, __file__, 10, "payload", (), None
                    )
                    handler.emit(record)
            finally:
                target.removeHandler(capture)
                target.setLevel(old_level)
                handler.close()
            announcements = [m for m in records if "log rotation failed" in m]
            self.assertEqual(len(announcements), 1, records)


class LoggerShapeTests(unittest.TestCase):
    @staticmethod
    def _close(logger):
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    def test_two_loggers_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = LOGSETUP.make_file_logger("alpha", Path(tmp))
            second = LOGSETUP.make_file_logger("beta", Path(tmp))
            try:
                self.assertIsNot(first, second)
                self.assertNotEqual(
                    first.handlers[0].baseFilename, second.handlers[0].baseFilename
                )
                first.info("only-alpha")
                second.info("only-beta")
                for handler in first.handlers + second.handlers:
                    handler.flush()
                alpha_text = Path(first.handlers[0].baseFilename).read_text(encoding="utf-8")
                beta_text = Path(second.handlers[0].baseFilename).read_text(encoding="utf-8")
                self.assertIn("only-alpha", alpha_text)
                self.assertNotIn("only-beta", alpha_text)
                self.assertIn("only-beta", beta_text)
            finally:
                self._close(first)
                self._close(second)

    def test_redact_callback_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = LOGSETUP.make_file_logger(
                "redact", Path(tmp), redact=lambda text: text.replace("secret", "***")
            )
            try:
                logger.info("the secret value")
                for handler in logger.handlers:
                    handler.flush()
                text = Path(logger.handlers[0].baseFilename).read_text(encoding="utf-8")
                self.assertIn("the *** value", text)
                self.assertNotIn("secret", text)
            finally:
                self._close(logger)

    def test_line_shape_matches_operator_convention(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = LOGSETUP.make_file_logger("shapetest", Path(tmp))
            try:
                logger.info("hello")
                for handler in logger.handlers:
                    handler.flush()
                line = Path(logger.handlers[0].baseFilename).read_text(encoding="utf-8").strip()
                # HH:MM:SS [*] [shapetest] hello
                self.assertRegex(line, r"^\d{2}:\d{2}:\d{2} \[\*\] \[shapetest\] hello$")
            finally:
                self._close(logger)


if __name__ == "__main__":
    unittest.main()

