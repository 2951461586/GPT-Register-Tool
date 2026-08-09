"""Shared HTTP/auth helper utilities for OpenAI auth protocol flows.

Centralizes helpers that were previously duplicated across auth_flow,
account_creation, account_2fa, codex_oauth, session_refresh, and auth_state:

  * _json_or_raw          – safely parse JSON or return truncated raw text
  * _absolute_url         – resolve relative URLs against a base
  * _follow_continue_url  – GET a continue URL with impersonation
  * _cookie_header        – serialize session cookies to a header string
  * _minimal_chatgpt_cookie_header  – keep only essential ChatGPT cookies
  * _validate_email_otp   – POST OTP code to validate endpoint(s)
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from .config import CFG
from .auth_headers import auth_impersonate
from .http_client import request_with_retry


# ── response parsing ──────────────────────────────────────────────────────────


def _json_or_raw(response, limit: int = 500):
    """Parse response as JSON; on failure return dict with truncated raw text."""
    try:
        return response.json()
    except Exception:
        return {"_raw": getattr(response, "text", "")[:limit]}


# ── URL helpers ───────────────────────────────────────────────────────────────


def _absolute_url(base_url: str, url: str) -> str:
    """Resolve *url* against *base_url* if it is not already absolute."""
    if not url:
        return ""
    if str(url).startswith(("http://", "https://")):
        return str(url)
    return base_url.rstrip("/") + "/" + str(url).lstrip("/")


# ── continue-URL navigation ───────────────────────────────────────────────────


def _follow_continue_url(session, url, base_headers, referer="", label="continue"):
    """GET a continue URL (resolved against auth_base) with impersonation."""
    if not url:
        return None
    full_url = _absolute_url(
        CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com"), url
    )
    headers = {**base_headers, "Accept": "text/html,application/xhtml+xml"}
    if referer:
        headers["Referer"] = referer
    response = request_with_retry(
        session, "get", full_url, label=label,
        headers=headers, impersonate=auth_impersonate(),
    )
    print(f"  {label}: {response.status_code} {response.url}")
    return response


# ── cookie helpers ────────────────────────────────────────────────────────────


def _minimal_chatgpt_cookie_header(cookie_header) -> str:
    """Return a minimal cookie header with only essential ChatGPT cookies."""
    keep = {
        "__Host-next-auth.csrf-token",
        "__Secure-next-auth.callback-url",
        "__Secure-next-auth.session-token",
    }
    output = []
    for item in str(cookie_header or "").split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name in keep and value:
            output.append(f"{name}={value}")
    return "; ".join(output)


def _cookie_header(session) -> str:
    """Serialize session cookies to a header string, keeping only ChatGPT essentials."""
    cookies = getattr(session, "cookies", None)
    if not cookies:
        return ""
    if hasattr(cookies, "get_dict"):
        items = cookies.get_dict().items()
    else:
        items = [(cookie.name, cookie.value) for cookie in cookies]
    return _minimal_chatgpt_cookie_header(
        "; ".join(f"{name}={value}" for name, value in items)
    )


# ── email OTP validation ──────────────────────────────────────────────────────


def _validate_email_otp(
    session,
    auth_base: str,
    base_headers: dict,
    code: str,
    sentinel_data=None,
    use_sentinel: bool = True,
):
    """Validate an email OTP code against the auth endpoint(s).

    Returns ``(ok: bool, body: dict)``.  Tries the primary endpoint first,
    then falls back to alternative endpoints if the primary returns 404/405.
    """
    primary_endpoint = "/api/accounts/email-otp/validate"
    fallback_endpoints = [
        "/api/accounts/email-verification/validate",
        "/api/accounts/email-verification/verify",
        "/api/accounts/verify-email",
    ]
    payload = {"code": code}
    validate_headers = dict(base_headers)
    validate_headers["Content-Type"] = "application/json"
    validate_headers["Origin"] = auth_base
    validate_headers["Referer"] = f"{auth_base}/email-verification"

    endpoints = [primary_endpoint] + fallback_endpoints
    last_error: dict = {}
    for endpoint in endpoints:
        url = _absolute_url(auth_base, endpoint)
        r = request_with_retry(
            session, "post", url, label=f"Email OTP validate {endpoint}",
            json=payload, headers=validate_headers, impersonate=auth_impersonate(),
        )
        body = _json_or_raw(r)
        if r.status_code == 200:
            print(f"  Email OTP validate: {endpoint} {r.status_code}")
            return True, body
        if r.status_code not in (404, 405):
            last_error = {"endpoint": endpoint, "status": r.status_code, "body": body}
            print(
                f"  Email OTP validate failed: {endpoint} {r.status_code} "
                f"{json.dumps(body, ensure_ascii=False, default=str)[:200]}"
            )
            break
        last_error = {"endpoint": endpoint, "status": r.status_code, "body": body}
    return False, last_error
