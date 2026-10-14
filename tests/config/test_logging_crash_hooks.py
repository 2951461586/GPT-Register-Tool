"""Uncaught exceptions must reach the log file, not just stderr (2026-09-22).

Why this exists
---------------
``configure_logging`` installs the rotating ``sms_tool.log`` and the correlated
``sms_tool.jsonl``, but nothing routed *uncaught* exceptions into them. A crash
therefore stopped the log at the last deliberate line and left the traceback on
stderr only -- for the WPF host (which captures stdout) that is a bare traceback
with no context, and for tooling it is a run that simply "ended".

Two properties matter and are both pinned below:

* the crash record is emitted through the logging stack (so it lands in **both**
  files, with the ``event`` key the JSON formatter needs);
* the previous hook still runs, so the operator's familiar stderr traceback is
  unchanged -- this adds a record, it does not replace a channel.

``KeyboardInterrupt`` is excluded on purpose: Ctrl-C is a normal stop, and
logging it as a crash would make every cancelled run look like a fault.
"""

from __future__ import annotations

import logging
import sys
import threading
import unittest

from sms_tool import logging_setup


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def events(self) -> list[str]:
        return [getattr(record, "event", "") for record in self.records]


class ExceptionHookTests(unittest.TestCase):
    def setUp(self):
        # The hooks are global; leaving them installed would leak into every
        # later test in the session.
        self._saved_excepthook = sys.excepthook
        self._saved_thread_hook = threading.excepthook
        self._saved_installed_flag = logging_setup._HOOKS_INSTALLED[0]
        self.addCleanup(self._restore_globals)

        self.capture = _Capture()
        crash_logger = logging.getLogger("sms_tool.crash")
        crash_logger.addHandler(self.capture)
        self.addCleanup(crash_logger.removeHandler, self.capture)

        # Spy on the "previous" handlers so we can prove they still run.  Both
        # are replaced, not just the main-thread one: leaving the default thread
        # hook in place would print a traceback and make pytest raise its own
        # PytestUnhandledThreadExceptionWarning.
        self.previous_calls = []
        self.previous_thread_calls = []
        sys.excepthook = lambda *args: self.previous_calls.append(args)
        threading.excepthook = lambda args: self.previous_thread_calls.append(args)

    def _restore_globals(self):
        sys.excepthook = self._saved_excepthook
        threading.excepthook = self._saved_thread_hook
        logging_setup._HOOKS_INSTALLED[0] = self._saved_installed_flag

    def _install(self):
        logging_setup._HOOKS_INSTALLED[0] = False
        logging_setup._install_exception_hooks()
        return sys.excepthook

    def _fire(self, exc: BaseException):
        try:
            raise exc
        except type(exc):
            sys.excepthook(*sys.exc_info())


class InstallationTests(ExceptionHookTests):
    def test_the_hook_is_installed(self):
        hook = self._install()
        self.assertIsNot(hook, self._saved_excepthook)

    def test_installing_twice_does_not_double_wrap(self):
        first = self._install()
        logging_setup._install_exception_hooks()
        self.assertIs(sys.excepthook, first, "the hook must be idempotent")

    def test_configure_logging_installs_the_hooks_even_when_already_configured(self):
        """A process that configured logging elsewhere still needs the hooks."""
        from unittest import mock

        logging_setup._HOOKS_INSTALLED[0] = False
        with mock.patch.object(logging_setup, "_CONFIGURED", True):
            logging_setup.configure_logging()
        self.assertTrue(logging_setup._HOOKS_INSTALLED[0])
        self.assertIsNot(sys.excepthook, self._saved_excepthook)


class CrashRecordTests(ExceptionHookTests):
    def test_an_uncaught_exception_is_logged_as_a_critical_record(self):
        self._install()
        self._fire(ValueError("boom"))

        crashes = [r for r in self.capture.records if getattr(r, "event", "") == "uncaught_exception"]
        self.assertEqual(len(crashes), 1)
        self.assertEqual(crashes[0].levelno, logging.CRITICAL)
        self.assertIn("boom", crashes[0].getMessage())
        self.assertIsNotNone(crashes[0].exc_info, "the traceback must travel with the record")

    def test_the_previous_hook_still_runs(self):
        self._install()
        self._fire(KeyError("kept"))
        self.assertEqual(len(self.previous_calls), 1, "the stderr channel must survive")

    def test_keyboard_interrupt_is_not_logged_as_a_crash(self):
        self._install()
        self._fire(KeyboardInterrupt())

        self.assertNotIn("uncaught_exception", self.capture.events())
        self.assertEqual(len(self.previous_calls), 1, "Ctrl-C must still reach the original hook")

    def test_a_thread_exception_is_logged(self):
        self._install()
        thread = threading.Thread(target=lambda: None, name="worker-7")
        threading.excepthook(
            threading.ExceptHookArgs((ValueError, ValueError("thread boom"), None, thread))
        )
        self.assertIn("uncaught_thread_exception", self.capture.events())

    def test_the_original_thread_hook_still_runs(self):
        self._install()
        thread = threading.Thread(target=lambda: None, name="worker-8")
        threading.excepthook(
            threading.ExceptHookArgs((ValueError, ValueError("x"), None, thread))
        )
        self.assertEqual(len(self.previous_thread_calls), 1, "the original thread channel must survive")


if __name__ == "__main__":
    unittest.main()
