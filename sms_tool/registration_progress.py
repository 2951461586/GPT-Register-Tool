from __future__ import annotations

import contextvars
import functools
import json
import threading
import time
import uuid
from typing import Any, Callable

from .config import CFG
from .paths import runtime_file
from .registration_concurrency import (
    RegistrationStageLease,
    acquire_registration_stage,
    registration_stage_metrics,
)
from .sanitizer import sanitize as _sanitize, sanitize_text as _sanitize_text


_current: contextvars.ContextVar["RegistrationProgress | None"] = contextvars.ContextVar(
    "registration_progress",
    default=None,
)
_write_lock = threading.Lock()


class RegistrationProgress:
    def __init__(self, email: str = ""):
        self.run_id = uuid.uuid4().hex
        self.email = str(email or "")
        self.started_at = int(time.time())
        self.events: list[dict[str, Any]] = []
        self.sequence = 0
        self.last_stage = "started"
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
        previous, self._lease = self._lease, acquire_registration_stage(name)
        if previous is not None:
            previous.release()
        return self._lease.waited_ms if self._lease is not None else 0.0

    def release_stage_gate(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    def stage(self, name: str, status: str = "running", detail: str = "") -> None:
        next_stage = str(name or "unknown")
        now_mono = time.monotonic()
        duration_ms = int(max(0.0, now_mono - self._stage_started_monotonic) * 1000)
        self.last_stage = next_stage
        self._stage_started_monotonic = now_mono
        self.sequence += 1
        event = {
            "stage": self.last_stage,
            "status": str(status or "running"),
            "at": int(time.time()),
            "sequence": self.sequence,
            "duration_ms": duration_ms,
        }
        if detail:
            event["detail"] = _sanitize_text(detail)[:240]
        self.events.append(event)
        try:
            from .desktop_ipc import emit_event

            emit_event({
                "domain": "registration",
                "run_id": self.run_id,
                "account_ref": self.email,
                **event,
            })
        except (OSError, ValueError, TypeError, RuntimeError):
            # A desktop observer must never affect registration behavior.
            return

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "last_stage": self.last_stage,
            "started_at": self.started_at,
            "events": list(self.events),
        }

    def persist(self, result: dict[str, Any] | None, error: str = "") -> None:
        success = bool((result or {}).get("success"))
        final_error = _sanitize_text(error or (result or {}).get("error") or "")[:300]
        terminal_stage = "completed" if success else "failed"
        terminal_status = "success" if success else "failed"
        last_event = self.events[-1] if self.events else {}
        if last_event.get("stage") != terminal_stage or last_event.get("status") != terminal_status:
            self.stage(terminal_stage, terminal_status, final_error)
        proxy_audit = (result or {}).get("proxy_audit")
        pool_index = -1
        if isinstance(proxy_audit, dict):
            try:
                pool_index = int(proxy_audit.get("pool_index"))
            except (TypeError, ValueError):
                pool_index = -1
        row = _sanitize({
            "run_id": self.run_id,
            "email": self.email or str((result or {}).get("email") or ""),
            "batch_id": str((result or {}).get("batch_id") or ""),
            "attempt": int((result or {}).get("registration_attempts") or 0),
            "success": success,
            "error": final_error,
            "failure_class": str((result or {}).get("failure_class") or "")[:80],
            "retryable": bool((result or {}).get("retryable")),
            "registration_state": str((result or {}).get("registration_state") or "")[:40],
            "registration_driver": str((result or {}).get("registration_driver") or "")[:32],
            "proxy_pool_index": pool_index,
            "started_at": self.started_at,
            "finished_at": int(time.time()),
            "last_stage": self.last_stage,
            "events": self.events,
        })
        path = runtime_file(CFG, "registration_progress.jsonl")
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")


def registration_stage(name: str, status: str = "running", detail: str = "") -> None:
    progress = _current.get()
    if progress is None:
        return
    waited_ms = progress.enter_stage_gate(name)
    wait_detail = f"stage_queue_wait_ms={waited_ms:.1f}" if waited_ms >= 1 else ""
    progress.stage(name, status, detail or wait_detail)


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
            lines = target.read_text(encoding="utf-8").splitlines()[-2000:]
            records = [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError, TypeError):
            records = []
    rows = [row for row in records if isinstance(row, dict)]
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
        progress = RegistrationProgress(_mailbox_email(kwargs))
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

    return wrapper
