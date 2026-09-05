"""Small process-shared health tracker for registration proxy candidates."""

from __future__ import annotations

import json
import hashlib
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .paths import runtime_file


_FILE_LOCK = threading.Lock()


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
        parsed = urlsplit(str(proxy or ""))
        base = cls._endpoint_key(proxy)
        # Rotating providers expose many sticky sessions through one endpoint.
        # Keep per-session health separate while never persisting credentials.
        username = str(parsed.username or "").strip()
        if username:
            sid_hash = hashlib.sha256(username.encode("utf-8")).hexdigest()[:10]
            return f"{base}#sid-{sid_hash}"
        return base

    def _read(self) -> dict[str, dict[str, float]]:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, data: dict[str, dict[str, float]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(self._path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
        temp.replace(self._path)

    def record(self, proxy: str, *, ok: bool, error: str = "") -> None:
        key = self.key(proxy)
        if key == ":0":
            return
        with _FILE_LOCK:
            data = self._read()
            keys = [key]
            endpoint = self._endpoint_key(proxy)
            if endpoint not in keys:
                keys.append(endpoint)
            for item_key in keys:
                row = data.setdefault(item_key, {"success": 0, "failure": 0, "cooldown_until": 0, "last_error": ""})
                row["success" if ok else "failure"] = int(row.get("success" if ok else "failure", 0) or 0) + 1
                row["last_error"] = "" if ok else str(error or "")[:120]
                if not ok and int(row.get("failure", 0)) >= 3 and int(row.get("failure", 0)) > int(row.get("success", 0)):
                    row["cooldown_until"] = time.time() + 120
                if ok:
                    row["cooldown_until"] = 0
            self._write(data)

    def rank(self, proxies: list[str]) -> list[str]:
        with self._lock:
            data = self._read()
        now = time.time()
        def score(proxy: str) -> tuple[int, float, int]:
            row = data.get(self.key(proxy)) or data.get(self._endpoint_key(proxy), {})
            cooldown = float(row.get("cooldown_until", 0) or 0)
            if cooldown > now:
                return (1, cooldown, 0)
            success = int(row.get("success", 0) or 0)
            failure = int(row.get("failure", 0) or 0)
            return (0, -(success / max(1, success + failure)), failure)
        return sorted(dict.fromkeys(proxies), key=score)


__all__ = ["ProxyHealthTracker"]
