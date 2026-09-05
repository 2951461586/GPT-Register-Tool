"""Cross-batch retry circuit breaker for disposable mailbox registrations."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from .paths import runtime_file


_LOCK = threading.Lock()


class RegistrationRetryGuard:
    """Track consecutive retryable failures per mailbox.

    The guard is intentionally separate from account storage: a failed signup
    must not create a misleading account row, while repeated attempts still
    need a durable cooldown across WPF/CLI invocations.
    """

    RETRYABLE_CLASSES = frozenset({"network", "auth_state"})

    def __init__(
        self,
        config: Mapping | None = None,
        *,
        path: Path | None = None,
        threshold: int = 2,
        cooldown_seconds: int = 1800,
    ) -> None:
        self.path = path or runtime_file(config or {}, "registration_retry_guard.json")
        self.threshold = max(1, int(threshold or 2))
        self.cooldown_seconds = max(60, int(cooldown_seconds or 1800))

    @staticmethod
    def _email(email: object) -> str:
        return str(email or "").strip().casefold()

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, value: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
        temp.replace(self.path)

    def check(self, email: object) -> dict[str, object]:
        key = self._email(email)
        if not key:
            return {"deferred": False, "consecutive": 0, "remaining_seconds": 0}
        with _LOCK:
            row = self._read().get(key) or {}
        until = float(row.get("cooldown_until") or 0) if isinstance(row, Mapping) else 0
        remaining = max(0, int(until - time.time()))
        return {
            "deferred": remaining > 0,
            "consecutive": int(row.get("consecutive") or 0) if isinstance(row, Mapping) else 0,
            "remaining_seconds": remaining,
            "failure_class": str(row.get("failure_class") or "") if isinstance(row, Mapping) else "",
        }

    def record(self, email: object, *, failure_class: str = "", error: str = "", success: bool = False) -> None:
        key = self._email(email)
        if not key:
            return
        with _LOCK:
            data = self._read()
            if success:
                data.pop(key, None)
            elif str(failure_class or "").strip().lower() in self.RETRYABLE_CLASSES:
                previous = data.get(key) if isinstance(data.get(key), Mapping) else {}
                same_class = str(previous.get("failure_class") or "") == str(failure_class or "")
                consecutive = int(previous.get("consecutive") or 0) + 1 if same_class else 1
                data[key] = {
                    "consecutive": consecutive,
                    "failure_class": str(failure_class or "")[:40],
                    "last_error": str(error or "")[:160],
                    "last_attempt_at": int(time.time()),
                    "cooldown_until": int(time.time() + self.cooldown_seconds)
                    if consecutive >= self.threshold else 0,
                }
            else:
                data.pop(key, None)
            self._write(data)


__all__ = ["RegistrationRetryGuard"]
