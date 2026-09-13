"""Central logging configuration for ``sms_tool``.

Call :func:`configure_logging` once at process start (CLI entry points such as
``chatgpt_phone_reg.py`` / ``cli``). It installs two size-capped, rotated
handlers under ``runtime/logs/`` so Python-side output is observable instead of
being swallowed by the WPF stdout capture:

- ``sms_tool.log`` — the operator log. :class:`HumanLogFormatter` renders one
  normalized line per record (``HH:MM:SS [*] [模块] 消息``), translates
  registration stage records into named phases, and never prints envelope
  metadata (schema_version/command_id/run_id) or raw JSON.
- ``sms_tool.jsonl`` — the machine log. :class:`CorrelatedJsonFormatter` keeps
  the full telemetry envelope for tooling and audits.

The previous state was 534 ``print()`` calls with zero rotation and zero
persistence. The call is idempotent: a second invocation is a no-op.
"""
from __future__ import annotations

import logging
import json
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
_DEFAULT_BACKUPS = 5
_ROOT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_STRUCTURED_RECORD_FIELDS = (
    "event",
    "stage",
    "status",
    "previous_stage",
    "previous_stage_duration_ms",
    "duration_ms",
    "driver",
    "failure_code",
    "failure_class",
    "batch_id",
    "account_ref",
)

class CorrelatedJsonFormatter(logging.Formatter):
    """One JSON envelope; sanitize both message arguments and exception text."""

    def format(self, record: logging.LogRecord) -> str:
        from .sanitizer import sanitize, sanitize_log_text
        from .telemetry import correlation_fields

        payload = {
            **correlation_fields(),
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize_log_text(super().format(record)),
        }
        for field in _STRUCTURED_RECORD_FIELDS:
            if hasattr(record, field):
                payload[field] = sanitize(getattr(record, field), key=field)
        return json.dumps(payload, ensure_ascii=True)


_LEVEL_MARKERS = {
    logging.DEBUG: "[.]",
    logging.INFO: "[*]",
    logging.WARNING: "[!]",
    logging.ERROR: "[x]",
    logging.CRITICAL: "[x]",
}

# Logger name -> operator-facing module label. Matched by dotted prefix, most
# specific first (the tuple is pre-sorted by length below).
_MODULE_LABELS = {
    "sms_tool.registration_progress": "注册",
    "sms_tool.registration_handlers": "注册",
    "sms_tool.registration_retry_guard": "注册",
    "sms_tool.registration_drivers": "浏览器注册",
    "sms_tool.registration": "注册",
    "sms_tool.batch_runner": "批量注册",
    "sms_tool.commands.one_click": "一键接码",
    "sms_tool.commands.registration": "注册",
    "sms_tool.commands.accounts": "账号",
    "sms_tool.accounts.account_liveness": "账号测活",
    "sms_tool.accounts.account_health_queue": "账号测活",
    "sms_tool.accounts.account_scan": "账号测活",
    "sms_tool.accounts.account_recovery": "账号测活",
    "sms_tool.accounts.account_promotion": "优惠检测",
    "sms_tool.codex_oauth": "Codex 授权",
    "sms_tool.mailbox": "邮箱",
    "sms_tool.providers": "邮箱",
    "sms_tool.phone": "接码",
    "proxy_bridge": "代理桥接",
    "sms_tool.proxy": "代理",
    "sms_tool.http_client": "HTTP",
    # Routed by logging.captureWarnings(): Python ``warnings`` output becomes
    # one formatted log line instead of raw multi-line stderr noise (which the
    # WPF panel renders without any formatter).
    "py.warnings": "告警",
}
_MODULE_LABELS_SORTED = tuple(
    sorted(_MODULE_LABELS.items(), key=lambda item: -len(item[0]))
)

_STAGE_LABELS = {
    "created": "已创建",
    "mailbox_ready": "邮箱就绪",
    "sentinel": "Sentinel 令牌",
    "identity_ready": "身份信息就绪",
    "auth_flow": "授权流程",
    "user_register": "提交注册",
    "email_otp_send": "发送邮箱验证码",
    "email_otp_resend": "重发邮箱验证码",
    "email_otp_wait": "等待邮箱验证码",
    "email_otp_validate": "校验邮箱验证码",
    "create_account": "创建账号",
    "auth_session": "建立会话",
    "codex_oauth": "Codex 授权",
    "access_token_probe": "访问令牌探测",
    "access_token_stability_wait": "令牌稳定性等待",
    "totp_enroll": "绑定 TOTP",
    "finalize": "收尾",
    "completed": "完成",
    "failed": "失败",
    "started": "开始",
}
_STAGE_SUFFIX_LABELS = (("_retry", "重试"), ("_reload", "重新加载"))
_STATUS_LABELS = {
    "running": "进行中",
    "success": "成功",
    "failed": "失败",
    "cancelled": "已取消",
    "retry_pending": "待重试",
}
_STAGE_LINE = re.compile(r"^Registration stage=(\S+) status=(\S+)")
# warnings -> py.warnings message shape: ``<file>:<lineno>: <Category>: <text>
# followed by an optional indented copy of the offending source line. Keep the
# category and the first text line; drop the absolute path and source echo.
_WARNING_LINE = re.compile(r"^[^\n]*\.py:\d+:\s*(\w+):\s*(.*)$", re.S)


def module_label(logger_name: str) -> str:
    """Operator-facing Chinese label for a dotted logger name."""
    name = str(logger_name or "")
    for prefix, label in _MODULE_LABELS_SORTED:
        if name == prefix or name.startswith(prefix + "."):
            return label
    return name.rpartition(".")[2] or name or "-"


def stage_display(stage: str, status: str) -> str:
    """Render one stage record as ``阶段 · 中文名 (code) — 状态``."""
    code = str(stage or "unknown")
    label = _STAGE_LABELS.get(code)
    if label is None:
        for suffix, suffix_label in _STAGE_SUFFIX_LABELS:
            base = code[: -len(suffix)] if code.endswith(suffix) else ""
            if base and base in _STAGE_LABELS:
                label = f"{_STAGE_LABELS[base]}（{suffix_label}）"
                break
    status_label = _STATUS_LABELS.get(str(status or ""), str(status or "进行中"))
    if label is None:
        return f"阶段 · {code} — {status_label}"
    return f"阶段 · {label} ({code}) — {status_label}"


class HumanLogFormatter(logging.Formatter):
    """Operator log line: ``HH:MM:SS [*] [模块] 消息``, stage-aware, sanitized."""

    def format(self, record: logging.LogRecord) -> str:
        from .sanitizer import sanitize_log_text

        message = sanitize_log_text(super().format(record))
        stage_match = _STAGE_LINE.match(message)
        if stage_match:
            message = stage_display(stage_match.group(1), stage_match.group(2))
        elif record.name == "py.warnings":
            warning_match = _WARNING_LINE.match(message)
            if warning_match:
                text = warning_match.group(2).splitlines()[0].strip()
                message = f"{warning_match.group(1)}: {text}" if text else warning_match.group(1)
        marker = _LEVEL_MARKERS.get(record.levelno, "[*]")
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        # ``account_ref`` is the same hash the JSONL envelope and the desktop
        # progress line carry, so an operator line can be grepped against both
        # machine channels. It is appended rather than prefixed to keep the
        # ``HH:MM:SS [*] [模块] 阶段 · ...`` shape that the stage regex and the
        # existing display tests rely on.
        account = sanitize_log_text(str(getattr(record, "account_ref", "") or "")).strip()
        suffix = f" · account_ref={account}" if account else ""
        return f"{stamp} {marker} [{module_label(record.name)}] {message}{suffix}"


_ROLLOVER_RETRY_EVERY = 200


class ResilientRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that never lets a failed rotation drop a record.

    ``RotatingFileHandler.emit`` lets a rotation ``OSError`` escape into
    ``logging.Handler.handleError``, which writes to stderr and *discards* the
    record. On Windows the rename is refused while another process holds the
    file open, and because the size stays above ``maxBytes`` every later record
    fails the same way -- so the channel stops permanently, silently.

    That is exactly what happened to ``runtime/logs/sms_tool.jsonl``: it reached
    5,242,694 of its 5,242,880-byte cap at 2026-09-13 10:50 and never received
    another record, while the human ``sms_tool.log`` (still under its cap) kept
    writing. Nothing surfaced the stop, because the seven concurrent Python
    processes that had the file open also meant ``sms_tool.1.jsonl`` was never
    created -- there was no rotated file to notice either.

    Here a failed rotation reopens the stream and appends anyway, which is
    strictly better than losing the record, and the first failure is announced
    through the logging system so it lands in the *other* channel too. Rotation
    is retried periodically so it self-heals once the file is released; between
    retries the records keep flowing.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rollover_failures = 0
        self._records_since_failure = 0

    def shouldRollover(self, record) -> int:
        if self._rollover_failures:
            # Backing off matters: a rollover attempt closes and reopens the
            # stream, and retrying that per record while the file is still held
            # is pure overhead. Retry occasionally so rotation recovers on its
            # own, and append in the meantime so nothing is lost.
            self._records_since_failure += 1
            if self._records_since_failure < _ROLLOVER_RETRY_EVERY:
                return 0
            self._records_since_failure = 0
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError as exc:
            first = not self._rollover_failures
            self._rollover_failures += 1
            # No explicit reopen is needed: ``RotatingFileHandler.doRollover``
            # closes the stream before renaming, and ``FileHandler.emit``
            # reopens a ``None`` stream before writing (that is how ``delay``
            # works), so the record still lands. Deleting a defensive reopen
            # here is an *equivalent* mutation -- verified, not assumed.
            if first:
                self._announce_rollover_failure(exc)
        else:
            self._rollover_failures = 0

    def _announce_rollover_failure(self, exc: OSError) -> None:
        # Re-entering this handler is safe: the failure counter is already
        # non-zero, so ``shouldRollover`` answers 0 for the warning record and it
        # is written normally. Announcing through ``logging`` (rather than
        # ``print(..., file=sys.stderr)``, which the desktop host does not
        # persist) is what puts the degradation into the operator log.
        logging.getLogger(__name__).warning(
            "log rotation failed for %s (%s); appending without rotation",
            self.baseFilename,
            exc,
        )


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

    # Route the warnings module through logging so library warnings render as
    # normalized ``[!] [告警] ...`` lines instead of raw ``path:line:`` stderr
    # noise leaking into the WPF output panel.
    logging.captureWarnings(True)

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(_ROOT_FORMAT)

    try:
        path = Path(log_path) if log_path else _default_log_path()
    except Exception as exc:  # pragma: no cover - last-resort only
        print(f"[logging] could not resolve the log path: {exc}")
        path = None

    if path is not None:
        # The two channels are installed independently: a failure on one must
        # not take the other down with it. They used to share a single ``try``,
        # so one unopenable file silenced both.
        for target, formatter in (
            (path, HumanLogFormatter("%(message)s")),
            (path.with_suffix(".jsonl"), CorrelatedJsonFormatter("%(message)s")),
        ):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                handler = ResilientRotatingFileHandler(
                    target, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
                )
                handler.setFormatter(formatter)
                root.addHandler(handler)
            except Exception as exc:  # pragma: no cover - last-resort only
                # Never let logging setup crash the application. Broad on
                # purpose: OSError (permissions/full disk) is the expected case,
                # but a broken path resolver must not take the CLI down either.
                print(f"[logging] could not open log file {target}: {exc}")

    if to_console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    _CONFIGURED = True
