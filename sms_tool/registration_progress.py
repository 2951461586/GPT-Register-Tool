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
    enter_registration_stage,
    registration_stage_metrics,
    release_registration_stage,
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
        self.stage("started")

    def stage(self, name: str, status: str = "running", detail: str = "") -> None:
        self.last_stage = str(name or "unknown")
        self.sequence += 1
        event = {
            "stage": self.last_stage,
            "status": str(status or "running"),
            "at": int(time.time()),
            "sequence": self.sequence,
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
        row = _sanitize({
            "run_id": self.run_id,
            "email": self.email or str((result or {}).get("email") or ""),
            "success": success,
            "error": final_error,
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
    waited_ms = enter_registration_stage(name)
    wait_detail = f"stage_queue_wait_ms={waited_ms:.1f}" if waited_ms >= 1 else ""
    progress.stage(name, status, detail or wait_detail)


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
                release_registration_stage()
                _current.reset(token)

    return wrapper
