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
    "mailbox_auth_invalid",
    "mailbox_endpoint_unavailable",
    "remail_api_auth_invalid",
    "outlook otp timeout",
    "email_otp_poll_timeout",
    "mailbox otp timeout",
    "mailbox_transport_unavailable",
    "relogin_mailbox_transport_failed",
    "remail poll transport",
)

AUTH_STATE_ERROR_MARKERS = (
    "browser_email_field_not_editable",
    "invalid_auth_step",
    "invalid_state",
    "sign-in session is no longer valid",
    "signup_auth_state",
    "browser_registration_state_unknown",
    "browser_email_verification_stuck",
    "browser_auth_state",
)

RATE_LIMIT_ERROR_MARKERS = (
    "rate_limit_exceeded",
    "registration_rate_limited",
    "registration_rate_limit_circuit_open",
    "too many requests",
    "http_429",
)

CANCELLED_ERROR_MARKERS = ("registration_cancelled", "cancelled_by_user")

INTERNAL_ERROR_MARKERS = (
    "registration_internal_error",
    "nameerror",
    "attributeerror",
    "typeerror",
    "keyerror",
    "importerror",
    "unboundlocalerror",
    "notimplementederror",
    " is not defined",
)

CONFIGURATION_ERROR_MARKERS = (
    "unsupported_registration_driver",
    "missing_dependency",
    "invalid_configuration",
    "configuration_error",
)

# Hard stops: retrying cannot change the outcome. Kept beside classify_error
# because this is pure classification -- the transport layer needs it and must
# not import registration policy to get it.
TERMINAL_ERROR_MARKERS = (
    "manual_challenge_required", "browser_proxy_blocked", "mailbox_auth_invalid",
    "mailbox_endpoint_unavailable", "remail_api_auth_invalid", "invalid_grant",
    "registration_cancelled", "session_circuit_open", "registration_rate_limit",
    "http_429", "stage_budget_exceeded",
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
    if any(marker in text for marker in CANCELLED_ERROR_MARKERS):
        return "cancelled"
    if any(marker in text for marker in INTERNAL_ERROR_MARKERS):
        return "internal"
    if any(marker in text for marker in CONFIGURATION_ERROR_MARKERS):
        return "configuration"
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


def is_terminal_registration_error(value) -> bool:
    """True when the error is a hard stop: no retry can change the outcome.

    Pure classification, so it lives here instead of in ``registration_policy``.
    That is what lets the transport layer (``http_client``) consult it without
    a top-level import back edge into the policy layer.
    """
    return any(marker in error_text(value) for marker in TERMINAL_ERROR_MARKERS)
