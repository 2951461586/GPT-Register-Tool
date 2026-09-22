"""Persist the backend's stdout/stderr so ``print()`` diagnostics survive.

The protocol lane reports almost everything through ``print()``, while
``runtime/logs/sms_tool.log`` only receives ``logger.*`` records. The desktop host
captures stdout for the IPC channel and never writes it to disk, so the only lines
that explain a protocol failure -- ``Existing account OTP send: /resend 409``,
``Account already exists``, ``Email OTP validate failed: ... 409`` -- were lost
entirely. A batch that died before its first progress record (2026-09-13 20:46:
``exited with code 2`` after 10m51s) therefore left no evidence at all, and the
operator log showed nothing between "starting" and "exited".

This module mirrors every complete line of process output to
``runtime/logs/processes/<pid>/backend_stdout.jsonl`` with correlation metadata.

Two deliberate constraints:

- **Passive only.** stdout is the WPF IPC channel (the ``@@SMSWORKBENCH_V2@@``
  envelope) and the host matches line *prefixes*
  (``tests/test_backend_log_noise_contract.py``), so the mirror observes the
  stream and never reroutes, decorates, or reorders it. Converting ``print`` to
  ``logger`` would break that contract; mirroring does not.
- **Sanitized input only.** ``SanitizingTextIO`` feeds the mirror the text *after*
  ``sanitize_log_text``, so the new file inherits the same redaction policy as
  every other operator-visible channel instead of becoming a fresh leak surface.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from .logging_setup import CorrelatedJsonFormatter, ResilientRotatingFileHandler, process_log_dir

_LOGGER_NAME = "sms_tool.backend_stdout"
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUPS = 3


def default_stdout_log_path() -> Path:
    """Per-process mirror; old shared log files remain untouched."""
    return process_log_dir() / "backend_stdout.jsonl"


class StdoutMirror:
    """Line sink backed by a dedicated, non-propagating logger.

    Reusing :class:`ResilientRotatingFileHandler` buys the rotation-without-loss
    behaviour already proven for ``sms_tool.jsonl``, and ``propagate=False`` keeps
    these lines out of ``sms_tool.log``/``.jsonl`` -- the mirror is a third
    channel, not a duplicate of the other two.
    """

    def __init__(
        self,
        path=None,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backups: int = _DEFAULT_BACKUPS,
    ) -> None:
        self.path = Path(path) if path else default_stdout_log_path()
        self._buffers = threading.local()
        # A standalone ``logging.Logger`` rather than ``getLogger(...)``: the
        # manager caches by name, so a second mirror in the same process would
        # silently reuse the first one's handler and write to the wrong file
        # (which is exactly what a test with a tmp_path would catch, and what a
        # re-install after a config reload would hit in production).
        self._logger = logging.Logger(_LOGGER_NAME, level=logging.INFO)
        self._logger.propagate = False
        handler = ResilientRotatingFileHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        # Only the disk copy is decorated. The desktop IPC stream stays byte-identical.
        handler.setFormatter(CorrelatedJsonFormatter("%(message)s"))
        self._logger.addHandler(handler)
        self._handler = handler

    # -- buffering ---------------------------------------------------------
    def _pending(self) -> str:
        try:
            return self._buffers.pending
        except AttributeError:
            self._buffers.pending = ""
            return ""

    def write(self, text: str) -> None:
        """Feed arbitrary text; emit complete lines, buffer the trailing partial."""
        pending = self._pending() + text
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            self._emit(line)
        self._buffers.pending = pending

    def flush(self) -> None:
        pending = self._pending()
        if pending:
            self._emit(pending)
            self._buffers.pending = ""

    def close(self) -> None:
        self.flush()
        try:
            self._handler.close()
        except Exception:
            pass

    # -- emission ----------------------------------------------------------
    def _emit(self, line: str) -> None:
        if not line:
            return
        # Reentrancy guard: ``logging`` reports a broken handler through
        # ``handleError``, which writes to ``sys.stderr`` -- i.e. straight back
        # into this mirror. Without the guard a single disk error becomes an
        # unbounded recursion.
        if getattr(self._buffers, "emitting", False):
            return
        self._buffers.emitting = True
        try:
            self._logger.info(line)
        except Exception:
            # The mirror is an observer. Losing a line is bad; taking down the
            # run because the observer failed is worse.
            pass
        finally:
            self._buffers.emitting = False


def install_stdout_mirror(path=None) -> StdoutMirror:
    """Attach a mirror to the current ``sys.stdout``/``sys.stderr``.

    Streams that are not :class:`~sms_tool.diagnostics.SanitizingTextIO` (an
    already-captured pytest stream, a plain file) are left untouched: without
    ``set_mirror`` there is nothing to attach to, and wrapping them here would
    change the object the caller holds.
    """
    mirror = StdoutMirror(path)
    for stream in (sys.stdout, sys.stderr):
        setter = getattr(stream, "set_mirror", None)
        if callable(setter):
            setter(mirror)
    return mirror
