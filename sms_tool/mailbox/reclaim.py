"""Keep a paid-for mailbox reusable when its registration fails mid-flow.

Why this exists
---------------
A ReMail order is charged at **purchase** time, before registration starts. When
a run dies after the mailbox was claimed -- ``create_account`` answering ``409
invalid_state``, a proxy drop during OTP, an internal abort -- nothing kept the
supplier token: ``persist_*_result`` writes an audit row and the mailbox's
credentials live only in the failed run's memory and in the supplier panel.
Re-running therefore buys a **second** mailbox for the same slot.

Measured 2026-10-26: a three-account batch bought three iCloud mailboxes and
produced two accounts (the third died at ``create_account`` with
``invalid_state``); the third purchase could not be retried without paying
again.

Why the record is not written from ``persist_*_result``'s failure branch
-----------------------------------------------------------------------
That branch receives the **sanitized** result dict: ``outcome._failure_result``
runs the shared policy over it, so ``data["mailbox"]["token"]`` is already
``[REDACTED]`` (``sensitive_policy.json`` lists ``token`` under
``sensitive_key_fragments``; verified against the live policy). A record written
from there would carry a placeholder token, parse as a mailbox whose credential
is the literal string ``[REDACTED]``, and fail at OTP time -- a silent no-op that
*looks* like coverage. The record is therefore taken where the **raw** mailbox
object is still in scope: the workflow's failure funnel
(``registration_handlers._abort_result``) and its terminal assembly
(``registration_finalize.finalize``), both through
:func:`sms_tool.registration.result.record_failed_mailbox_purchase`.

What lands on disk
------------------
One canonical ReMail pool line per mailbox -- exactly the ``remail://`` form
:func:`sms_tool.mailbox.parsers.parse_mailbox_pool_line` reads -- so a retry is
the ordinary mailbox-file path::

    python -m sms_tool --mailbox-file runtime/mailbox_reclaim.txt --count 1 ...

The file holds a **live supplier token**. It lives under ``runtime/`` (gitignored)
and its content must never be logged; callers only ever report the masked email.

Which failures are worth reclaiming
-----------------------------------
The mailbox itself survives failures that belong to the *session* or the
*transport* (``auth_state`` / ``network`` / ``unknown`` / ``internal`` /
``cancelled``). It must NOT be reclaimed when the address is spent or already
owned: ``account_deactivated``, ``user_already_exists``,
``identity_provider_mismatch``, ``partial_registered``,
``phone_already_registered``, or when the credential itself is dead
(``invalid_grant`` / ``mailbox_auth_invalid`` / ``mailbox_token_expired``).
Re-offering those just burns another OTP round.

Set ``MAILBOX_RECLAIM_RECORD=0`` to disable the record entirely (operator escape
hatch, same shape as the other switches in this package).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: Reuse ledger consumed with ``--mailbox-file``; never committed (``runtime/``).
RECLAIM_FILE_NAME = "mailbox_reclaim.txt"

STATUS_RECORDED = "recorded"
STATUS_DUPLICATE = "duplicate"
STATUS_SKIPPED_NO_MAILBOX = "no_mailbox"
STATUS_SKIPPED_PROVIDER = "provider_not_supported"
STATUS_SKIPPED_NO_TOKEN = "no_token"
STATUS_SKIPPED_EXHAUSTED = "mailbox_exhausted"
STATUS_DISABLED = "disabled"
STATUS_ERROR = "error"

#: Only ReMail rows are written today: their pool line is the one whose format
#: carries the supplier token. Adding a provider means teaching
#: :func:`format_reclaim_line` that provider's line shape first -- writing a
#: guess would produce a file the parser silently skips.
RECLAIMABLE_PROVIDERS = frozenset({"remail"})

#: Failure classes whose cause is the session/transport, not the mailbox.
RECLAIMABLE_FAILURE_CLASSES = frozenset({"auth_state", "network", "unknown", "internal", "rate_limit", "cancelled"})

#: Error substrings that mean "do not offer this address again".
_EXHAUSTED_ERROR_MARKERS = (
    "account_deactivated",
    "user_already_exists",
    "identity_provider_mismatch",
    "partial_registered",
    "phone_already_registered",
    "mailbox_auth_invalid",
    "invalid_grant",
    "mailbox_token_expired",
    "mailbox_endpoint_unavailable",
)

#: A sanitized credential can never be replayed, so it is not a token.
_REDACTED_MARKERS = ("[REDACTED]", "***")


def reclaim_enabled() -> bool:
    """``MAILBOX_RECLAIM_RECORD=0`` disables the ledger (default: enabled)."""
    value = str(os.environ.get("MAILBOX_RECLAIM_RECORD", "") or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def reclaim_pool_path(config: Any = None) -> Path:
    """Ledger path inside ``runtime/`` for the given config (default: live config).

    The resolver is imported here rather than at module scope: ``kernel`` and the
    root ``cross_process_gate`` sit in a *lower* layer than ``mailbox`` for the
    purposes of ``scripts/import_layer_ratchet.py``, and that ratchet freezes
    module-level cross-directory edges. A function-local import is the
    documented way this codebase takes such a dependency (the same trade the
    payment/registration seams make), so the cost is visible in
    ``scripts/delayed_import_baseline.json`` instead of silently widening the
    architecture ceiling.
    """
    from ..kernel.paths import runtime_file

    return runtime_file(config or {}, RECLAIM_FILE_NAME)


def format_reclaim_line(mailbox: Any) -> str:
    """``remail://email---token[---order_no[---purchase_id]]`` for ``mailbox``.

    Returns ``""`` when the identity is incomplete. The column order and the
    ``---`` separator are the parser's, not ours: three dashes (not the four-dash
    chatai form), and trailing empty columns are omitted so the legacy
    ``email---token`` shape is what lands when the order identity is unknown.
    """
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    token = str(getattr(mailbox, "token", "") or "").strip()
    if not email or not token:
        return ""
    if any(marker in token for marker in _REDACTED_MARKERS):
        return ""
    fields = [email, token]
    order_no = str(getattr(mailbox, "order_no", "") or "").strip()
    purchase_id = str(getattr(mailbox, "purchase_id", "") or "").strip()
    if order_no or purchase_id:
        fields.append(order_no)
        if purchase_id:
            fields.append(purchase_id)
    return "remail://" + "---".join(fields)


def reclaimed_emails(path: Path | str | None = None, *, config: Any = None) -> set[str]:
    """Lower-cased emails already in the ledger (for dedupe and for operators)."""
    target = Path(path) if path else reclaim_pool_path(config)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    emails: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        payload = line.split("://", 1)[1] if "://" in line else line
        email = payload.split("---", 1)[0].strip().lower()
        if email:
            emails.add(email)
    return emails


def record_reclaimable_mailbox(
    mailbox: Any,
    *,
    failure_class: str = "",
    error: str = "",
    config: Any = None,
    path: Path | str | None = None,
) -> str:
    """Append ``mailbox`` to the reuse ledger when the failure left it spendable.

    Never raises: losing a bookkeeping line must not turn a failed registration
    into a crashed one. Every refusal is a status string so the caller (and its
    tests) can tell "not applicable" from "write failed".
    """
    try:
        if not reclaim_enabled():
            return STATUS_DISABLED
        if mailbox is None:
            return STATUS_SKIPPED_NO_MAILBOX
        provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
        if provider not in RECLAIMABLE_PROVIDERS:
            return STATUS_SKIPPED_PROVIDER
        line = format_reclaim_line(mailbox)
        if not line:
            return STATUS_SKIPPED_NO_TOKEN
        kind = str(failure_class or "").strip().lower()
        if not kind and error:
            from ..kernel.error_classification import classify_error

            kind = classify_error(error)
        if kind and kind not in RECLAIMABLE_FAILURE_CLASSES:
            return STATUS_SKIPPED_EXHAUSTED
        lowered = str(error or "").lower()
        if any(marker in lowered for marker in _EXHAUSTED_ERROR_MARKERS):
            return STATUS_SKIPPED_EXHAUSTED
        email = line.split("://", 1)[1].split("---", 1)[0].strip().lower()
        target = Path(path) if path else reclaim_pool_path(config)
        return _append_once(target, email, line)
    except Exception as exc:  # pragma: no cover - defensive: bookkeeping only
        _LOGGER.warning("mailbox reclaim record failed: %s", type(exc).__name__)
        return STATUS_ERROR


def _append_once(target: Path, email: str, line: str) -> str:
    """Append ``line`` once per email, under the cross-process write lock.

    Returns a status instead of raising: a ledger that cannot be written is a
    bookkeeping loss, and the caller must be able to say so without turning a
    failed registration into a crashed one.
    """
    lock_path = target.with_suffix(target.suffix + ".lock")
    from ..cross_process_gate import GateTimeoutError, cross_process_write_lock

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with cross_process_write_lock(lock_path):
            if email in reclaimed_emails(target):
                return STATUS_DUPLICATE
            needs_newline = False
            try:
                if target.stat().st_size > 0:
                    with open(target, "rb") as handle:
                        handle.seek(-1, os.SEEK_END)
                        needs_newline = handle.read(1) not in (b"\n", b"\r")
            except OSError:
                needs_newline = False
            with open(target, "a", encoding="utf-8", newline="\n") as handle:
                if needs_newline:
                    handle.write("\n")
                handle.write(line + "\n")
    except (OSError, GateTimeoutError) as exc:
        _LOGGER.warning("mailbox reclaim append failed: %s", type(exc).__name__)
        return STATUS_ERROR
    return STATUS_RECORDED


__all__ = [
    "RECLAIMABLE_FAILURE_CLASSES",
    "RECLAIMABLE_PROVIDERS",
    "RECLAIM_FILE_NAME",
    "STATUS_DISABLED",
    "STATUS_DUPLICATE",
    "STATUS_ERROR",
    "STATUS_RECORDED",
    "STATUS_SKIPPED_EXHAUSTED",
    "STATUS_SKIPPED_NO_MAILBOX",
    "STATUS_SKIPPED_NO_TOKEN",
    "STATUS_SKIPPED_PROVIDER",
    "format_reclaim_line",
    "reclaim_enabled",
    "reclaim_pool_path",
    "reclaimed_emails",
    "record_reclaimable_mailbox",
]
