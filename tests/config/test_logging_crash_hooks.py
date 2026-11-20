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

pytest style since 2026-11-20 (`unittest.TestCase` -> fixture): the shared setUp
became the ``hooks`` fixture, so the two former classes are simply two groups of
tests over the same state, and the restore path runs even when a test fails.
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass, field
from unittest import mock

import pytest

from sms_tool import logging_setup


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def events(self) -> list[str]:
        return [getattr(record, "event", "") for record in self.records]


@dataclass
class HookState:
    """What the former ``setUp`` installed, plus the spies the tests assert on."""

    saved_excepthook: object
    saved_thread_hook: object
    saved_installed_flag: bool
    capture: _Capture
    previous_calls: list[tuple] = field(default_factory=list)
    previous_thread_calls: list[object] = field(default_factory=list)

    def install(self):
        logging_setup._HOOKS_INSTALLED[0] = False
        logging_setup._install_exception_hooks()
        return sys.excepthook

    def fire(self, exc: BaseException) -> None:
        try:
            raise exc
        except type(exc):
            exc_type, exc_value, exc_tb = sys.exc_info()
            assert exc_type is not None and exc_value is not None
            sys.excepthook(exc_type, exc_value, exc_tb)


@pytest.fixture
def hooks():
    """Install the crash hooks over spies and restore every global on teardown.

    The hooks are process-global; leaving them installed would leak into every
    later test in the session. Both the main-thread and the thread hook are
    replaced: leaving the default thread hook in place would print a traceback
    and make pytest raise its own ``PytestUnhandledThreadExceptionWarning``.
    """
    state = HookState(
        saved_excepthook=sys.excepthook,
        saved_thread_hook=threading.excepthook,
        saved_installed_flag=logging_setup._HOOKS_INSTALLED[0],
        capture=_Capture(),
    )
    crash_logger = logging.getLogger("sms_tool.crash")
    crash_logger.addHandler(state.capture)
    sys.excepthook = lambda *args: state.previous_calls.append(args)
    threading.excepthook = lambda args: state.previous_thread_calls.append(args)
    try:
        yield state
    finally:
        sys.excepthook = state.saved_excepthook
        threading.excepthook = state.saved_thread_hook
        logging_setup._HOOKS_INSTALLED[0] = state.saved_installed_flag
        crash_logger.removeHandler(state.capture)


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def test_the_hook_is_installed(hooks):
    hook = hooks.install()
    assert hook is not hooks.saved_excepthook


def test_installing_twice_does_not_double_wrap(hooks):
    first = hooks.install()
    logging_setup._install_exception_hooks()
    assert sys.excepthook is first, "the hook must be idempotent"


def test_configure_logging_installs_the_hooks_even_when_already_configured(hooks):
    """A process that configured logging elsewhere still needs the hooks."""
    logging_setup._HOOKS_INSTALLED[0] = False
    with mock.patch.object(logging_setup, "_CONFIGURED", True):
        logging_setup.configure_logging()
    assert logging_setup._HOOKS_INSTALLED[0]
    assert sys.excepthook is not hooks.saved_excepthook


# ---------------------------------------------------------------------------
# The crash record itself
# ---------------------------------------------------------------------------


def test_an_uncaught_exception_is_logged_as_a_critical_record(hooks):
    hooks.install()
    hooks.fire(ValueError("boom"))

    crashes = [record for record in hooks.capture.records if getattr(record, "event", "") == "uncaught_exception"]
    assert len(crashes) == 1
    assert crashes[0].levelno == logging.CRITICAL
    assert "boom" in crashes[0].getMessage()
    assert crashes[0].exc_info is not None, "the traceback must travel with the record"


def test_the_previous_hook_still_runs(hooks):
    hooks.install()
    hooks.fire(KeyError("kept"))
    assert len(hooks.previous_calls) == 1, "the stderr channel must survive"


def test_keyboard_interrupt_is_not_logged_as_a_crash(hooks):
    hooks.install()
    hooks.fire(KeyboardInterrupt())

    assert "uncaught_exception" not in hooks.capture.events()
    assert len(hooks.previous_calls) == 1, "Ctrl-C must still reach the original hook"


def test_a_thread_exception_is_logged(hooks):
    hooks.install()
    thread = threading.Thread(target=lambda: None, name="worker-7")
    threading.excepthook(threading.ExceptHookArgs((ValueError, ValueError("thread boom"), None, thread)))
    assert "uncaught_thread_exception" in hooks.capture.events()


def test_the_original_thread_hook_still_runs(hooks):
    hooks.install()
    thread = threading.Thread(target=lambda: None, name="worker-8")
    threading.excepthook(threading.ExceptHookArgs((ValueError, ValueError("x"), None, thread)))
    assert len(hooks.previous_thread_calls) == 1, "the original thread channel must survive"
