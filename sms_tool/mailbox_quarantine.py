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

# Minimum number of distinct quarantined credentials before the breaker is
# allowed to take down the whole batch in per-account mode.  Below this,
# per-credential filtering already prevents wasted OTP work, so blocking every
# account would be pure collateral damage.
_GLOBAL_BLOCK_MIN_ENTRIES = 3


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
    if not isinstance(entries, dict):
        return {}
    now = time.time()
    active: dict[str, dict[str, Any]] = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        try:
            retry_after = float(entry.get("retry_after") or 0)
        except (TypeError, ValueError):
            # A hand-edited or corrupt entry must never raise out of _read:
            # fail closed and keep the quarantine instead.
            retry_after = 0.0
        if retry_after <= 0 or retry_after > now:
            active[key] = entry
    return active


# A ``mailbox_auth_invalid`` entry used to be written with ``retry_after = 0``,
# which ``_read`` treats as "active forever".  One transient icloud HTTP 401
# therefore froze OTP recovery for the *entire* pool until an operator
# acknowledged a repair or the credential itself left the pool -- observed as
# 17 of 25 accounts returning ``mailbox_pool_repair_required``.  Entries now
# expire on their own; a genuinely dead credential is simply re-quarantined on
# the next attempt, which caps the wasted work at one probe per window.
MAILBOX_AUTH_INVALID_COOLDOWN_SECONDS = 1800
TRANSIENT_AUTH_INVALID_COOLDOWN_SECONDS = 300


def record_mailbox_auth_invalid(
    mailbox: Any, *, reason: str = "mailbox_auth_invalid",
    cooldown_seconds: int = MAILBOX_AUTH_INVALID_COOLDOWN_SECONDS,
) -> str:
    """Persist only a fingerprint and public metadata, never URL/token values."""
    return _record_failure(
        mailbox, reason=reason, code="mailbox_auth_invalid",
        cooldown_seconds=cooldown_seconds,
    )


def record_mailbox_endpoint_unavailable(mailbox: Any) -> str:
    """A missing inbox endpoint is transient, not an invalid credential."""
    return _record_failure(
        mailbox, reason="mailbox_endpoint_unavailable",
        code="mailbox_endpoint_unavailable", cooldown_seconds=300,
    )


def _record_failure(
    mailbox: Any, *, reason: str, code: str, cooldown_seconds: int = 0,
) -> str:
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
            "code": code,
            "retry_after": now + cooldown_seconds if cooldown_seconds else 0,
            "first_seen_at": int(previous.get("first_seen_at") or now),
            "last_seen_at": now,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return fingerprint


def raise_if_mailbox_quarantined(mailbox: Any) -> None:
    """Stop repeated reads before using an unchanged quarantined credential."""
    entry = _read().get(mailbox_fingerprint(mailbox))
    if not entry:
        return
    if entry.get("code") == "mailbox_endpoint_unavailable":
        from .mailbox_errors import MailboxEndpointUnavailableError

        raise MailboxEndpointUnavailableError()
    from .providers.mailbox_graph import MailboxAuthInvalidError

    raise MailboxAuthInvalidError(detail="mailbox credential quarantined; repair the mailbox pool")


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


def mailbox_relogin_allowed(email: str | None = None) -> bool:
    """Return whether automatic OTP recovery may run for *email*.

    A quarantined credential always blocks recovery.  ReMail keeps a separate
    dead-account registry; when *email* is given, only that account's entry is
    checked (per-account circuit breaker).  Without *email*, the legacy
    global behaviour is preserved — any un-acknowledged
    ``mailbox_auth_invalid`` entry blocks all relogin.

    In per-account mode the quarantine no longer freezes the whole batch on a
    single entry: per-credential filtering (``raise_if_mailbox_quarantined``)
    already stops the accounts that actually use the dead credential, so a
    pool-wide block is reserved for a systemic failure.
    """
    entries = _read()
    target_email = str(email or "").strip().lower()
    if not target_email:
        if entries:
            return False
    elif len(entries) >= _GLOBAL_BLOCK_MIN_ENTRIES:
        # Enough distinct credentials failed that the pool itself is more
        # likely broken than any one account.
        return False
    elif any(str((item or {}).get("email") or "").strip().lower() == target_email for item in entries.values()):
        # Best effort: the quarantine stores the *mailbox* identity, which only
        # coincides with the account email for some providers.  When it does
        # match, fail this account fast instead of burning a relogin lane.
        return False
    try:
        from .providers.mailbox_remail import _read_dead_remail_registry

        # Test seams may replace quarantine_path with an isolated temporary
        # file; do not let the live runtime registry leak into those tests.
        if quarantine_path() == runtime_file(current_config_data(), "mailbox_auth_quarantine.json"):
            for item in _read_dead_remail_registry():
                reason = str((item or {}).get("reason") or "").strip().lower()
                if reason != "mailbox_auth_invalid":
                    continue
                if target_email:
                    item_email = str((item or {}).get("email") or "").strip().lower()
                    if item_email != target_email:
                        continue
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
