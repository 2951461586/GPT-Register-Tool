"""External dependencies of the auth_flow package, re-exported in one place.

Submodules reach these through ``deps`` (``from . import deps``) rather than
importing each name directly. A test then patches exactly one attribute --
``sms_tool.auth_flow.deps.request_with_retry`` -- and every caller sees it. A
direct ``from ..http_client import request_with_retry`` in a submodule would
bind the function at import time and make the patch silently ineffective (the
same defect ``docs/CONTEXT.md`` records for the payment-capability seam).
"""
from __future__ import annotations

from ..accounts.account_creation import _validate_email_otp
from ..auth_headers import auth_impersonate, nextauth_headers, openai_auth_headers
from ..auth_state import fetch_client_auth_session_dump as _fetch_client_auth_session_dump
from ..config import current_config_data
from ..http_client import _retry_after_seconds, request_with_retry
from ..http_utils import _absolute_url, _cookie_presence, _follow_continue_url, _json_or_raw
from ..mailbox import _poll_email_otp
from ..phone_proxy import redact_proxy_url
from ..registration_concurrency import mark_registration_rate_limited

__all__ = [
    "_absolute_url",
    "_cookie_presence",
    "_fetch_client_auth_session_dump",
    "_follow_continue_url",
    "_json_or_raw",
    "_poll_email_otp",
    "_retry_after_seconds",
    "_validate_email_otp",
    "auth_impersonate",
    "current_config_data",
    "mark_registration_rate_limited",
    "nextauth_headers",
    "openai_auth_headers",
    "redact_proxy_url",
    "request_with_retry",
]
