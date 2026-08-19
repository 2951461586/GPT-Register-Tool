"""Shared failure classification for batch-safe account handling."""

from __future__ import annotations

import json


NETWORK_ERROR_MARKERS = (
    "tls",
    "ssl",
    "sslerror",
    "eof occurred",
    "connection",
    "connect error",
    "timeout",
    "timed out",
    "proxy",
    "socks",
    "dns",
    "name resolution",
    "winerror 10060",
    "curl: (35)",
    "curl: (28)",
    "curl: (6)",
    "curl: (7)",
    "remote disconnected",
    "connection reset",
    "connection aborted",
    "session_circuit_open",
    "max retries exceeded",
    "/sentinel/req",
    "sentinel quickjs",
    "sentinel_extract_failed",
    "cloudflare",
    "just a moment",
)

ACCOUNT_ERROR_MARKERS = (
    "account_deactivated",
    "account deactivated",
    "account has been deactivated",
    "deleted or deactivated",
    "registration_disallowed",
    "invalid_grant",
    "authenticationfailed",
    "invalid credentials",
    "wrong_email_otp_code",
    "password_verify_failed",
    "phone_recently_used",
    "unsupported_phone_number",
    "fraud_guard",
    "token_invalidated",
)

MAILBOX_ERROR_MARKERS = (
    "outlook otp timeout",
    "email_otp_poll_timeout",
    "mailbox otp timeout",
)

AUTH_STATE_ERROR_MARKERS = (
    "invalid_auth_step",
    "invalid_state",
    "sign-in session is no longer valid",
    "signup_auth_state",
)

RATE_LIMIT_ERROR_MARKERS = (
    "rate_limit_exceeded",
    "registration_rate_limited",
    "registration_rate_limit_circuit_open",
    "too many requests",
    "http_429",
)


def error_text(value) -> str:
    if isinstance(value, dict):
        parts = []
        for key in ("error", "error_code", "message", "body", "status", "scan_status", "quota_status"):
            item = value.get(key)
            if item:
                parts.append(str(item))
        for key in ("refresh", "oauth", "relogin", "token_probe"):
            item = value.get(key)
            if isinstance(item, dict):
                parts.append(error_text(item))
        if not parts:
            try:
                parts.append(json.dumps(value, ensure_ascii=False, default=str)[:1000])
            except Exception:
                pass
        return " ".join(parts).lower()
    return str(value or "").lower()


def classify_error(value) -> str:
    text = error_text(value)
    if any(marker in text for marker in ACCOUNT_ERROR_MARKERS):
        return "account"
    if any(marker in text for marker in MAILBOX_ERROR_MARKERS):
        return "mailbox"
    if any(marker in text for marker in RATE_LIMIT_ERROR_MARKERS):
        return "rate_limit"
    if any(marker in text for marker in NETWORK_ERROR_MARKERS):
        return "network"
    if any(marker in text for marker in AUTH_STATE_ERROR_MARKERS):
        return "auth_state"
    return "unknown"


def is_account_failure(value) -> bool:
    return classify_error(value) == "account"


def is_network_failure(value) -> bool:
    return classify_error(value) == "network"
