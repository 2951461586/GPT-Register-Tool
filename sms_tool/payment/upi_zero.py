"""P2: zero-mode strategies + account-level 0-元 eligibility memory.

Three findings from the reference implementation, applied to the local stage:

1. **``zero_mode``** (``upi_extract.py:3732``): the campaign promo is only one way
   to make a Checkout come out at zero.  ``trial`` adds a real
   ``subscription_data.trial_period_days``; ``momo`` mirrors the MoMo flow
   (trial + billing profile + campaign).  Local had exactly one strategy
   (``promo_campaign``) and failed hard when it did not apply.
2. **``zero_eligibility_precheck``** (``:9011``): asking whether the account can
   still produce a 0-元 chain *before* creating a Checkout saves the whole
   session when the answer is already known.
3. **``zero_negative_cache``** (``:9041`` + ``fingerprint_store.mark_zero_ineligible``):
   once an account answers "non-zero amount" under ``require_zero``, remember it
   for a day instead of re-learning it on the next run.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .upi_identity_store import account_entry, ledger_transaction

#: How long an account stays marked "cannot produce a zero-amount link".
ZERO_INELIGIBLE_SECONDS = 86400


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _record(ledger: dict[str, Any], key: str) -> dict[str, Any]:
    entry = account_entry(ledger, key)
    record = entry.get("zero_record")
    if not isinstance(record, dict):
        record = {}
        entry["zero_record"] = record
    return record


def mark_zero_ineligible(
    account_key: str,
    *,
    cfg: Mapping[str, Any] | None = None,
    cooldown_seconds: int | None = None,
) -> bool:
    """Remember that this account cannot currently produce a 0-元 link."""
    key = _text(account_key)
    if not key:
        return False
    seconds = ZERO_INELIGIBLE_SECONDS if cooldown_seconds is None else max(0, int(cooldown_seconds))
    now = time.time()
    try:
        with ledger_transaction(cfg=cfg) as ledger:
            record = _record(ledger, key)
            record["ineligible_until"] = now + seconds
            record["last_marked_at"] = now
            return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def zero_ineligible_until(account_key: str, *, cfg: Mapping[str, Any] | None = None) -> float:
    """Unix timestamp until which the account is 0-元 ineligible (``0.0`` if none)."""
    key = _text(account_key)
    if not key:
        return 0.0
    try:
        with ledger_transaction(cfg=cfg) as ledger:
            record = _record(ledger, key)
            try:
                return float(record.get("ineligible_until") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    except (OSError, ValueError, TypeError, KeyError):
        return 0.0


def clear_zero_ineligible(account_key: str, *, cfg: Mapping[str, Any] | None = None) -> bool:
    """Drop the marker (a run produced a zero-amount link after all)."""
    key = _text(account_key)
    if not key:
        return False
    try:
        with ledger_transaction(cfg=cfg) as ledger:
            record = _record(ledger, key)
            if not record.get("ineligible_until"):
                return False
            record["ineligible_until"] = 0.0
            return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def in_cooldown(account_key: str, *, cfg: Mapping[str, Any] | None = None) -> tuple[bool, float]:
    until = zero_ineligible_until(account_key, cfg=cfg)
    remaining = until - time.time()
    return (remaining > 0, max(0.0, remaining))


__all__ = [
    "ZERO_INELIGIBLE_SECONDS",
    "clear_zero_ineligible",
    "in_cooldown",
    "mark_zero_ineligible",
    "zero_ineligible_until",
]
