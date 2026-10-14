"""Pure payload helpers shared by the iDEAL / TWINT / BLIK extractors.

These six functions were byte-identical copies in all three extractors. They
depend only on the payload they are given (no module-level state, no provider
prefix), so there is one definition here instead of three.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import unquote

from .protocol_core import collect_strings, find_submission_attempt, first_value_by_key
from .proxy_bookkeeping import find_named_token

__all__ = [
    "find_session_cookie",
    "format_payment_summary",
    "infer_processor_entity",
    "normalize_access_token",
    "payment_page_summary",
    "stripe_browser_id",
]


def stripe_browser_id() -> str:
    return f"{uuid.uuid4()}{uuid.uuid4().hex[:8]}"


def find_session_cookie(payload: Any) -> str:
    for value in collect_strings(payload):
        match = re.search(r"(?:^|[;\s])__Secure-next-auth\.session-token=([^;\s]+)", value)
        if match:
            return unquote(match.group(1))
    return ""


def normalize_access_token(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return "", ""
    session_token = ""
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
            access_token = find_named_token(
                data,
                (
                    "accessToken",
                    "access_token",
                    "token",
                    "bearerToken",
                    "bearer_token",
                    "jwt",
                ),
            )
            session_token = find_named_token(
                data,
                (
                    "sessionToken",
                    "session_token",
                    "nextAuthSessionToken",
                    "next_auth_session_token",
                    "__Secure-next-auth.session-token",
                    "secureNextAuthSessionToken",
                ),
            ) or find_session_cookie(data)
            text = access_token
        except json.JSONDecodeError:
            pass
    return text, session_token


def infer_processor_entity(payload: Any) -> str:
    for value in collect_strings(payload):
        match = re.search(r"[?&]processor_entity=([A-Za-z0-9_]+)", value)
        if match:
            return match.group(1)
    return ""


def payment_page_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    elements_options = payload.get("elements_options") if isinstance(payload.get("elements_options"), dict) else {}
    submission = find_submission_attempt(payload)
    next_action = first_value_by_key(payload, "next_action")
    payment_intent = first_value_by_key(payload, "payment_intent")
    setup_intent = first_value_by_key(payload, "setup_intent")
    summary: dict[str, Any] = {
        "object": payload.get("object"),
        "id": payload.get("id"),
        "status": payload.get("status"),
        "payment_status": payload.get("payment_status"),
        "amount": elements_options.get("amount") if elements_options else first_value_by_key(payload, "amount"),
        "currency": payload.get("currency") or (elements_options.get("currency") if elements_options else None),
        "mode": elements_options.get("mode") if elements_options else payload.get("mode"),
        "payment_method_types": elements_options.get("payment_method_types") if elements_options else None,
        "submission_state": submission.get("state") if submission else None,
        "submission_status": submission.get("status") if submission else None,
        "has_next_action": isinstance(next_action, dict) and bool(next_action),
    }
    if isinstance(payment_intent, dict):
        summary["payment_intent_status"] = payment_intent.get("status")
    elif isinstance(payment_intent, str):
        summary["payment_intent"] = payment_intent
    if isinstance(setup_intent, dict):
        summary["setup_intent_status"] = setup_intent.get("status")
    elif isinstance(setup_intent, str):
        summary["setup_intent"] = setup_intent
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def format_payment_summary(summary: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in summary.items())
