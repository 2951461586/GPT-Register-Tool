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
        self.last_stage = "started"
        self.stage("started")

    def stage(self, name: str, status: str = "running", detail: str = "") -> None:
        self.last_stage = str(name or "unknown")
        event = {
            "stage": self.last_stage,
            "status": str(status or "running"),
            "at": int(time.time()),
        }
        if detail:
            event["detail"] = str(detail)[:240]
        self.events.append(event)

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "last_stage": self.last_stage,
            "started_at": self.started_at,
            "events": list(self.events),
        }

    def persist(self, result: dict[str, Any] | None, error: str = "") -> None:
        success = bool((result or {}).get("success"))
        final_error = str(error or (result or {}).get("error") or "")[:300]
        self.stage("completed" if success else "failed", "success" if success else "failed", final_error)
        row = {
            "run_id": self.run_id,
            "email": self.email or str((result or {}).get("email") or ""),
            "success": success,
            "error": final_error,
            "started_at": self.started_at,
            "finished_at": int(time.time()),
            "last_stage": self.last_stage,
            "events": self.events,
        }
        path = runtime_file(CFG, "registration_progress.jsonl")
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")


def registration_stage(name: str, status: str = "running", detail: str = "") -> None:
    progress = _current.get()
    if progress is not None:
        progress.stage(name, status, detail)


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
            finally:
                _current.reset(token)

    return wrapper
