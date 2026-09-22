"""Logging conventions for the ``services/protocol-payment`` process boundary.

This package is a separate process from ``sms_tool`` (Boundary Rule 10: it must
not import ``sms_tool``), so it cannot reuse ``sms_tool.logging_setup``. This
module is the services-side equivalent: a minimal, dependency-free convention
that every extractor can adopt without changing a single byte of stdout.

Contract with the desktop host
------------------------------
The WPF frontend parses the child process **stdout** for both the structured
``@@SMSWORKBENCH_V2@@`` envelope and human-readable progress lines. Any logging
wired here must therefore write to **files** (and optionally stderr), never to
stdout. ``make_file_logger`` honours that: the returned logger uses a private
``logging.Logger`` instance with ``propagate=False`` and only file handlers.

What this provides
------------------
- :class:`ResilientRotatingFileHandler` — a size-capped rotating handler whose
  failed rotation (Windows: another process holds the file) degrades to
  *appending* instead of silently dropping every subsequent record.
- :func:`make_file_logger` — a per-extractor logger writing one normalised,
  redacted line per record to a per-process log file.

What this deliberately does NOT provide
---------------------------------------
- No stdout mirroring. Extractors keep their existing ``log()``/``print()``
  for the IPC channel; this module only adds a *durable, rotated* copy.
- No hard dependency on ``redaction``: pass ``redact=`` to plug in the
  extractor's own redactor, or omit it (the line is written verbatim).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
_DEFAULT_BACKUPS = 5

_LEVEL_MARKERS = {
    logging.DEBUG: "[.]",
    logging.INFO: "[*]",
    logging.WARNING: "[!]",
    logging.ERROR: "[x]",
    logging.CRITICAL: "[x]",
}


class HumanFormatter(logging.Formatter):
    """``HH:MM:SS [*] [logger] message`` — mirrors the sms_tool operator shape."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if record.exc_info and not message.endswith(str(record.exc_info[1])):
            message = f"{message} ({record.exc_info[0].__name__}: {record.exc_info[1]})"
        marker = _LEVEL_MARKERS.get(record.levelno, "[*]")
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        name = record.name.rpartition(".")[2] or record.name or "-"
        return f"{stamp} {marker} [{name}] {message}"



class ResilientRotatingFileHandler(RotatingFileHandler):
    """A rotating handler that never lets a failed rotation drop a record.

    ``RotatingFileHandler.emit`` routes a rotation ``OSError`` into
    ``logging.Handler.handleError``, which writes to stderr and *discards* the
    record. On Windows the rename is refused while another process holds the
    file open; because the size stays above ``maxBytes`` every later record
    fails the same way, so the channel stops permanently and silently.

    Here a failed rotation appends anyway (``FileHandler.emit`` reopens the
    ``None`` stream that ``doRollover`` left behind), announces the degradation
    once through ``logging``, and retries rotation on a backoff so the file
    recovers on its own once the other process closes its handle.
    """

    _ROLLOVER_RETRY_EVERY = 200

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rollover_failures = 0
        self._records_since_failure = 0

    def shouldRollover(self, record: logging.LogRecord) -> int:
        if self._rollover_failures:
            self._records_since_failure += 1
            if self._records_since_failure < self._ROLLOVER_RETRY_EVERY:
                return 0
            self._records_since_failure = 0
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError as exc:
            first = not self._rollover_failures
            self._rollover_failures += 1
            if first:
                # Re-entering this handler is safe: the failure counter is now
                # non-zero, so ``shouldRollover`` answers 0 for the warning
                # record and it is written normally.
                logging.getLogger(__name__).warning(
                    "log rotation failed for %s (%s); appending without rotation",
                    self.baseFilename,
                    exc,
                )
        else:
            self._rollover_failures = 0


def make_file_logger(
    name: str,
    log_dir: Path,
    *,
    level: int = logging.INFO,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backups: int = _DEFAULT_BACKUPS,
    redact: Optional[Callable[[str], str]] = None,
) -> logging.Logger:
    """Return a per-extractor logger writing redacted lines to a rotated file.

    Each call builds an **independent** ``logging.Logger`` (not ``getLogger``,
    which caches by name and would make a second extractor reuse the first's
    handler and write to the wrong file). The logger has ``propagate=False`` so
    it never leaks into the root handlers or — critically — stdout.

    The log file is per-process (``<log_dir>/<name>_<pid>_<timestamp>.log``) so
    concurrent extractors never hold each other's file open, which is what makes
    rotation reliable on Windows.
    """
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{name}_{os.getpid()}_{stamp}.log"

    logger = logging.Logger(name, level)
    logger.propagate = False

    handler = ResilientRotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True
    )
    if redact is not None:
        _redact = redact

        class _RedactingFormatter(HumanFormatter):
            def format(self, record: logging.LogRecord) -> str:
                return _redact(super().format(record))

        handler.setFormatter(_RedactingFormatter())
    else:
        handler.setFormatter(HumanFormatter())
    logger.addHandler(handler)
    return logger
