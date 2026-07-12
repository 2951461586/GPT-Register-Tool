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
    "max retries exceeded",
    "/sentinel/req",
    "sentinel quickjs",
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
    "outlook otp timeout",
    "email_otp_poll_timeout",
    "password_verify_failed",
    "phone_recently_used",
    "unsupported_phone_number",
    "fraud_guard",
    "token_invalidated",
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
    if any(marker in text for marker in NETWORK_ERROR_MARKERS):
        return "network"
    return "unknown"


def is_account_failure(value) -> bool:
    return classify_error(value) == "account"


def is_network_failure(value) -> bool:
    return classify_error(value) == "network"
