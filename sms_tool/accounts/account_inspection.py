"""Lightweight offline-triage account inspection workflow.

Filters the local account pool by decoding each stored access token's JWT
``exp`` claim locally before making any network calls:

1. Permanently deactivated / unrecoverable token-revoked accounts are skipped.
2. Accounts whose JWT ``exp`` is still comfortably in the future (beyond
   ``skew_seconds``) and not already marked invalid are skipped with zero HTTP
   or proxy cost.
3. Only accounts with missing, malformed, expired, expiring-soon, or
   previously-invalidated tokens are handed to the local liveness/recovery
   batch, and optionally followed by a targeted promotion check on the
   recovered subset.
"""

from __future__ import annotations

import base64
import importlib
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .account_health import resolve_account_health_budgets
from .account_recovery import (
    CFG,
    _is_token_revoked_drop,
    _local_quota_accounts,
    is_permanently_deactivated,
)
from .promotion_batch import refresh_promotion_statuses

DEFAULT_INSPECT_SKEW_SECONDS = 300


def _load_native_account_inspection() -> Any | None:
    mode = str(os.environ.get("RUST_ANYTHING_MODE") or "").strip().lower()
    unit_mode = str(os.environ.get("RUST_ANYTHING_UNIT_ACCOUNT_INSPECTION") or "").strip().lower()
    if mode in {"old", "original"} or unit_mode in {"old", "original"}:
        return None
    pkg_dir = Path(__file__).resolve().parents[2] / "rust" / "account-inspection-rs" / "python"
    if pkg_dir.is_dir():
        pkg_str = str(pkg_dir)
        if pkg_str not in sys.path:
            sys.path.insert(0, pkg_str)
    try:
        return importlib.import_module("account_inspection_rs")
    except (ImportError, OSError):
        if mode in {"new", "rust"} or unit_mode in {"new", "rust"}:
            raise
        return None


_NATIVE_ACCOUNT_INSPECTION = _load_native_account_inspection()
_NATIVE_DECODE_JWT_EXP = getattr(_NATIVE_ACCOUNT_INSPECTION, "decode_jwt_exp", None)
_NATIVE_SCALAR_TYPES = (str, type(None), bool, int, float)

_MARKED_INVALID_MARKERS = frozenset(
    {
        "at_invalid",
        "token_invalid",
        "token_expired",
        "401失效",
        "at失效",
    }
)


def _strip_bearer(token: Any) -> str:
    raw = str(token or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


def decode_jwt_exp(token: Any) -> float | None:
    """Decode the numeric ``exp`` claim from a JWT payload without network I/O."""
    if _NATIVE_DECODE_JWT_EXP is not None and isinstance(token, _NATIVE_SCALAR_TYPES):
        return _NATIVE_DECODE_JWT_EXP(token)
    raw = _strip_bearer(token)
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    segment = parts[1]
    padded = segment + ("=" * (-len(segment) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    exp_float = float(exp)
    if not math.isfinite(exp_float):
        return None
    return exp_float


def access_token_needs_refresh(
    token: Any,
    *,
    now: float | None = None,
    skew_seconds: int = DEFAULT_INSPECT_SKEW_SECONDS,
) -> tuple[bool, str]:
    """Return ``(needs_refresh, reason)`` based solely on local JWT inspection."""
    raw = _strip_bearer(token)
    if not raw:
        return True, "missing_token"
    exp = decode_jwt_exp(raw)
    if exp is None:
        return True, "malformed_token"
    now_ts = time.time() if now is None else float(now)
    skew = max(0, int(skew_seconds or 0))
    if exp <= now_ts:
        return True, "expired"
    if exp <= now_ts + skew:
        return True, "expiring_soon"
    return False, "valid"


def _account_marked_token_invalid(account: Mapping[str, Any]) -> bool:
    for field in ("status", "quota_status", "account_scan_status", "promotion_status"):
        value = str(account.get(field) or "").strip().lower()
        if not value:
            continue
        if value in _MARKED_INVALID_MARKERS:
            return True
    return False


def partition_accounts_for_inspection(
    accounts: Sequence[Mapping[str, Any]],
    *,
    now: float | None = None,
    skew_seconds: int = DEFAULT_INSPECT_SKEW_SECONDS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split accounts into ``(candidates, skipped_valid, skipped_deactivated)``."""
    now_ts = time.time() if now is None else float(now)
    candidates: list[dict[str, Any]] = []
    skipped_valid: list[dict[str, Any]] = []
    skipped_deactivated: list[dict[str, Any]] = []

    for row in accounts:
        if not isinstance(row, Mapping):
            continue
        account = dict(row)
        email = str(account.get("email") or "").strip().lower()
        if not email:
            continue
        if is_permanently_deactivated(account):
            skipped_deactivated.append(
                {
                    "email": email,
                    "status": "account_deactivated",
                    "reason": "account_deactivated",
                    "skipped": True,
                }
            )
            continue
        if _is_token_revoked_drop(account):
            skipped_deactivated.append(
                {
                    "email": email,
                    "status": "token_invalid",
                    "reason": "token_revoked_unrecoverable",
                    "skipped": True,
                }
            )
            continue
        needs, reason = access_token_needs_refresh(
            account.get("access_token"),
            now=now_ts,
            skew_seconds=skew_seconds,
        )
        if not needs and _account_marked_token_invalid(account):
            needs = True
            reason = "marked_invalid"
        if needs:
            candidates.append({"email": email, "reason": reason, "account": account})
        else:
            exp = decode_jwt_exp(account.get("access_token"))
            skipped_valid.append(
                {
                    "email": email,
                    "status": "valid",
                    "reason": "valid_jwt",
                    "exp": int(exp) if exp is not None else None,
                    "skipped": True,
                }
            )

    return candidates, skipped_valid, skipped_deactivated


def inspect_accounts_with_runner(
    emails: list[str] | None = None,
    *,
    refresh_quota_fn: Callable[..., dict[str, Any]],
    refresh_promotion_fn: Callable[..., dict[str, Any]] | None = None,
    load_accounts_fn: Callable[[list[str] | None], list[dict[str, Any]]] | None = None,
    workers: int = 4,
    proxy: str | None = None,
    proxy_pool: str | list[str] | None = None,
    timeout: int = 30,
    skew_seconds: int = DEFAULT_INSPECT_SKEW_SECONDS,
    relogin_on_401: bool = True,
    relogin_timeout: int | None = None,
    relogin_mode: str = "auto",
    batch_timeout: int | None = None,
    account_timeout: int | None = None,
    refresh_promotion: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """Run offline JWT triage, then probe/recover only accounts that need attention."""
    budgets = resolve_account_health_budgets(
        CFG,
        relogin_timeout=relogin_timeout,
        batch_timeout=batch_timeout,
        account_timeout=account_timeout,
    )
    loader = load_accounts_fn or _local_quota_accounts
    accounts = loader(emails)
    candidates, skipped_valid, skipped_deactivated = partition_accounts_for_inspection(
        accounts,
        now=now,
        skew_seconds=skew_seconds,
    )

    reason_counts: dict[str, int] = {}
    for entry in candidates:
        reason = str(entry.get("reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    quota_report: dict[str, Any] | None = None
    quota_results: list[dict[str, Any]] = []
    recovered_emails: list[str] = []
    failed_count = 0

    if candidates:
        candidate_emails = [entry["email"] for entry in candidates]
        reason_by_email = {entry["email"]: entry["reason"] for entry in candidates}
        quota_report = refresh_quota_fn(
            emails=candidate_emails,
            workers=max(1, int(workers or 4)),
            proxy=proxy,
            timeout=max(5, int(timeout or 30)),
            relogin_on_401=bool(relogin_on_401),
            relogin_timeout=budgets["relogin_timeout"],
            relogin_mode=str(relogin_mode or "auto"),
            batch_timeout=budgets["batch_timeout"],
            account_timeout=budgets["account_timeout"],
        )
        raw_results = quota_report.get("results") if isinstance(quota_report, dict) else []
        for item in raw_results if isinstance(raw_results, list) else []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            email = str(row.get("email") or "").strip().lower()
            if email and email in reason_by_email:
                row["triage_reason"] = reason_by_email[email]
            quota_results.append(row)
            if row.get("ok"):
                if email:
                    recovered_emails.append(email)
            else:
                failed_count += 1

    promotion_report: dict[str, Any] | None = None
    if refresh_promotion and recovered_emails:
        promo_runner = refresh_promotion_fn or refresh_promotion_statuses
        promotion_report = promo_runner(
            emails=recovered_emails,
            workers=max(1, int(workers or 4)),
            proxy=proxy,
            proxy_pool=proxy_pool,
            timeout=max(5, min(int(timeout or 20), 30)),
        )

    promotion_ok = True if promotion_report is None else bool(promotion_report.get("ok"))
    return {
        "ok": failed_count == 0 and promotion_ok,
        "mode": "lightweight_inspection",
        "total": len(accounts),
        "success": len(skipped_valid) + len(recovered_emails),
        "failed": failed_count,
        "skipped_valid": len(skipped_valid),
        "skipped_deactivated": len(skipped_deactivated),
        "inspected": len(candidates),
        "recovered": len(recovered_emails),
        "reasons": reason_counts,
        "promotion": promotion_report,
        "results": quota_results,
        "skipped": {
            "valid": [item["email"] for item in skipped_valid],
            "deactivated": [item["email"] for item in skipped_deactivated],
        },
    }


__all__ = [
    "DEFAULT_INSPECT_SKEW_SECONDS",
    "access_token_needs_refresh",
    "decode_jwt_exp",
    "inspect_accounts_with_runner",
    "partition_accounts_for_inspection",
]
