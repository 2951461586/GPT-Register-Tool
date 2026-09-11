from __future__ import annotations

import contextvars
import functools
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable

from .config import CFG
from .paths import runtime_file
from .registration_concurrency import (
    RegistrationStageLease,
    acquire_registration_stage,
    registration_stage_group,
    registration_stage_metrics,
)
from .cross_process_gate import cross_process_write_lock
from .sanitizer import sanitize as _sanitize, sanitize_text as _sanitize_text
from .sanitizer import account_reference
from .registration_policy import registration_retry_decision
from .telemetry import correlation_fields, current_run_id


_current: contextvars.ContextVar["RegistrationProgress | None"] = contextvars.ContextVar(
    "registration_progress",
    default=None,
)
_write_lock = threading.Lock()
_PROGRESS_MAX_BYTES = 5 * 1024 * 1024
_PROGRESS_BACKUPS = 5
_PROGRESS_SCHEMA_VERSION = 2


class RegistrationProgress:
    def __init__(
        self,
        email: str = "",
        *,
        batch_id: str = "",
        attempt: int = 0,
        driver: str = "",
        run_id: str = "",
    ):
        self.run_id = str(run_id or uuid.uuid4().hex)
        self.email = str(email or "")
        self.driver = driver
        self.batch_id = str(batch_id or "")
        self.attempt = max(0, int(attempt or 0))
        self.started_at = int(time.time())
        self.events: list[dict[str, Any]] = []
        self.sequence = 0
        self.last_stage = ""
        self._stage_started_monotonic = time.monotonic()
        # The concurrency gate is owned by this progress object, never by a
        # ContextVar: pool workers are reused, so context-local ownership leaks
        # into the next account on the same worker.
        self._lease: RegistrationStageLease | None = None
        self.stage("started")

    def enter_stage_gate(self, name: str) -> float:
        """Take the concurrency gate for ``name``, releasing the previous one.

        Returns the queue wait in milliseconds so callers can report it.
        """
        next_group = registration_stage_group(name)
        if self._lease is not None and self._lease.group == next_group:
            return 0.0
        previous, self._lease = self._lease, acquire_registration_stage(name)
        if previous is not None:
            previous.release()
        return self._lease.waited_ms if self._lease is not None else 0.0

    def release_stage_gate(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    def stage(
        self,
        name: str,
        status: str = "running",
        detail: str = "",
        failure_class: str = "",
    ) -> None:
        next_stage = str(name or "unknown")
        failure_class = _sanitize_text(failure_class)[:80].strip().lower()
        now_mono = time.monotonic()
        previous_stage = self.last_stage
        previous_duration_ms = int(
            max(0.0, now_mono - self._stage_started_monotonic) * 1000
        )
        if self.events:
            self.events[-1]["duration_ms"] = previous_duration_ms
            self.events[-1]["finished_at"] = int(time.time())
        self.last_stage = next_stage
        self._stage_started_monotonic = now_mono
        self.sequence += 1
        event = {
            **correlation_fields(),
            "progress_schema_version": _PROGRESS_SCHEMA_VERSION,
            "run_id": self.run_id,
            "stage": self.last_stage,
            "status": str(status or "running"),
            "at": int(time.time()),
            "sequence": self.sequence,
            # Failure class rides on the event, not only on the persisted row:
            # the row does not exist until the run finishes, so without this the
            # operator log cannot separate a 300s OTP timeout from an auth_state
            # failure while the batch is still in flight (2026-09-11: the whole
            # 56%-failure triage had to wait for terminal rows to be written).
            "failure_class": failure_class,
            # Duration belongs to the stage named by this event. It is filled
            # when the next transition occurs (or when persist finalizes the
            # terminal stage), never attributed to the stage being entered.
            "duration_ms": 0,
        }
        if previous_stage:
            event["previous_stage"] = previous_stage
            event["previous_stage_duration_ms"] = previous_duration_ms
        if detail:
            event["detail"] = _sanitize_text(detail)[:240]
        self.events.append(event)
        # run_id travels in the event dict, the desktop IPC event and the JSONL
        # envelope; the operator log message stays metadata-free on purpose.
        logging.getLogger(__name__).info(
            "Registration stage=%s status=%s",
            next_stage,
            event["status"],
            extra={
                "event": "registration_stage",
                "stage": next_stage,
                "status": event["status"],
                "previous_stage": previous_stage,
                "previous_stage_duration_ms": previous_duration_ms,
                "failure_class": failure_class,
            },
        )
        try:
            from .desktop_ipc import emit_event

            emit_event({
                "domain": "registration",
                "run_id": self.run_id,
                "account_ref": account_reference(self.email),
                **event,
            })
        except (OSError, ValueError, TypeError, RuntimeError):
            # A desktop observer must never affect registration behavior.
            return

    def snapshot(self) -> dict[str, Any]:
        return {
            **correlation_fields(),
            "run_id": self.run_id,
            "last_stage": self.last_stage,
            "batch_id": self.batch_id,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "events": list(self.events),
        }

    def persist(self, result: dict[str, Any] | None, error: str = "") -> None:
        success = bool((result or {}).get("success"))
        cancelled = str((result or {}).get("registration_state") or "").lower() == "cancelled" or str(
            (result or {}).get("error") or ""
        ).lower() == "registration_cancelled"
        final_error = _sanitize_text(error or (result or {}).get("error") or "")[:300]
        failure_class = str(
            (result or {}).get("failure_class")
            or ("" if success else registration_retry_decision(final_error).failure_class)
        )[:80]
        terminal_stage = "completed" if success else "cancelled" if cancelled else "failed"
        terminal_status = "success" if success else "cancelled" if cancelled else "failed"
        last_event = self.events[-1] if self.events else {}
        if last_event.get("stage") != terminal_stage or last_event.get("status") != terminal_status:
            self.stage(terminal_stage, terminal_status, final_error, failure_class=failure_class)
        elif not last_event.get("failure_class"):
            # The caller already emitted the terminal stage -- the registration
            # loop stages "failed" itself before persisting, which is the common
            # path.  Backfill the class so every terminal event is aggregatable,
            # not just the ones this method emits.
            last_event["failure_class"] = _sanitize_text(failure_class)[:80].strip().lower()
        if self.events:
            now_mono = time.monotonic()
            self.events[-1]["duration_ms"] = int(
                max(0.0, now_mono - self._stage_started_monotonic) * 1000
            )
            self.events[-1]["finished_at"] = int(time.time())
        proxy_audit = (result or {}).get("proxy_audit")
        pool_index = -1
        if isinstance(proxy_audit, dict):
            try:
                pool_index = int(proxy_audit.get("pool_index"))
            except (TypeError, ValueError):
                pool_index = -1
        row = _sanitize({
            **correlation_fields(),
            "progress_schema_version": _PROGRESS_SCHEMA_VERSION,
            "run_id": self.run_id,
            "account_ref": account_reference(
                self.email or str((result or {}).get("email") or "")
            ),
            "batch_id": str((result or {}).get("batch_id") or self.batch_id),
            "attempt": int((result or {}).get("registration_attempts") or self.attempt),
            "success": success,
            "error": final_error,
            "failure_class": failure_class,
            "retryable": bool((result or {}).get("retryable", not success and registration_retry_decision(final_error).retryable)),
            "registration_state": str((result or {}).get("registration_state") or "")[:40],
            "registration_driver": str((result or {}).get("registration_driver") or self.driver or "unknown")[:32],
            "proxy_pool_index": pool_index,
            "started_at": self.started_at,
            "finished_at": int(time.time()),
            "last_stage": self.last_stage,
            "events": self.events,
        })
        # Failure rows carry the token-free browser diagnostics (URL shape +
        # DOM landmark counts) so post-OTP page states are debuggable from the
        # progress log alone instead of only an error code.
        diagnostics = (result or {}).get("browser_diagnostics")
        if not success and isinstance(diagnostics, dict) and diagnostics:
            row["browser_diagnostics"] = diagnostics
        path = runtime_file(CFG, "registration_progress.jsonl")
        with _write_lock:
            with cross_process_write_lock(
                path.with_name(f"{path.name}.lock"),
                timeout=10,
            ):
                _append_progress_row(path, row)


def _append_progress_row(
    path,
    row: dict[str, Any],
    *,
    max_bytes: int = _PROGRESS_MAX_BYTES,
    backups: int = _PROGRESS_BACKUPS,
) -> None:
    """Append one JSONL row and rotate bounded progress history by size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n"
    encoded_size = len(line.encode("utf-8"))
    if (
        max_bytes > 0
        and path.is_file()
        and path.stat().st_size + encoded_size > max_bytes
    ):
        for index in range(max(0, backups), 0, -1):
            source = path.with_name(
                path.name if index == 1 else f"{path.name}.{index - 1}"
            )
            target = path.with_name(f"{path.name}.{index}")
            if not source.exists():
                continue
            if target.exists():
                target.unlink()
            source.replace(target)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def registration_stage(
    name: str,
    status: str = "running",
    detail: str = "",
    failure_class: str = "",
) -> None:
    progress = _current.get()
    if progress is None:
        return
    waited_ms = progress.enter_stage_gate(name)
    wait_detail = f"stage_queue_wait_ms={waited_ms:.1f}" if waited_ms >= 1 else ""
    progress.stage(name, status, detail or wait_detail, failure_class=failure_class)


def admit_registration_stage(name: str) -> float:
    """Acquire a stage gate before allocating the resource it protects."""
    progress = _current.get()
    if progress is None:
        return 0.0
    return progress.enter_stage_gate(name)


def registration_quality_metrics(records: list[dict[str, Any]] | None = None, *, path=None) -> dict[str, Any]:
    """Aggregate token-free registration quality metrics from progress rows.

    The helper is intentionally pure when ``records`` is supplied, making it
    suitable for tests and dashboards.  When omitted it reads the bounded tail
    of ``registration_progress.jsonl`` from the runtime directory.
    """
    if records is None:
        target = path or runtime_file(CFG, "registration_progress.jsonl")
        records = []
        try:
            candidates = [
                target.with_name(f"{target.name}.{index}")
                for index in range(_PROGRESS_BACKUPS, 0, -1)
            ] + [target]
            lines: list[str] = []
            for candidate in candidates:
                if candidate.is_file():
                    lines.extend(candidate.read_text(encoding="utf-8").splitlines())
                    lines = lines[-2000:]
            records = [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError, TypeError):
            records = []
    rows = [row for row in records if isinstance(row, dict) and row.get("source") != "test"]
    durations: list[float] = []
    retry_count = 0
    failure_count = 0
    stage_samples: dict[str, list[float]] = {}
    for row in rows:
        if not row.get("success"):
            failure_count += 1
        for event in row.get("events") or []:
            if not isinstance(event, dict):
                continue
            stage = str(event.get("stage") or "")
            duration = float(event.get("duration_ms") or 0)
            if duration > 0:
                stage_samples.setdefault(stage, []).append(duration)
                if stage == "auth_session":
                    durations.append(duration)
            if "retry" in stage:
                retry_count += 1
    def stats(values: list[float]) -> dict[str, float]:
        if not values:
            return {"count": 0, "average_ms": 0.0, "p95_ms": 0.0}
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round(len(ordered) * 0.95)) - 1))
        return {"count": len(values), "average_ms": round(sum(values) / len(values), 1), "p95_ms": round(ordered[index], 1)}
    return {
        "runs": len(rows),
        "success_rate": round((sum(1 for row in rows if row.get("success")) / len(rows)), 4) if rows else 0.0,
        "failure_count": failure_count,
        "retry_count": retry_count,
        "auth_session": stats(durations),
        "stages": {name: stats(values) for name, values in stage_samples.items()},
    }


def _mailbox_email(kwargs: dict[str, Any]) -> str:
    mailbox = kwargs.get("mailbox")
    return str(getattr(mailbox, "email", "") or "")


def track_registration(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from .config import resolve_runtime_config
        from .registration_drivers.base import normalize_registration_driver

        try:
            config = resolve_runtime_config(kwargs.get("runtime_config"), workflow="registration")
            driver = normalize_registration_driver(kwargs.get("registration_driver"), config.data)
        except Exception:
            # An invalid driver/config must still be recorded: let func raise
            # inside the tracked block so the failure is persisted as a row
            # instead of vanishing before any progress object exists.
            driver = str(kwargs.get("registration_driver") or "")
        run_id = uuid.uuid4().hex
        correlation_token = current_run_id.set(run_id)
        progress = RegistrationProgress(
            _mailbox_email(kwargs),
            batch_id=str(kwargs.get("batch_id") or ""),
            attempt=int(kwargs.get("registration_attempt") or 0),
            driver=driver,
            run_id=run_id,
        )
        token = _current.set(progress)
        result: dict[str, Any] | None = None
        error = ""
        try:
            result = func(*args, **kwargs)
            return result
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            try:
                progress.persist(result, error)
                if isinstance(result, dict):
                    result["registration_progress"] = progress.snapshot()
                    result["registration_stage_metrics"] = registration_stage_metrics()
            finally:
                progress.release_stage_gate()
                _current.reset(token)
                current_run_id.reset(correlation_token)

    return wrapper
