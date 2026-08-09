"""Pure, provider-neutral helpers shared by protocol-payment scripts."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable


RESULT_SCHEMA = "protocol_payment.v1"

_SENSITIVE_RE = re.compile(
    r"(?is)(access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|"
    r"client[_-]?secret|api[_-]?key|ba[_-]?token|totp(?:[_-]?secret)?|"
    r"card(?:[_-]?(?:number|cvv|cvc))?|blik[_-]?code)(\s*[=:]\s*)['\"]?[^\s,}\"']+"
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")
_BA_RE = re.compile(r"\bBA-[A-Za-z0-9_.-]+\b")


def sanitize_text(value: Any) -> str:
    text = str(value or "")
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED]", text)
    text = _BA_RE.sub("[REDACTED]", text)
    return _SENSITIVE_RE.sub(r"\1\2[REDACTED]", text)


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if any(part in str(key).lower() for part in ("token", "secret", "card", "cvv", "blik_code")) else sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


@dataclass(frozen=True)
class ProtocolResult:
    payment_method: str
    ok: bool
    status: str
    operation: str = "extract_link"
    url: str = ""
    link_type: str = ""
    message: str = ""
    error: str = ""
    error_code: str = ""
    error_stage: str = ""
    retryable: bool = False
    side_effect_started: bool = False
    requires_reconciliation: bool = False
    schema: str = RESULT_SCHEMA

    def to_json(self) -> str:
        return json.dumps(sanitize_payload(asdict(self)), ensure_ascii=False, separators=(",", ":"))


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError:
        return max(minimum, default)


def collect_strings(payload: Any, result: list[str] | None = None) -> list[str]:
    values = result if result is not None else []
    if isinstance(payload, str):
        values.append(payload)
    elif isinstance(payload, dict):
        for value in payload.values():
            collect_strings(value, values)
    elif isinstance(payload, list):
        for item in payload:
            collect_strings(item, values)
    return values


def amount_from_payload(payload: Any) -> int:
    if isinstance(payload, dict):
        total_summary = payload.get("total_summary")
        if isinstance(total_summary, dict) and total_summary.get("due") is not None:
            return int(total_summary.get("due") or 0)
        invoice = payload.get("invoice")
        if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
            return int(invoice.get("amount_due") or 0)
        line_items = payload.get("line_items")
        if isinstance(line_items, list):
            amounts = [
                int(item.get("amount") or 0)
                for item in line_items
                if isinstance(item, dict) and item.get("amount") is not None
            ]
            if amounts:
                return sum(amounts)
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    for pattern in (
        r'"total"\s*:\s*(\d+)',
        r'"amount_total"\s*:\s*(\d+)',
        r'"checkout_amount"\s*:\s*(\d+)',
        r'"amount"\s*:\s*(\d+)',
    ):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return 0


def collect_urls(payload: Any, urls: list[str] | None = None) -> list[str]:
    found = urls if urls is not None else []
    if isinstance(payload, str):
        for match in re.findall(r"https?://[^\s\"'<>]+", payload):
            found.append(match.rstrip("),.;]"))
        for match in re.findall(r"data:image/(?:png|svg\+xml|jpeg);base64,[A-Za-z0-9+/=]+", payload):
            found.append(match)
    elif isinstance(payload, dict):
        for value in payload.values():
            collect_urls(value, found)
    elif isinstance(payload, list):
        for item in payload:
            collect_urls(item, found)
    return found


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        value = payload.get("submission_attempt")
        if isinstance(value, dict):
            return value
        for item in payload.values():
            nested = find_submission_attempt(item)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = find_submission_attempt(item)
            if nested:
                return nested
    return {}


def extract_redirect_url(
    payload: Any,
    is_redirect_like: Callable[[Any, bool], bool],
) -> str:
    if isinstance(payload, dict):
        next_action = payload.get("next_action")
        if isinstance(next_action, dict):
            redirect = next_action.get("redirect_to_url")
            if isinstance(redirect, dict):
                url = str(redirect.get("url") or "").strip()
                if is_redirect_like(url, True):
                    return url
            for key in ("url", "redirect_url", "redirect_to_url", "hosted_url"):
                value = next_action.get(key)
                if is_redirect_like(value, True):
                    return str(value)
        for key in ("redirect_url", "redirect_to_url", "authorization_url", "authentication_url"):
            value = payload.get(key)
            if is_redirect_like(value, True):
                return str(value)
        for value in payload.values():
            nested = extract_redirect_url(value, is_redirect_like)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = extract_redirect_url(item, is_redirect_like)
            if nested:
                return nested
    return ""


def first_value_by_key(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = first_value_by_key(value, key)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = first_value_by_key(item, key)
            if found not in (None, "", [], {}):
                return found
    return None
