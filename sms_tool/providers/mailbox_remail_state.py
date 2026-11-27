"""ReMail error types, registry persistence, quality metrics, and message parsers.

Extracted from :mod:`sms_tool.providers.mailbox_remail` so stateless parsing,
dead-mailbox persistence, and error contracts live apart from HTTP order and
OTP polling flows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from ..mailbox.mail_otp import (
    candidate_is_newer as candidate_is_newer,
    email_otp_candidate as email_otp_candidate,
    extract_otp_from_text as extract_otp_from_text,
    message_received_ts,
)
from ..phone.phone_proxy import redact_proxy_url

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://remail.best"
DEFAULT_PROJECT_ID = 2
_DEAD_REMAIL_LOCK = threading.Lock()
_OPENAI_REMAIL_SENDERS = re.compile(r"(?i)(?:otp@tm1\.openai\.com|noreply@tm\.openai\.com|(?:otp|noreply)@openai\.com)")
_REMAIL_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

#: ``OrderBatchItemErrorResponse.code`` enum from the live OpenAPI document.
_REMAIL_ITEM_ERROR_HINTS = {
    "insufficient_balance": "ReMail wallet balance is exhausted — recharge it",
    "insufficient_inventory": "the requested suffix has no stock right now",
    "idempotency_conflict": "the batch Idempotency-Key was reused with a different payload",
    "temporarily_unavailable": "supplier-side transient outage — safe to retry the same batch",
}

_FETCH_STATUS_READY = frozenset({"normal", "succeeded"})
_FETCH_STATUS_SETTLING = frozenset({"pending", "processing"})
_FETCH_STATUS_FAILING = frozenset({"abnormal", "failed"})


def _remail_idempotency_key(
    mode: str,
    supply: str,
    payload: dict,
    *,
    batch_id: str = "",
) -> str:
    """Stable idempotency key for a ReMail batch order."""
    envelope: dict[str, object] = {
        "serviceMode": mode,
        "supply": supply,
        "payload": payload,
    }
    if batch_id:
        envelope["batchId"] = str(batch_id)
    canonical = json.dumps(
        envelope,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReMailHttpError(RuntimeError):
    def __init__(self, status_code, body):
        self.status_code = int(status_code or 0)
        self.body = body
        super().__init__(f"ReMail HTTP {self.status_code}: {body}")


class ReMailTransportError(RuntimeError):
    """Transport failure reaching ReMail through the selected proxy."""

    def __init__(self, *, email="", proxy="", attempts=0, detail=""):
        self.code = "mailbox_transport_unavailable"
        self.email = str(email or "").strip().lower()
        self.proxy = str(proxy or "").strip()
        self.attempts = int(attempts or 0)
        self.detail = str(detail or "").strip()
        mailbox = f" for {self.email}" if self.email else ""
        route = f" via {redact_proxy_url(self.proxy, empty_placeholder='DIRECT')}" if self.proxy else ""
        suffix = f" after {self.attempts} attempt(s)" if self.attempts else ""
        super().__init__(f"{self.code}{mailbox}{route}{suffix}: {self.detail}".strip())


class ReMailCredentialError(RuntimeError):
    """A mailbox or provider credential cannot be used for pickup anymore."""

    def __init__(self, *, code, email, request_id="", detail=""):
        self.code = str(code or "remail_credential_invalid")
        self.email = str(email or "").strip().lower()
        self.request_id = str(request_id or "").strip()
        self.detail = str(detail or "").strip()
        suffix = f" requestId={self.request_id}" if self.request_id else ""
        mailbox = f" for {self.email}" if self.email else ""
        message = f"{self.code}{mailbox}{suffix}"
        if self.detail:
            message += f": {self.detail}"
        super().__init__(message)


def _remail_request_id(error):
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return str(body.get("requestId") or body.get("request_id") or "").strip()
    return ""


def _remail_identity(value):
    if isinstance(value, dict):
        _raw_mailbox: object = value.get("mailbox")
        mailbox: dict = _raw_mailbox if isinstance(_raw_mailbox, dict) else {}
        _raw_purchase: object = value.get("purchase")
        purchase: dict = _raw_purchase if isinstance(_raw_purchase, dict) else {}
        email = value.get("email") or mailbox.get("email") or purchase.get("email")
        order_no = value.get("order_no") or mailbox.get("order_no") or purchase.get("order_no")
        purchase_id = value.get("purchase_id") or mailbox.get("purchase_id") or purchase.get("purchase_id")
    else:
        email = getattr(value, "email", "")
        order_no = getattr(value, "order_no", "")
        purchase_id = getattr(value, "purchase_id", "")
    return {
        "email": str(email or "").strip().lower(),
        "order_no": str(order_no or "").strip(),
        "purchase_id": str(purchase_id or "").strip(),
    }


def _remail_identity_keys(value):
    identity = _remail_identity(value)
    return {f"{key}:{item.lower() if key == 'email' else item}" for key, item in identity.items() if item}


def read_dead_remail_registry_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return []
    records = payload.get("accounts") if isinstance(payload, dict) else payload
    return [item for item in (records or []) if isinstance(item, dict)]


def record_dead_remail_account_file(path: Path, value, *, reason="account_deactivated") -> bool:
    identity = _remail_identity(value)
    if not identity["email"]:
        return False
    now = int(time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    with _DEAD_REMAIL_LOCK:
        records = read_dead_remail_registry_file(path)
        target_keys = _remail_identity_keys(identity)
        existing = next((item for item in records if target_keys & _remail_identity_keys(item)), None)
        if existing is None:
            existing = {**identity, "first_seen_at": now}
            records.append(existing)
        else:
            for key, item in identity.items():
                if item:
                    existing[key] = item
        existing["reason"] = str(reason or "account_deactivated")[:120]
        existing["last_seen_at"] = now
        payload = {"version": 1, "accounts": records}
        temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    return True


def record_remail_batch_quality_file(
    path: Path,
    batch_id,
    results,
    *,
    requested=0,
    threshold: float = 0.25,
) -> dict[str, Any]:
    rows = [row for row in (results or []) if isinstance(row, dict)]
    total = max(int(requested or 0), len(rows))
    deactivated = sum("account_deactivated" in str(row.get("error") or "").lower() for row in rows)
    otp_timeout = sum(
        "otp" in str(row.get("error") or "").lower() and "timeout" in str(row.get("error") or "").lower()
        for row in rows
    )
    dead_rate = (deactivated / total) if total else 0.0
    payload = {
        "batch_id": str(batch_id or "")[:100],
        "requested": total,
        "completed": len(rows),
        "account_deactivated": deactivated,
        "otp_timeout": otp_timeout,
        "dead_rate": round(dead_rate, 6),
        "stop_threshold": threshold,
        "halt_replenishment": bool(total and dead_rate >= threshold),
        "created_at": int(time.time()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with _DEAD_REMAIL_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return payload


def redact_remail_value(value, secret_values) -> Any:
    safe = deepcopy(value)
    secrets_list = [str(item) for item in secret_values if str(item or "")]

    def clean(item):
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        if isinstance(item, list):
            return [clean(child) for child in item]
        text = str(item)
        for secret in secrets_list:
            text = text.replace(secret, "[REDACTED]")
        return text

    return clean(safe)


def _recoverable_batch_error(exc):
    if isinstance(exc, ReMailHttpError):
        return exc.status_code in {408, 425, 429} or exc.status_code >= 500
    return "ReMail request failed:" in str(exc)


def _order_created_timestamp(order):
    raw = str((order or {}).get("createdAt") or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _batch_item_failure(item):
    """``{index, code, message}`` for one failed batch item (never a credential)."""
    item = item if isinstance(item, dict) else {}
    _raw_error: object = item.get("error")
    error: dict = _raw_error if isinstance(_raw_error, dict) else {}
    return {
        "index": item.get("index"),
        "code": str(error.get("code") or "").strip() or "unknown",
        "message": str(error.get("message") or "").strip()[:160],
    }


def _summarize_item_failures(failures: list[dict]) -> str:
    counts = {}
    for failure in failures:
        counts[failure["code"]] = counts.get(failure["code"], 0) + 1
    summary = ", ".join(f"{code}={count}" for code, count in sorted(counts.items()))
    first = failures[0] if failures else {}
    detail = f"; first: {first.get('message')}" if first.get("message") else ""
    return f"codes[{summary}]{detail}"


def _batch_failure_reason(failures: list[dict]) -> str:
    """Stable, actionable error for a batch that produced no mailbox."""
    codes = {str(failure.get("code") or "") for failure in failures}
    if codes and codes <= {"insufficient_balance"}:
        return "remail_insufficient_balance: " + _REMAIL_ITEM_ERROR_HINTS["insufficient_balance"]
    if codes and codes <= {"idempotency_conflict"}:
        return "remail_idempotency_conflict: " + _REMAIL_ITEM_ERROR_HINTS["idempotency_conflict"]
    if "temporarily_unavailable" in codes:
        return "remail_temporarily_unavailable: " + _REMAIL_ITEM_ERROR_HINTS["temporarily_unavailable"]
    if codes and codes <= {"insufficient_inventory"}:
        return "remail_insufficient_inventory: " + _REMAIL_ITEM_ERROR_HINTS["insufficient_inventory"]
    return "ReMail batch returned no usable mailboxes" + (
        f" ({_summarize_item_failures(failures)})" if failures else ""
    )


def _normalize_remail_message(message):
    message = message or {}
    recipient = str(message.get("recipient") or "").strip().lower()
    verification_code = str(message.get("verificationCode") or "").strip()
    preview = str(message.get("bodyPreview") or "")
    if verification_code and verification_code not in preview:
        preview = verification_code + "\n" + preview
    return {
        "id": str(message.get("id") or ""),
        "from": str(message.get("sender") or ""),
        "toRecipients": [{"emailAddress": {"address": recipient}}] if recipient else [],
        "receivedDateTime": str(message.get("receivedAt") or ""),
        "subject": str(message.get("subject") or ""),
        "bodyPreview": preview,
        "body": {"content": str(message.get("body") or "")},
        "verificationCode": verification_code,
    }


def _remail_parse_fetch_next_allowed(response):
    """Parse fetch.nextFetchAllowedAt from the /v1/pickup response."""
    try:
        fetch_info = (response or {}).get("fetch") or {}
        next_allowed = str(fetch_info.get("nextFetchAllowedAt") or "").strip()
        if not next_allowed:
            return 0.0
        dt = datetime.fromisoformat(next_allowed.replace("Z", "+00:00"))
        wait = dt.timestamp() - time.time()
        return max(0.0, wait)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _remail_fetch_state(response):
    """Parse the fetch state from /v1/pickup response."""
    try:
        fetch_info = (response or {}).get("fetch") or {}
        last_status = str(fetch_info.get("lastStatus") or "").strip().lower()
        return {
            "last_status": last_status,
            "ready": last_status not in _FETCH_STATUS_SETTLING | _FETCH_STATUS_FAILING,
            "last_received_at": str(fetch_info.get("lastReceivedAt") or "").strip(),
            "next_allowed_delay": _remail_parse_fetch_next_allowed(response),
            "last_safe_error": str(fetch_info.get("lastSafeError") or "").strip(),
        }
    except (AttributeError, TypeError, ValueError):
        return {
            "last_status": "",
            "ready": True,
            "last_received_at": "",
            "next_allowed_delay": 0.0,
            "last_safe_error": "",
        }


def _remail_item_received_ts(item):
    """Parse receivedAt timestamp from a raw ReMail message item."""
    return message_received_ts({"receivedDateTime": str((item or {}).get("receivedAt") or "")})


def _trusted_structured_remail_code(mailbox, item):
    """Return ReMail's structured OTP only for the target mailbox and a known OpenAI sender."""
    code = str((item or {}).get("verificationCode") or "").strip()
    recipient = str((item or {}).get("recipient") or "").strip().lower()
    target = str(getattr(mailbox, "email", "") or "").strip().lower()
    sender = str((item or {}).get("sender") or "")
    if not re.fullmatch(r"\d{6}", code):
        return ""
    if not recipient or recipient != target:
        return ""
    if not _OPENAI_REMAIL_SENDERS.search(sender):
        return ""
    return code


def _fetch_state_interval(interval, fetch_state, prev_safe_error):
    """Apply the documented ``fetch.lastStatus`` to the poll interval."""
    last_status = str((fetch_state or {}).get("last_status") or "")
    if last_status in _FETCH_STATUS_FAILING:
        safe_error = str((fetch_state or {}).get("last_safe_error") or "").strip()
        if safe_error and safe_error != prev_safe_error:
            _LOGGER.info("  [remail fetch abnormal: %s]", safe_error[:80])
            prev_safe_error = safe_error
        return max(interval, 3.0), prev_safe_error
    if last_status in _FETCH_STATUS_SETTLING:
        return max(interval, 2.0), prev_safe_error
    return interval, prev_safe_error
