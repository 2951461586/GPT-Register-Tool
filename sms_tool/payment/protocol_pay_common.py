"""Shared constants, structured errors, card loading, and pure helpers for protocol card payments."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .checkout_contract import CHATGPT_BASE, OPENAI_PAY_BASE, STRIPE_JS_ORIGIN
from .protocol_codec import billing_address_for, normalize_card

HOSTED_API_VERSION = "2020-08-27;custom_checkout_beta=v1"
HOSTED_ELEMENTS_VERSION = "2026-03-25.dahlia"
HOSTED_PAYMENT_UA = "stripe.js/2bae21f786; stripe-js-v3/2bae21f786; checkout"

_OPENAI_PAY_HOST = urlsplit(OPENAI_PAY_BASE).hostname or ""
_STRIPE_JS_HOST = urlsplit(STRIPE_JS_ORIGIN).hostname or ""
_CHATGPT_HOST = urlsplit(CHATGPT_BASE).hostname or ""
_CARD_SECRET_KEYS = ("number", "cvc", "exp_month", "exp_year")
_CS_ID_FULL_RE = re.compile(r"cs_(?:live|test)_[A-Za-z0-9_]+")
_SCRIPT_REF_RE = re.compile(r'(?:src|href)=["\']([^"\']+)["\']')
_MAX_HTML_BYTES = 4_000_000
_MAX_JS_BYTES = 8_000_000


class ProtocolPaymentError(RuntimeError):
    """Structured error raised by Hosted or Custom Checkout protocol operations."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "protocol_payment_error",
        error_stage: str = "protocol_payment",
        retryable: bool = False,
        status_code: int = 0,
        stripe_code: str = "",
        decline_code: str = "",
        confirm_error_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code or "protocol_payment_error")
        self.error_stage = str(error_stage or "protocol_payment")
        self.retryable = bool(retryable)
        self.status_code = int(status_code or 0)
        self.stripe_code = str(stripe_code or "")
        self.decline_code = str(decline_code or "")
        self.confirm_error_reason = str(confirm_error_reason or "")


def _new_device_id() -> str:
    return str(uuid.uuid4()) + uuid.uuid4().hex[:6]


def _local_device_ids(client_session_id: str = "") -> dict[str, str]:
    return {
        "guid": _new_device_id(),
        "muid": _new_device_id(),
        "sid": _new_device_id(),
        "client_session_id": client_session_id or str(uuid.uuid4()),
    }


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        parsed = response.json()
    except (ValueError, TypeError, AttributeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _stripe_error_fields(payload: Mapping[str, Any]) -> dict[str, str]:
    raw_err = payload.get("error")
    err = raw_err if isinstance(raw_err, Mapping) else {}
    raw_extra = err.get("extra_fields")
    extra = raw_extra if isinstance(raw_extra, Mapping) else {}
    return {
        "type": str(err.get("type") or ""),
        "code": str(err.get("code") or ""),
        "decline_code": str(err.get("decline_code") or ""),
        "param": str(err.get("param") or ""),
        "reason": str(extra.get("confirm_error_reason") or ""),
        "message": str(err.get("message") or "")[:300],
    }


def _stripe_error_from_intent(payload: Mapping[str, Any]) -> dict[str, str]:
    err = payload.get("last_payment_error") or payload.get("last_setup_error") or payload.get("error")
    if not isinstance(err, Mapping):
        return {}
    out: dict[str, str] = {}
    for key in ("code", "decline_code", "type", "message"):
        val = str(err.get(key) or "").strip()
        if val:
            out[key] = val[:240]
    return out


def _obj_id(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return str(value.get("id") or "").strip()
    return ""


def _hosted_stage(path: str) -> str:
    if path.endswith("/init"):
        return "init"
    if path.endswith("/confirm"):
        return "confirm"
    if path.endswith("/poll"):
        return "poll"
    if path == "elements/sessions":
        return "elements"
    return "tax_sync"


def _billing_email(billing: Mapping[str, str]) -> str:
    email = str(billing.get("email") or "").strip()
    if email:
        return email
    slug = re.sub(r"[^a-z0-9]+", ".", str(billing.get("name") or "cardholder").lower()).strip(".") or "cardholder"
    return f"{slug}.{uuid.uuid4().hex[:6]}@example.com"


def _resolve_billing(billing: Mapping[str, str] | None, *, country: str = "") -> dict[str, str]:
    target_country = str((billing or {}).get("country") or country or "US").strip().upper() or "US"
    base = billing_address_for(target_country)
    if isinstance(billing, Mapping):
        for key, value in billing.items():
            text = str(value or "").strip()
            if text:
                base[str(key)] = text
    base["country"] = str(base.get("country") or target_country).upper()
    return base


def _custom_outcome(
    state: str,
    stage: str,
    message: str,
    *,
    intent_status: str = "",
    code: str = "",
    decline_code: str = "",
    next_action: dict[str, Any] | None = None,
    checkout_url: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": state == "paid",
        "status": "completed" if state == "paid" else "failed",
        "state": state,
        "stage": stage,
        "message": message,
        "intent_status": intent_status,
        "code": code,
        "decline_code": decline_code,
        "manual_action_required": state == "challenge_required",
    }
    if checkout_url:
        result["checkout_url"] = checkout_url
    if next_action:
        result["next_action"] = next_action
    return result


def load_card_from_env_or_file(card_file: str = "") -> dict[str, str]:
    """Load and validate card JSON from ``card_file`` or ``GPT_CARD_JSON`` env var."""
    path_str = str(card_file or "").strip()
    raw_text = ""
    source = "GPT_CARD_JSON"
    if path_str:
        source = "card_file"
        try:
            raw_text = Path(path_str).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ProtocolPaymentError(
                f"failed to read card file: {exc}",
                error_code="card_file_unreadable",
                error_stage="validation",
            ) from exc
    else:
        raw_text = str(os.environ.get("GPT_CARD_JSON") or "").strip()
    if not raw_text:
        raise ProtocolPaymentError(
            "card data required via --card-file or GPT_CARD_JSON (or pass --payment-method-id)",
            error_code="missing_card_payload",
            error_stage="validation",
        )
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        raise ProtocolPaymentError(
            f"invalid JSON in {source}",
            error_code="invalid_card_json",
            error_stage="validation",
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ProtocolPaymentError(
            f"card payload in {source} must be a JSON object",
            error_code="invalid_card_payload",
            error_stage="validation",
        )
    try:
        return normalize_card(parsed)
    except ValueError as exc:
        raise ProtocolPaymentError(
            str(exc),
            error_code="invalid_card_fields",
            error_stage="validation",
        ) from exc
