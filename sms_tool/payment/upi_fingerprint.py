"""Sticky-per-account browser fingerprint, with rotation and cooldown.

Why this module exists
----------------------
``session._upi_fingerprint`` picked a template with ``secrets.randbelow`` on every
call, and ``pipeline._resolve_upi_runtime`` called it once per run.  So a retry
presented a *different* user agent, ``sec-ch-ua`` and timezone -- while the
device id and Sentinel pair stayed the same.  A device whose UA changes under a
fixed device id is one of the cheapest contradictions to detect, and it also
threw away the one property a fingerprint is supposed to have: consistency.

The reference implementation keeps one identity per account
(``fingerprint_store.py:320 _ensure_account_fingerprint``), rotates it only on
retry (``UPI_ROTATE_FINGERPRINT_ON_RETRY``), and cools a fingerprint down after a
``blocked`` verdict (``:176 record_fingerprint_outcome`` / ``:235
fingerprint_in_cooldown``, 30 minutes).

Policy
------
* **First run for an account**: allocate a index deterministically from the
  account seed (so a crash before the ledger write lands on the same template).
* **Retry** (``rotate=True``): advance to the next template and persist it.
* **Cooldown**: once ``blocked`` is recorded, ``rotate=True`` hops past it; the
  caller emits a line so the operator can see why the UA changed.
* **No account key**: random, i.e. exactly the old behaviour, and no disk I/O.

The store, its lock and its atomic write belong to :mod:`identity_store`; this
module only decides *which* index and *when*.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any

from .upi_identity_store import account_entry, ledger_transaction, read_ledger

#: How long a ``blocked`` fingerprint stays "cooling" (seconds).  Same 30
#: minutes the reference uses; long enough to stop hammering one template
#: inside a batch, short enough that a recovered account is not stuck.
FINGERPRINT_COOLDOWN_SECONDS = 1800


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any, default: int = 0) -> int:
    """Parse an int defensively: a hand-edited ledger must not raise here."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fingerprint_entry(ledger: dict[str, Any], key: str) -> dict[str, Any]:
    entry = account_entry(ledger, key)
    record = entry.get("fingerprint")
    if not isinstance(record, dict):
        record = {}
        entry["fingerprint"] = record
    return record


def _seed_index(account_key: str, count: int) -> int:
    """Deterministic starting index for an account with no ledger entry yet."""
    digest = hashlib.sha256(_text(account_key).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % max(1, count)


def cooldown_status(account_key: str, cfg: Mapping[str, Any] | None = None) -> tuple[bool, float]:
    """Return ``(in_cooldown, seconds_remaining)`` for one account's fingerprint."""
    key = _text(account_key)
    if not key:
        return False, 0.0
    try:
        ledger = read_ledger(cfg=cfg)
    except Exception:
        return False, 0.0
    accounts = ledger.get("accounts")
    entry = accounts.get(key) if isinstance(accounts, Mapping) else None
    record = entry.get("fingerprint") if isinstance(entry, Mapping) else None
    if not isinstance(record, Mapping):
        return False, 0.0
    try:
        until = float(record.get("cooldown_until") or 0.0)
    except (TypeError, ValueError):
        return False, 0.0
    remaining = until - time.time()
    return (remaining > 0, max(0.0, remaining))


def fingerprint_index(
    account_key: str,
    count: int,
    cfg: Mapping[str, Any] | None = None,
    *,
    rotate: bool = False,
    names: Any = None,
) -> int | None:
    """Return the template index this account should use, or ``None``.

    ``None`` means "the caller has no account identity" -- it must fall back to
    its own random choice, which is the documented pre-existing behaviour.
    ``names`` is an optional index-aligned label list; when given, the chosen
    label is stored so an operator reading the ledger sees ``chrome-mac`` rather
    than ``1``.
    """
    key = _text(account_key)
    total = _int(count)
    if not key or total <= 0:
        return None
    try:
        with ledger_transaction(cfg=cfg) as ledger:
            record = _fingerprint_entry(ledger, key)
            try:
                current = record.get("index")
                index = int(current) if current is not None else None
            except (TypeError, ValueError):
                index = None
            if index is None:
                index = _seed_index(key, total)
            elif rotate:
                index = (index + 1) % total
            index %= total
            record["index"] = index
            labels = names if isinstance(names, (list, tuple)) else ()
            if 0 <= index < len(labels):
                record["name"] = str(labels[index])
            record.setdefault("assigned_at", time.time())
            return index
    except Exception:
        # A locked/unwritable ledger must not stop a link: fall back to random.
        return None


def record_outcome(
    account_key: str,
    *,
    success: bool,
    blocked: bool = False,
    reason: str = "",
    cfg: Mapping[str, Any] | None = None,
    cooldown_seconds: int | None = None,
) -> dict[str, Any] | None:
    """Record one attempt's verdict on this account's fingerprint.

    Best-effort telemetry: returns the updated record, or ``None`` when nothing
    could be persisted.  Never raises -- a failed write must not change the
    extraction result.
    """
    key = _text(account_key)
    if not key:
        return None
    cooldown = FINGERPRINT_COOLDOWN_SECONDS if cooldown_seconds is None else max(0, _int(cooldown_seconds))
    now = time.time()
    try:
        with ledger_transaction(cfg=cfg) as ledger:
            record = _fingerprint_entry(ledger, key)
            record["usage"] = _int(record.get("usage")) + 1
            if success:
                record["success"] = _int(record.get("success")) + 1
                record["last_status"] = "success"
                record["cooldown_until"] = 0.0
            else:
                record["failed"] = _int(record.get("failed")) + 1
                if blocked:
                    record["blocked"] = _int(record.get("blocked")) + 1
                    record["cooldown_until"] = now + cooldown
                record["last_status"] = "blocked" if blocked else "failed"
            record["last_status_ts"] = now
            record["last_reason"] = _text(reason)[:200]
            return dict(record)
    except Exception:
        return None


def reset_account(account_key: str, cfg: Mapping[str, Any] | None = None) -> bool:
    """Forget this account's fingerprint assignment (keeps the billing one)."""
    key = _text(account_key)
    if not key:
        return False
    try:
        with ledger_transaction(cfg=cfg) as ledger:
            accounts = ledger.get("accounts")
            entry = accounts.get(key) if isinstance(accounts, Mapping) else None
            if not isinstance(entry, dict) or "fingerprint" not in entry:
                return False
            entry.pop("fingerprint", None)
            return True
    except Exception:
        return False


__all__ = [
    "FINGERPRINT_COOLDOWN_SECONDS",
    "cooldown_status",
    "fingerprint_index",
    "record_outcome",
    "reset_account",
]
