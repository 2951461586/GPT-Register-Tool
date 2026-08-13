"""Read-only PayPal authorization-response context parser and normalizer.

Mirrors the *engineering pattern* (context validation + result normalization +
fatal-contingency classification) used by the external paypal-agreement-protocol
``elevation_flow.py``, WITHOUT porting any anti-bot / risk-evasion interaction.

This module ONLY parses and normalizes an already-obtained authorization
response (GraphQL / JSON).  It never crafts requests to evade risk controls.
The output is a :class:`PayPalAuthorizationContext` plus helpers that produce
dicts aligned with the project's :class:`sms_tool.payment_contracts.PaymentResult`
contract, so downstream code (``omakse_client``, ``payment_link_manager``) can
consume and gate retries uniformly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

# Regex for PayPal agreement / token identifiers.
_BA_RE = re.compile(r"BA-[A-Za-z0-9_.-]+")
_BILLING_AGREEMENT_RE = re.compile(r"B-[A-Za-z0-9_-]+")
_EC_RE = re.compile(r"(EC-[A-Z0-9]{17,})")

# A PayPal "billing without purchase" checkout is the expected context for a
# billing-agreement approval.
_EXPECTED_CHECKOUT_TYPES = {
    "BILLING_WITHOUT_PURCHASE",
    "billing_without_purchase",
    "BILLING",
}

# Fatal / account-level contingencies -> not retryable.
_FATAL_CONTINGENCIES = {
    "PAYER_ACCOUNT_RESTRICTED",
    "ACCOUNT_LOCKED",
    "ACCOUNT_CLOSED",
    "NOT_FOUND",
    "INVALID_BA_TOKEN",
    "BA_TOKEN_NOT_FOUND",
    "AGREEMENT_ALREADY_CREATED",
    "AGREEMENT_NOT_ACTIVE",
    "UNSUPPORTED_COUNTRY",
    "PAYER_CANNOT_PAY",
    "PERMISSION_DENIED",
}

# Transient / rate-limit / challenge signals -> retryable.
_TRANSIENT_SIGNALS = {
    "AUTH_CHALLENGE",
    "CAPTCHA_REQUIRED",
    "RATE_LIMIT",
    "TOO_MANY_REQUESTS",
    "INTERNAL_SERVER_ERROR",
    "UNAVAILABLE",
    "TIMEOUT",
}

_STATUS_NORMALIZED = {
    "completed": "completed",
    "approved": "completed",
    "success": "completed",
    "failed": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "unknown": "unknown",
}


def _dig(value: Mapping[str, Any], *path: str, default: Any = None) -> Any:
    """Case-insensitive nested dict lookup across a path of keys."""
    node: Any = value
    for key in path:
        if not isinstance(node, Mapping):
            return default
        found = None
        for k, v in node.items():
            if str(k).strip().lower() == str(key).strip().lower():
                found = v
                break
        if found is None:
            return default
        node = found
    return node


def _as_text(value: Any) -> str:
    return str(value or "").strip()


@dataclass
class PayPalAuthorizationContext:
    """Normalized read of a PayPal authorization response."""

    checkout_session_type: str = ""
    billing_agreement_id: str = ""
    ba_token: str = ""
    ec_token: str = ""
    funding_context: Mapping[str, Any] = field(default_factory=dict)
    approved: bool = False
    status: str = ""          # normalized: completed / failed / cancelled / unknown
    error_code: str = ""
    error_stage: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkout_session_type": self.checkout_session_type,
            "billing_agreement_id": self.billing_agreement_id,
            "ba_token": self.ba_token,
            "ec_token": self.ec_token,
            "approved": self.approved,
            "status": self.status,
            "error_code": self.error_code,
            "error_stage": self.error_stage,
        }


def _token_from_text(text: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(text or "")
    return match.group(0) if match else ""


def parse_authorization_context(
    payload: Mapping[str, Any] | str | None,
    *,
    ba_token: str = "",
) -> PayPalAuthorizationContext:
    """Parse a PayPal authorization response body into a normalized context.

    ``payload`` may be a dict (GraphQL JSON) or a raw string (HTML / text).
    Only reads; performs no network interaction.
    """
    raw: dict[str, Any]
    if isinstance(payload, Mapping):
        raw = dict(payload)
        text = json.dumps(raw, ensure_ascii=False)
    elif payload is None:
        raw, text = {}, ""
    else:
        text = str(payload)
        try:
            parsed = json.loads(text)
            raw = dict(parsed) if isinstance(parsed, Mapping) else {}
        except (ValueError, TypeError):
            raw = {}

    checkout_session_type = _as_text(
        _dig(raw, "checkoutSessionType")
        or _dig(raw, "checkout_session_type")
        or _dig(raw, "data", "checkoutSessionType")
    )
    billing_agreement_id = _as_text(
        _dig(raw, "billingAgreementId")
        or _dig(raw, "billing_agreement_id")
        or _dig(raw, "data", "billingAgreementId")
        or _token_from_text(text, _BILLING_AGREEMENT_RE)
    )
    ctx_ba = _as_text(
        _dig(raw, "baToken") or _dig(raw, "ba_token") or _token_from_text(text, _BA_RE)
    )
    ctx_ec = _as_text(
        _dig(raw, "ecToken") or _dig(raw, "ec_token") or _token_from_text(text, _EC_RE)
    )

    approved = _as_bool(
        _dig(raw, "approved")
        or _dig(raw, "isApproved")
        or _dig(raw, "data", "approved")
    )
    if approved is None:
        # heuristics: a concrete B- id or BA token alongside no error implies ok
        approved = bool(billing_agreement_id) and not _contains_fatal(raw)

    # find an explicit error code / message
    error_code = _as_text(
        _dig(raw, "errorCode")
        or _dig(raw, "error_code")
        or _dig(raw, "errors", 0, "code")
        or _dig(raw, "error", "code")
    )
    if not error_code:
        for candidate in _FATAL_CONTINGENCIES | _TRANSIENT_SIGNALS:
            if candidate.lower() in text.lower():
                error_code = candidate
                break

    funding = _dig(raw, "fundingContext", default={})
    if not isinstance(funding, Mapping):
        funding = _dig(raw, "data", "buyerFundingContext", default={})
        if not isinstance(funding, Mapping):
            funding = _dig(raw, "buyerFundingContext", default={})
    if not isinstance(funding, Mapping):
        funding = {}

    # normalize status
    raw_status = _as_text(
        _dig(raw, "status") or _dig(raw, "state") or _dig(raw, "data", "status")
    ).lower()
    status = _STATUS_NORMALIZED.get(raw_status, "")
    if not status:
        if approved:
            status = "completed"
        elif error_code:
            status = "failed"
        else:
            status = "unknown"

    error_stage = _as_text(_dig(raw, "errorStage") or _dig(raw, "error_stage"))
    if not error_stage:
        # infer from which phase surfaces the error
        if error_code and error_code not in _FATAL_CONTINGENCIES:
            error_stage = "authorization"

    return PayPalAuthorizationContext(
        checkout_session_type=checkout_session_type,
        billing_agreement_id=billing_agreement_id,
        ba_token=ctx_ba or _as_text(ba_token),
        ec_token=ctx_ec,
        funding_context=funding,
        approved=approved,
        status=status,
        error_code=error_code,
        error_stage=error_stage,
        raw=raw,
    )


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "approved", "success"}:
            return True
        if normalized in {"0", "false", "no", "failed", "error"}:
            return False
    return None


def _contains_fatal(raw: Mapping[str, Any]) -> bool:
    text = json.dumps(raw, ensure_ascii=False).lower()
    return any(cont.lower() in text for cont in _FATAL_CONTINGENCIES)


def classify_authorization_outcome(ctx: PayPalAuthorizationContext) -> dict[str, Any]:
    """Classify a parsed context into a normalized outcome dict.

    Returns fields aligned with ``PaymentResult.from_mapping``:
    ``ok / status / error_code / error_stage / retryable``.
    """
    checkout_type = (ctx.checkout_session_type or "").upper()
    expected_match = checkout_type in {t.upper() for t in _EXPECTED_CHECKOUT_TYPES}

    ok = ctx.approved and bool(ctx.billing_agreement_id)
    error_code = ctx.error_code
    retryable = False

    if ok:
        status = "completed"
        error_code = ""
    elif checkout_type and not expected_match:
        status = "failed"
        error_code = error_code or "unexpected_checkout_type"
        retryable = False
    elif ctx.error_code in _FATAL_CONTINGENCIES:
        status = "failed"
        retryable = False
    elif ctx.error_code in _TRANSIENT_SIGNALS:
        status = "failed"
        retryable = True
    elif ctx.status in {"cancelled", "unknown"}:
        status = ctx.status
        retryable = ctx.status == "unknown"
    else:
        status = "failed"
        error_code = error_code or "authorization_failed"
        # conservative: pre-side-effect failures are retryable
        retryable = True

    return {
        "ok": ok,
        "status": status,
        "error_code": error_code,
        "error_stage": ctx.error_stage,
        "retryable": retryable,
        "checkout_session_type": ctx.checkout_session_type,
        "billing_agreement_id": ctx.billing_agreement_id,
        "ba_token": ctx.ba_token,
    }


def to_payment_result(
    ctx: PayPalAuthorizationContext,
    *,
    payment_method: str = "paypal",
    operation: str = "execute_payment",
    url: str = "",
) -> dict[str, Any]:
    """Map a parsed context to a dict consumable by ``PaymentResult.from_mapping``."""
    outcome = classify_authorization_outcome(ctx)
    result: dict[str, Any] = dict(outcome)
    result.update({
        "payment_method": payment_method,
        "operation": operation,
        "error": (
            _as_text(ctx.raw.get("error") or ctx.raw.get("message"))
            if not outcome["ok"]
            else ""
        ),
    })
    if url:
        result["url"] = url
    return result