"""Guards for the rotating file logger (round 6).

``sms_tool/logging_setup.py`` spent its whole life with **zero callers**, and two
bugs hid inside it as a result:

1. ``configure_logging`` was never wired into any entry point, so every
   ``logger.*`` call in the package went nowhere (the codebase reports through
   745 ``print()`` calls and had 0 ``logger.exception``).
2. ``_default_log_path`` called ``paths.runtime_file(cfg, filename)`` passing the
   *directory* string ``"logs"`` where the *config* was expected. That raised
   ``AttributeError`` on ``cfg.get``, which a broad ``except Exception`` silently
   swallowed by falling back to a repo-root ``logs/`` directory.

Both are now fixed. These tests lock the fixes so neither can regress quietly.
"""
import inspect
import logging
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import Mock

from sms_tool import cli, logging_setup

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DefaultLogPathTests(unittest.TestCase):
    def test_path_is_named_sms_tool_log_under_a_logs_dir(self):
        path = logging_setup._default_log_path()
        self.assertEqual(path.name, "sms_tool.log")
        self.assertEqual(path.parent.name, "logs")

    def test_path_lives_under_the_runtime_tree_not_the_repo_root(self):
        """The old fallback polluted <repo>/logs/ because of the config/dir mix-up."""
        path = logging_setup._default_log_path()
        parts = [p.lower() for p in path.parts]
        self.assertIn("runtime", parts)
        # Guard the specific regression: the resolved path must not sit directly
        # inside the repository root.
        self.assertNotEqual(path.parent.parent.resolve(), PROJECT_ROOT)


class ResilientRotationTests(unittest.TestCase):
    """A failed rotation must degrade to appending, never to silent data loss.

    ``RotatingFileHandler.emit`` hands a rotation ``OSError`` to
    ``logging.Handler.handleError``, which writes to stderr and *discards* the
    record. On Windows the rename is refused while another process holds the
    file open, and the size stays above ``maxBytes`` -- so every later record
    fails the same way and the channel stops permanently. That is exactly what
    happened to ``runtime/logs/sms_tool.jsonl``: it reached 5,242,694 of its
    5,242,880-byte cap at 2026-09-13 10:50 and never received another record,
    while the human ``sms_tool.log`` kept writing.
    """

    def _handler(self, path):
        return logging_setup.ResilientRotatingFileHandler(
            path, maxBytes=1, backupCount=1, encoding="utf-8", delay=True
        )

    def test_records_survive_a_failing_rotation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sms_tool.jsonl"
            handler = self._handler(path)
            # ``RotatingFileHandler.doRollover`` calls ``self.rotate`` (os.rename
            # by default); a held-open file is what makes it fail on Windows.
            handler.rotate = Mock(side_effect=PermissionError(13, "file is in use"))
            logger = logging.getLogger("resilient-rotation-test")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.addHandler(handler)
            try:
                logger.info("first")
                logger.info("second")
                handler.flush()
            finally:
                logger.removeHandler(handler)
                handler.close()

            content = path.read_text(encoding="utf-8")
            # Both land: the record whose rotation failed *and* the next one,
            # which the stock handler would have dropped as well.
            self.assertIn("first", content)
            self.assertIn("second", content)
            self.assertEqual(1, handler._rollover_failures)

    def test_the_first_failure_is_announced_through_logging(self):
        """Silence is the other half of the bug: the stop was invisible."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sms_tool.log"
            # The file has to be non-empty before the rollover is attempted.
            # CPython 3.12 added "never roll over an empty file" (gh-116263):
            # ``shouldRollover`` now returns False as soon as
            # ``self.stream.tell()`` is 0, where 3.11 computed
            # ``0 + len(msg) >= maxBytes`` and rolled over anyway. With an empty
            # file the rollover is never attempted on 3.12, so no warning can
            # exist and the only symptom is the assertion below reporting an
            # empty list -- which blames the announcement instead of the missing
            # precondition. This test was green on a 3.11 interpreter and red on
            # the 3.12 CI runner for exactly that reason.
            path.write_text("seed\n", encoding="utf-8")
            handler = self._handler(path)
            handler.rotate = Mock(side_effect=PermissionError(13, "file is in use"))
            captured = []

            class Capture(logging.Handler):
                def emit(self, record):
                    captured.append(record)

            capture = Capture()
            root = logging.getLogger()
            root.addHandler(capture)
            logger = logging.getLogger("resilient-rotation-announce-test")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.addHandler(handler)
            try:
                logger.info("boom")
            finally:
                logger.removeHandler(handler)
                handler.close()
                root.removeHandler(capture)

            self.assertEqual(
                1,
                handler._rollover_failures,
                "no rollover was attempted, so nothing about the announcement "
                "was exercised",
            )
            messages = [
                record.getMessage()
                for record in captured
                if record.levelno >= logging.WARNING
            ]
            self.assertTrue(
                any("log rotation failed" in message for message in messages),
                f"no rotation warning reached the logging system: {messages}",
            )
            self.assertTrue(
                any(str(handler.baseFilename) in message for message in messages),
                f"the warning does not name the degraded file: {messages}",
            )

    def test_repeated_rotation_failures_are_announced_once(self):
        """One announcement per degraded handler, not one per retry cycle.

        A warning per retry would write to the very log it is warning about,
        and it would drown the single line an operator needs to see.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = self._handler(Path(temp_dir) / "sms_tool.log")
            handler.rotate = Mock(side_effect=PermissionError(13, "file is in use"))
            captured = []

            class Capture(logging.Handler):
                def emit(self, record):
                    captured.append(record)

            capture = Capture()
            root = logging.getLogger()
            root.addHandler(capture)
            logger = logging.getLogger("resilient-rotation-repeat-test")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.addHandler(handler)
            try:
                # Enough records to cross the backoff window and attempt a
                # second rollover, which fails the same way.
                for index in range(logging_setup._ROLLOVER_RETRY_EVERY + 2):
                    logger.info("record-%d", index)
                handler.flush()
            finally:
                logger.removeHandler(handler)
                handler.close()
                root.removeHandler(capture)

            warnings = [
                record.getMessage()
                for record in captured
                if record.levelno >= logging.WARNING and "log rotation failed" in record.getMessage()
            ]
            self.assertEqual(1, len(warnings), warnings)
            self.assertGreaterEqual(handler._rollover_failures, 2)


class ConfigureLoggingTests(unittest.TestCase):
    def setUp(self):
        self._orig_configured = logging_setup._CONFIGURED
        self._orig_handlers = list(logging.getLogger().handlers)
        self._orig_level = logging.getLogger().level
        # `configure_logging` is idempotent via a module-level flag, and an
        # earlier test that goes through cli.main() already sets it. Clear it so
        # every case below exercises a real first call instead of a no-op --
        # otherwise this file is green alone and red in the full run.
        logging_setup._CONFIGURED = False
        self.addCleanup(self._restore)

    def _restore(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler not in self._orig_handlers:
                handler.close()
        root.handlers[:] = self._orig_handlers
        root.setLevel(self._orig_level)
        logging_setup._CONFIGURED = self._orig_configured

    def test_installs_a_rotating_file_handler(self):
        # Count only what this call adds. An earlier test that goes through
        # cli.main() already wired logging once, and the root logger may carry
        # handlers installed by pytest itself; asserting on the absolute count
        # is green alone and red in the full run.
        before = set(map(id, logging.getLogger().handlers))
        logging_setup.configure_logging(to_console=False)
        rotating = [
            h
            for h in logging.getLogger().handlers
            if id(h) not in before and isinstance(h, RotatingFileHandler)
        ]
        # Human stage log (sms_tool.log) + machine envelope log (sms_tool.jsonl).
        self.assertEqual(len(rotating), 2)
        names = sorted(Path(h.baseFilename).name for h in rotating)
        self.assertEqual(names, ["sms_tool.jsonl", "sms_tool.log"])
        for handler in rotating:
            self.assertGreater(handler.maxBytes, 0)
            self.assertGreater(handler.backupCount, 0)

    def test_human_log_normalizes_stages_and_jsonl_keeps_the_envelope(self):
        before = set(map(id, logging.getLogger().handlers))
        logging_setup.configure_logging(to_console=False)
        added = [
            h
            for h in logging.getLogger().handlers
            if id(h) not in before and isinstance(h, RotatingFileHandler)
        ]
        by_suffix = {Path(h.baseFilename).suffix: h for h in added}
        logging.getLogger("sms_tool.registration_progress").info(
            "Registration stage=%s status=%s", "create_account", "running"
        )
        for handler in added:
            handler.flush()
        try:
            human = Path(by_suffix[".log"].baseFilename).read_text(encoding="utf-8", errors="replace")
            machine = Path(by_suffix[".jsonl"].baseFilename).read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - environment cannot host the file
            self.skipTest("log files are not readable in this environment")
        self.assertIn("阶段 · 创建账号 (create_account) — 进行中", human)
        self.assertNotIn("schema_version", human)
        self.assertIn('"schema_version": 1', machine)

    def test_is_idempotent(self):
        logging_setup.configure_logging(to_console=False)
        after_first = len(logging.getLogger().handlers)
        logging_setup.configure_logging(to_console=False)
        self.assertEqual(len(logging.getLogger().handlers), after_first)

    def test_both_channels_use_the_resilient_handler(self):
        before = set(map(id, logging.getLogger().handlers))
        logging_setup.configure_logging(to_console=False)
        added = [
            h
            for h in logging.getLogger().handlers
            if id(h) not in before and isinstance(h, RotatingFileHandler)
        ]
        self.assertTrue(added)
        for handler in added:
            self.assertIsInstance(handler, logging_setup.ResilientRotatingFileHandler)

    def test_one_unopenable_channel_does_not_silence_the_other(self):
        """Both channels used to share a single ``try``.

        A ``.log`` path that cannot be opened (here: a directory) must not stop
        the machine channel from being installed.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            blocker = Path(temp_dir) / "sms_tool.log"
            blocker.mkdir()
            before = set(map(id, logging.getLogger().handlers))
            logging_setup.configure_logging(log_path=blocker, to_console=False)
            added = [
                h
                for h in logging.getLogger().handlers
                if id(h) not in before and isinstance(h, RotatingFileHandler)
            ]
            try:
                self.assertEqual(
                    ["sms_tool.jsonl"], sorted(Path(h.baseFilename).name for h in added)
                )
            finally:
                # Close before the temporary directory is removed: an open
                # handle makes the Windows cleanup fail, not the assertion.
                for handler in added:
                    logging.getLogger().removeHandler(handler)
                    handler.close()

    def test_to_console_false_keeps_stdout_clean_for_the_wpf_ipc_channel(self):
        """stdout carries the ``@@SMSWORKBENCH_V2@@`` envelope; no formatter noise."""
        # Only count handlers this call adds -- pytest's logging plugin installs
        # handlers of its own, and counting every StreamHandler on the root
        # logger would assert on somebody else's setup.
        before = set(map(id, logging.getLogger().handlers))
        logging_setup.configure_logging(to_console=False)
        added_stream_handlers = [
            h
            for h in logging.getLogger().handlers
            if id(h) not in before
            and isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
        ]
        self.assertEqual(added_stream_handlers, [])

    def test_to_console_true_still_available_for_local_debugging(self):
        before = set(map(id, logging.getLogger().handlers))
        logging_setup.configure_logging(to_console=True)
        added_stream_handlers = [
            h
            for h in logging.getLogger().handlers
            if id(h) not in before
            and isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
        ]
        self.assertEqual(len(added_stream_handlers), 1)

    def test_records_are_written_to_the_log_file(self):
        logging_setup.configure_logging(to_console=False)
        rotating = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)][0]
        marker = "logging-setup-guard-marker"
        logging.getLogger("sms_tool.test").warning(marker)
        rotating.flush()
        try:
            content = Path(rotating.baseFilename).read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - environment cannot host the file
            self.skipTest("log file is not readable in this environment")
        self.assertIn(marker, content)

    def test_warnings_module_is_captured_and_formatted(self):
        """Library warnings must render as ``[!] [告警] ...`` log lines instead of
        raw ``path:line:`` stderr noise leaking into the WPF output panel."""
        import warnings as warnings_module

        # configure_logging wires warnings -> logging via captureWarnings.
        source = Path(logging_setup.__file__).read_text(encoding="utf-8")
        self.assertIn("captureWarnings(True)", source)

        # pytest installs its own showwarning during test calls, so drive the
        # exact integration point captureWarnings uses: py.warnings receives
        # warnings.formatwarning() output.
        before = set(map(id, logging.getLogger().handlers))
        logging_setup.configure_logging(to_console=False)
        added = [
            h
            for h in logging.getLogger().handlers
            if id(h) not in before and isinstance(h, RotatingFileHandler)
        ]
        by_suffix = {Path(h.baseFilename).suffix: h for h in added}
        message = warnings_module.formatwarning(
            "captured-warning-marker", UserWarning, __file__, 1
        )
        logging.getLogger("py.warnings").warning("%s", message)
        for handler in added:
            handler.flush()
        try:
            human = Path(by_suffix[".log"].baseFilename).read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - environment cannot host the file
            self.skipTest("log files are not readable in this environment")
        self.assertIn("[!] [告警] UserWarning: captured-warning-marker", human)
        self.assertNotIn("test_logging_setup.py:", human)


class EntryPointWiringTests(unittest.TestCase):
    def test_cli_main_calls_configure_logging(self):
        """Round 6 P0: the module was dead code until this wiring existed."""
        source = inspect.getsource(cli.main)
        self.assertIn("configure_logging", source)

    def test_wiring_requests_no_console_handler(self):
        """A StreamHandler would inject formatter lines into the WPF IPC stream."""
        source = inspect.getsource(cli.main)
        self.assertIn("to_console=False", source)


if __name__ == "__main__":
    unittest.main()
