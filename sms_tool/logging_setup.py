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
    """Resolve ``<runtime dir>/logs/sms_tool.log``.

    ``paths.runtime_file(cfg, filename)`` takes the **config** as its first
    argument, not a directory. The previous call passed ``"logs"`` there, which
    raised ``AttributeError`` on ``cfg.get``; the broad ``except Exception``
    then silently fell back to a repo-root ``logs/`` directory. It went
    unnoticed for as long as ``configure_logging`` had zero callers.
    Resolve via ``runtime_dir`` so the log lands in the git-ignored runtime tree.
    """
    from .paths import PROJECT_ROOT

    try:
        from .config import current_config_data
        from .paths import runtime_dir

        directory = runtime_dir(current_config_data()) / "logs"
    except Exception:  # pragma: no cover - defensive fallback only
        directory = PROJECT_ROOT / "runtime" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "sms_tool.log"


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
    except Exception as exc:  # pragma: no cover - last-resort only
        # Never let logging setup crash the application. Broad on purpose:
        # OSError (permissions/full disk) is the expected case, but a broken
        # path resolver must not take the CLI down either -- it reports and
        # continues, which is strictly better than the old silent fallback.
        print(f"[logging] could not open log file: {exc}")

    if to_console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    _CONFIGURED = True
