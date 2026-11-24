"""Small process-shared health tracker for registration proxy candidates."""

from __future__ import annotations

import json
import hashlib
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..cross_process_gate import cross_process_write_lock
from ..kernel.paths import runtime_file
from .entry import strip_session_id


_FILE_LOCK = threading.Lock()

# Additive-smoothing prior for the success rate used by ``ProxyHealthTracker.rank``.
# 1 == Laplace. See the ``rank`` docstring for why the raw ratio is not enough.
_SMOOTHING_PRIOR = 1.0
#: Idle rows older than 24h decay so ancient failures do not permanently poison
#: a recovered residential gateway.
_DECAY_WINDOW_SECONDS = 86_400.0


class ProxyHealthTracker:
    """Persist bounded success/failure counters without proxy credentials."""

    def __init__(self, config: Mapping | None = None, *, path: Path | None = None):
        self._path = path or runtime_file(config or {}, "registration_proxy_health.json")
        self._lock = threading.Lock()

    @staticmethod
    def _endpoint_key(proxy: str) -> str:
        parsed = urlsplit(str(proxy or ""))
        try:
            port = int(parsed.port or 0)
        except (TypeError, ValueError):
            port = 0
        return f"{(parsed.hostname or '').lower()}:{port}"

    @classmethod
    def key(cls, proxy: str) -> str:
        """Exact per-session opaque key (used by ``AsnRegistry`` where distinct
        sticky sessions on one gateway can exit through different ASNs)."""
        parsed = urlsplit(str(proxy or ""))
        base = cls._endpoint_key(proxy)
        username = str(parsed.username or "").strip()
        password = str(parsed.password or "").strip()
        if username or password:
            cred_identity = f"{username}:{password}" if password else username
            sid_hash = hashlib.sha256(cred_identity.encode("utf-8")).hexdigest()[:10]
            return f"{base}#sid-{sid_hash}"
        return base

    @classmethod
    def template_key(cls, proxy: str) -> str:
        """Session-stripped template key for bounded health tracking and ranking."""
        canonical = strip_session_id(str(proxy or "")) or str(proxy or "")
        return cls.key(canonical)

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(self._path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
        temp.replace(self._path)

    def record(self, proxy: str, *, ok: bool, error: str = "") -> None:
        key = self.template_key(proxy)
        if key == ":0":
            return
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with _FILE_LOCK, cross_process_write_lock(lock_path):
            data = self._read()
            keys = [key]
            endpoint = self._endpoint_key(proxy)
            if endpoint not in keys:
                keys.append(endpoint)
            now = time.time()
            for item_key in keys:
                row = data.setdefault(
                    item_key,
                    {"success": 0, "failure": 0, "cooldown_until": 0, "last_error": "", "updated_at": now},
                )
                last_ts = float(row.get("updated_at") or 0)
                if last_ts > 0 and (now - last_ts) >= _DECAY_WINDOW_SECONDS:
                    row["success"] = int(row.get("success", 0) or 0) // 2
                    row["failure"] = int(row.get("failure", 0) or 0) // 2
                row["success" if ok else "failure"] = int(row.get("success" if ok else "failure", 0) or 0) + 1
                row["last_error"] = "" if ok else str(error or "")[:120]
                row["updated_at"] = now
                if (
                    not ok
                    and int(row.get("failure", 0)) >= 3
                    and int(row.get("failure", 0)) > int(row.get("success", 0))
                ):
                    row["cooldown_until"] = now + 120
                if ok:
                    row["cooldown_until"] = 0
            self._write(data)

    def rank(self, proxies: list[str]) -> list[str]:
        """Order candidates worst-last: cooling, then worst success rate, then most failed.

        P2-4: the success rate is additively smoothed (Laplace, prior=1) instead of
        the raw ``success / (success + failure)``. The raw ratio had two defects:

        * a proxy with **one** lucky success scored 1.0 and outranked a proven
          8-of-10 endpoint at 0.8, so a single fluke could pin traffic to an
          untested endpoint;
        * every never-used proxy collapsed onto 0.0, identical to a proxy with
          zero successes and zero *recorded* failures -- so cold start silently
          degenerated to config order and a newly added endpoint could only win
          once an incumbent had already failed.

        Smoothing puts an unproven proxy at a neutral 0.5: behind proven-good
        endpoints, ahead of ones carrying a failure record. It therefore gets its
        turn without random churn, and low-sample endpoints can no longer spike
        to the top. Ties (including all-fresh pools) still fall back to config
        order, because :func:`sorted` is stable.
        """
        with _FILE_LOCK, self._lock:
            data = self._read()
        now = time.time()

        def score(proxy: str) -> tuple[int, float, int]:
            row = (
                data.get(self.template_key(proxy))
                or data.get(self.key(proxy))
                or data.get(self._endpoint_key(proxy), {})
            )
            cooldown = float(row.get("cooldown_until", 0) or 0)
            if cooldown > now:
                return (1, cooldown, 0)
            success = int(row.get("success", 0) or 0)
            failure = int(row.get("failure", 0) or 0)
            last_ts = float(row.get("updated_at") or 0)
            if last_ts > 0 and (now - last_ts) >= _DECAY_WINDOW_SECONDS:
                success //= 2
                failure //= 2
            rate = (success + _SMOOTHING_PRIOR) / (success + failure + 2 * _SMOOTHING_PRIOR)
            return (0, -rate, failure)

        return sorted(dict.fromkeys(proxies), key=score)


__all__ = ["ProxyHealthTracker"]
