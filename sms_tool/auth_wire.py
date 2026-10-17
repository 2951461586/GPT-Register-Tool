"""Pure auth wire-shape contract: URL predicates, payload predicates, headers.

Why this module exists
----------------------
``sms_tool/auth_flow/`` is the OTP/login flow package. Application-layer modules
(``otp_strategy``, ``phone_registration``) needed a handful of *pure* helpers
from it -- "is this URL the login landing page", "is this error payload an
invalid-state", "append a query parameter", "build auth headers" -- and importing
them through the package facade made ``sms_tool -> sms_tool/auth_flow`` a
minority-direction import edge that the layering ratchet freezes.

None of those helpers touch flow state: they parse a URL, read a dict, or
assemble a header mapping. They belong to the wire vocabulary, not to the flow,
so they live here and both sides import inward:

    application (registration, otp_strategy, phone_registration)
        -> auth_wire          (this module: pure, no flow state)
        -> auth_flow          (the flow itself)

``auth_flow/steps.py`` re-exports every name below, so
``sms_tool.auth_flow.steps._response_next_url``-style patch targets and the
package-level re-exports keep working unchanged. Nothing here imports
``auth_flow``: that would recreate the edge this module exists to remove.

``_auth_request_headers`` reads ``openai_auth_headers`` from
:mod:`sms_tool.auth_headers` directly. ``auth_flow.deps`` re-exports the same
function object, and no test patches ``deps.openai_auth_headers`` (the injection
seam for this helper is the ``RegistrationOperations`` dataclass field), so the
binding is equivalent.
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

from .auth_headers import openai_auth_headers

#: Subject fragment that identifies a login (not signup) OTP mail.
LOGIN_EMAIL_OTP_SUBJECT_KEYWORD = "login code"


def _is_existing_login_redirect(url) -> bool:
    """True when ``url`` is the "you already have an account" login page."""
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if host and host != "auth.openai.com" and not host.endswith(".auth.openai.com"):
        return False
    path = (parsed.path or url or "").lower()
    if not path:
        return False
    # Normalize: strip trailing slashes
    path = path.rstrip("/")
    return path in {"/log-in", "/login"} or path.startswith("/log-in/") or path.startswith("/login/")


def _with_query_param(url, key, value):
    """Append ``key=value`` to ``url`` unless the key is already present."""
    if not value or f"{key}=" in (url or ""):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{key}={quote(str(value), safe='')}"


def _invalid_state_auth_response(data) -> bool:
    """True when an authorize payload reports the session is no longer valid."""
    if not isinstance(data, dict):
        return False
    _raw_error: object = data.get("error")
    error: dict = _raw_error if isinstance(_raw_error, dict) else {}
    code = str(error.get("code") or "").strip().lower()
    message = str(error.get("message") or "").strip().lower()
    return code == "invalid_state" or "session is no longer valid" in message


def _auth_request_headers(
    base_headers, did="", referer="", origin="", sentinel_token="", sentinel_so_token="", extra=None
):
    """Merge the canonical OpenAI auth headers over ``base_headers``."""
    return {
        **(base_headers or {}),
        **openai_auth_headers(
            did,
            referer=referer,
            origin=origin,
            sentinel_token=sentinel_token,
            sentinel_so_token=sentinel_so_token,
            extra=extra or {},
        ),
    }


__all__ = [
    "LOGIN_EMAIL_OTP_SUBJECT_KEYWORD",
    "_auth_request_headers",
    "_invalid_state_auth_response",
    "_is_existing_login_redirect",
    "_with_query_param",
]
