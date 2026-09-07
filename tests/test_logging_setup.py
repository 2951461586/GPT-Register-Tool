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
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
