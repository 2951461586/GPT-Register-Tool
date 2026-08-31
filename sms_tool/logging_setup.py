"""Central logging configuration for ``sms_tool``.

Call :func:`configure_logging` once at process start (CLI entry points such as
``chatgpt_phone_reg.py`` / ``cli``). It installs a :class:`RotatingFileHandler`
that writes to ``runtime/logs/sms_tool.log`` (size-capped, rotated) so Python-side
output is observable instead of being swallowed by the WPF stdout capture. The
previous state was 534 ``print()`` calls with zero rotation and zero persistence.

The call is idempotent: a second invocation is a no-op.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
_DEFAULT_BACKUPS = 5
_ROOT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _default_log_path() -> Path:
    # runtime/ is git-ignored local state; fall back to a local logs/ dir if unavailable.
    try:
        from .paths import runtime_file

        return runtime_file("logs", "sms_tool.log")
    except Exception:  # pragma: no cover - defensive fallback only
        return Path("logs") / "sms_tool.log"


def configure_logging(
    *,
    level: int = logging.INFO,
    log_path=None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backups: int = _DEFAULT_BACKUPS,
    to_console: bool = True,
) -> None:
    """Configure root logging exactly once.

    Args:
        level: root logger level.
        log_path: override the log file location.
        max_bytes: rotate once the file reaches this size.
        backups: number of rotated ``.log.N`` files to keep.
        to_console: also attach a StreamHandler (the WPF host captures stdout).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(_ROOT_FORMAT)

    try:
        path = Path(log_path) if log_path else _default_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:  # pragma: no cover - last-resort only
        # Never let logging setup crash the application.
        print(f"[logging] could not open log file: {exc}")

    if to_console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    _CONFIGURED = True
