"""Token-free quarantine for mailbox credentials that are no longer usable."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import current_config_data
from .paths import runtime_file

_LOCK = threading.Lock()


def quarantine_path() -> Path:
    return runtime_file(current_config_data(), "mailbox_auth_quarantine.json")


def repair_marker_path() -> Path:
    return runtime_file(current_config_data(), "mailbox_pool_repair.json")


def mark_mailbox_pool_repaired(*, actor: str = "operator") -> None:
    """Record an explicit mailbox-pool repair acknowledgement."""
    path = repair_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"repaired_at": int(time.time()), "actor": str(actor or "operator")[:80]}, ensure_ascii=True),
        encoding="utf-8",
    )


def mailbox_fingerprint(mailbox: Any) -> str:
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    token = str(getattr(mailbox, "token", "") or "").strip()
    return hashlib.sha256(f"{provider}|{email}|{token}".encode("utf-8")).hexdigest()


def _read(path: Path | None = None) -> dict[str, dict[str, Any]]:
    target = path or quarantine_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else {}


def record_mailbox_auth_invalid(mailbox: Any, *, reason: str = "mailbox_auth_invalid") -> str:
    """Persist only a fingerprint and public metadata, never URL/token values."""
    fingerprint = mailbox_fingerprint(mailbox)
    if not fingerprint:
        return ""
    now = int(time.time())
    path = quarantine_path()
    with _LOCK:
        entries = _read(path)
        previous = entries.get(fingerprint) if isinstance(entries.get(fingerprint), dict) else {}
        entries[fingerprint] = {
            "provider": str(getattr(mailbox, "provider", "") or "").strip().lower(),
            "email": str(getattr(mailbox, "email", "") or "").strip().lower(),
            "reason": str(reason or "mailbox_auth_invalid")[:120],
            "first_seen_at": int(previous.get("first_seen_at") or now),
            "last_seen_at": now,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return fingerprint


def is_mailbox_quarantined(mailbox: Any) -> bool:
    return mailbox_fingerprint(mailbox) in _read()


def filter_quarantined_mailboxes(mailboxes: list[Any] | tuple[Any, ...]) -> list[Any]:
    values = list(mailboxes or [])
    entries = _read()
    if not entries:
        return values
    return [mailbox for mailbox in values if mailbox_fingerprint(mailbox) not in entries]


def prune_quarantine_against_pool(mailboxes: list[Any] | tuple[Any, ...]) -> int:
    """Drop quarantine records whose exact credential is no longer in the pool."""
    current = {mailbox_fingerprint(mailbox) for mailbox in list(mailboxes or [])}
    path = quarantine_path()
    with _LOCK:
        entries = _read(path)
        stale = set(entries) - current
        if not stale:
            return 0
        for fingerprint in stale:
            entries.pop(fingerprint, None)
        if entries:
            path.write_text(
                json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif path.exists():
            path.unlink()
        return len(stale)


def mailbox_relogin_allowed() -> bool:
    """Return whether automatic OTP recovery may run.

    A quarantined credential always blocks recovery.  ReMail keeps a separate
    dead-account registry, so mailbox auth failures there must also remain a
    hard stop until the pool is explicitly repaired and acknowledged.
    """
    entries = _read()
    if entries:
        return False
    try:
        from .providers.mailbox_remail import _read_dead_remail_registry

        # Test seams may replace quarantine_path with an isolated temporary
        # file; do not let the live runtime registry leak into those tests.
        if quarantine_path() == runtime_file(current_config_data(), "mailbox_auth_quarantine.json"):
            for item in _read_dead_remail_registry():
                reason = str((item or {}).get("reason") or "").strip().lower()
                if reason == "mailbox_auth_invalid":
                    marker = repair_marker_path()
                    try:
                        repaired_at = int((json.loads(marker.read_text(encoding="utf-8")) or {}).get("repaired_at") or 0)
                    except (OSError, TypeError, ValueError):
                        repaired_at = 0
                    if repaired_at <= int((item or {}).get("last_seen_at") or 0):
                        return False
    except Exception:
        pass
    return True
