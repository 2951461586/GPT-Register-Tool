"""Shared HTTP/auth helper utilities for OpenAI auth protocol flows.

Centralizes helpers that were previously duplicated across auth_flow,
account_creation, account_2fa, codex_oauth, session_refresh, and auth_state:

  * _json_or_raw          – safely parse JSON or return truncated raw text
  * _absolute_url         – resolve relative URLs against a base
  * _follow_continue_url  – GET a continue URL with impersonation
  * _cookie_header        – serialize session cookies to a header string
  * _minimal_chatgpt_cookie_header  – keep only essential ChatGPT cookies
  * _cookie_presence      – which auth-relevant cookies a session currently holds
  * _validate_email_otp   – POST OTP code to validate endpoint(s)
"""

from __future__ import annotations

import base64
import importlib
import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from ..config import CFG
from .auth_headers import auth_impersonate
from .http_client import request_with_retry
from ..observability.operator_output import emit
from . import endpoints

_LOGGER = logging.getLogger(__name__)


def _load_native_http_utils() -> Any | None:
    mode = str(os.environ.get("RUST_ANYTHING_MODE") or "").strip().lower()
    unit_mode = str(os.environ.get("RUST_ANYTHING_UNIT_HTTP_UTILS") or "").strip().lower()
    if mode in {"old", "original"} or unit_mode in {"old", "original"}:
        return None
    pkg_dir = Path(__file__).resolve().parents[2] / "rust" / "http-utils-rs" / "python"
    if pkg_dir.is_dir():
        pkg_str = str(pkg_dir)
        if pkg_str not in sys.path:
            sys.path.insert(0, pkg_str)
    try:
        return importlib.import_module("http_utils_rs")
    except (ImportError, OSError):
        if mode in {"new", "rust"} or unit_mode in {"new", "rust"}:
            raise
        return None


_NATIVE_HTTP_UTILS = _load_native_http_utils()
_NATIVE_IS_ALLOWED_CALLBACK_URL = getattr(_NATIVE_HTTP_UTILS, "_is_allowed_callback_url", None)
_NATIVE_MINIMAL_CHATGPT_COOKIE_HEADER = getattr(_NATIVE_HTTP_UTILS, "_minimal_chatgpt_cookie_header", None)
_NATIVE_SCALAR_TYPES = (str, type(None), bool, int, float)
_SESSION_ID_RE = re.compile(r"\bus_[A-Za-z0-9]{16,}\b")
_WORKSPACE_FIELD_RE = re.compile(r'"(?:workspace_id|workspaceId)"\s*:\s*"([^"\s]+)"')
_WORKSPACES_FIRST_RE = re.compile(r'"workspaces"\s*:\s*\[\s*\{\s*"id"\s*:\s*"([^"\s]+)"')
_WORKSPACES_STREAM_RE = re.compile(r'workspaces".{0,1600}?"id","([0-9a-fA-F-]{36})"', re.DOTALL)


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


def _is_allowed_callback_url(url: str, auth_base: str, chat_base: str) -> bool:
    """Allow callback hops only to the configured auth/chat hosts without CRLF or userinfo."""
    if (
        _NATIVE_IS_ALLOWED_CALLBACK_URL is not None
        and isinstance(url, _NATIVE_SCALAR_TYPES)
        and isinstance(auth_base, str)
        and isinstance(chat_base, str)
    ):
        return bool(_NATIVE_IS_ALLOWED_CALLBACK_URL(url, auth_base, chat_base))
    text = str(url or "").strip()
    if not text or any(ch in text for ch in ("\r", "\n", "\\")):
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    allowed_hosts = {host.lower() for base in (auth_base, chat_base) for host in [urlparse(base).hostname] if host}
    if parsed.scheme not in {"http", "https"}:
        return False
    if (parsed.hostname or "").lower() not in allowed_hosts:
        return False
    if parsed.username or parsed.password:
        return False
    return parsed.port in (None, 80, 443)


def _response_header(response, name: str) -> str:
    headers = getattr(response, "headers", None)
    if not headers:
        return ""
    for key in (name, name.lower(), name.title()):
        value = headers.get(key) if hasattr(headers, "get") else None
        if value:
            return str(value).strip()
    return ""


def _next_url_from_step_response(response, referer: str, auth_base: str) -> str:
    location = _response_header(response, "location")
    if location:
        return urljoin(referer, location)
    data = _json_or_raw(response, limit=400)
    if not isinstance(data, dict):
        return ""
    raw_page: object = data.get("page")
    page: dict[str, object] = raw_page if isinstance(raw_page, dict) else {}
    raw_payload: object = page.get("payload")
    payload: dict[str, object] = raw_payload if isinstance(raw_payload, dict) else {}
    for source in (data, page, payload):
        for key in ("continue_url", "external_url", "url"):
            candidate = str(source.get(key) or "").strip()
            if candidate:
                return urljoin(auth_base.rstrip("/") + "/", candidate)
    return ""


def _extract_workspace_id(session, html: str, current_url: str) -> str:
    """Extract a workspace id from auth cookies, query parameters, or SSR HTML."""
    cookies = getattr(session, "cookies", None)
    raw_values: list[str] = []
    if cookies is not None:
        if hasattr(cookies, "get_dict"):
            raw = cookies.get_dict().get("oai-client-auth-session")
            if raw:
                raw_values.append(str(raw))
        else:
            for item in cookies:
                if getattr(item, "name", "") == "oai-client-auth-session" and getattr(item, "value", ""):
                    raw_values.append(str(item.value))
    for raw in raw_values:
        for segment in raw.split(".")[:2]:
            try:
                decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
                payload = json.loads(decoded.decode("utf-8"))
            except (ValueError, UnicodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("workspace_id"):
                return str(payload["workspace_id"])
            workspaces = payload.get("workspaces")
            if isinstance(workspaces, list):
                for entry in workspaces:
                    if isinstance(entry, dict) and entry.get("id"):
                        return str(entry["id"])
    parsed = urlparse(current_url)
    if parsed.path.rstrip("/").startswith("/workspace"):
        query = parse_qs(parsed.query)
        for key in ("workspace_id", "id"):
            values = query.get(key)
            if values and values[0]:
                return str(values[0]).strip()
    clean = str(html or "").replace('\\"', '"')
    for pattern in (_WORKSPACE_FIELD_RE, _WORKSPACES_FIRST_RE, _WORKSPACES_STREAM_RE):
        match = pattern.search(clean)
        if match:
            return match.group(1)
    return ""


def _select_callback_account(session, auth_base: str, current_url: str, html: str, base_headers: dict) -> str:
    """Resolve ``/choose-an-account`` only when the page exposes a single session id."""
    ids = set(_SESSION_ID_RE.findall(html or ""))
    if len(ids) != 1:
        emit(_LOGGER, "  Callback choose-an-account skipped: found %s session id(s)", len(ids))
        return ""
    session_id = ids.pop()
    headers = {
        **base_headers,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": auth_base,
        "Referer": current_url,
    }
    response = request_with_retry(
        session,
        "post",
        _absolute_url(auth_base, "/api/accounts/session/select"),
        label="Callback session select",
        json={"session_id": session_id},
        headers=headers,
        impersonate=auth_impersonate(),
    )
    if getattr(response, "status_code", 0) in (404, 405):
        form_headers = {
            **base_headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/html;q=0.9",
            "Origin": auth_base,
            "Referer": current_url,
        }
        response = request_with_retry(
            session,
            "post",
            _absolute_url(auth_base, "/choose-an-account"),
            label="Callback choose-an-account form",
            data=urlencode({"intent": "select", "session_id": session_id}),
            headers=form_headers,
            impersonate=auth_impersonate(),
        )
    if getattr(response, "status_code", 0) not in (200, 201, 302, 303):
        emit(_LOGGER, "  Callback session select failed: HTTP %s", getattr(response, "status_code", 0))
        return ""
    return _next_url_from_step_response(response, current_url, auth_base)


def _select_callback_workspace(session, auth_base: str, current_url: str, html: str, base_headers: dict) -> str:
    """Resolve ``/workspace`` or ``/consent`` interstitials in the callback chain."""
    workspace_id = _extract_workspace_id(session, html, current_url)
    if not workspace_id:
        emit(_LOGGER, "  Callback workspace select skipped: workspace_id not found")
        return ""
    headers = {
        **base_headers,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": auth_base,
        "Referer": current_url,
    }
    response = request_with_retry(
        session,
        "post",
        _absolute_url(auth_base, "/api/accounts/workspace/select"),
        label="Callback workspace select",
        json={"workspace_id": workspace_id},
        headers=headers,
        impersonate=auth_impersonate(),
    )
    if getattr(response, "status_code", 0) not in (200, 201, 302, 303):
        emit(_LOGGER, "  Callback workspace select failed: HTTP %s", getattr(response, "status_code", 0))
        return ""
    return _next_url_from_step_response(response, current_url, auth_base)


def _is_workspace_interstitial_path(path: str) -> bool:
    return path in ("/workspace", "/consent") or path.startswith(("/workspace/", "/consent/", "/sign-in-with-chatgpt/"))


def _resolve_auth_callback_stops(
    session,
    response,
    base_headers: dict,
    *,
    auth_base: str,
    chat_base: str,
    label: str,
    max_hops: int = 6,
):
    """Advance ``/choose-an-account`` and ``/workspace`` stops encountered during continue GETs."""
    auth_host = (urlparse(auth_base).hostname or "").lower()
    visited: set[str] = set()
    current_resp = response
    for _ in range(max_hops):
        if getattr(current_resp, "status_code", 0) != 200:
            break
        current_url = str(getattr(current_resp, "url", "") or "").strip()
        parsed = urlparse(current_url)
        if not auth_host or (parsed.hostname or "").lower() != auth_host:
            break
        path = parsed.path.rstrip("/") or "/"
        is_account_choice = path == "/choose-an-account"
        is_workspace_step = _is_workspace_interstitial_path(path)
        if not (is_account_choice or is_workspace_step):
            break
        if current_url in visited:
            emit(_LOGGER, "  %s stopped: repeated callback interstitial %s", label, path)
            break
        visited.add(current_url)
        html = str(getattr(current_resp, "text", "") or "")
        next_url = (
            _select_callback_account(session, auth_base, current_url, html, base_headers)
            if is_account_choice
            else _select_callback_workspace(session, auth_base, current_url, html, base_headers)
        )
        if not next_url or not _is_allowed_callback_url(next_url, auth_base, chat_base):
            break
        hop_headers = {**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": current_url}
        current_resp = request_with_retry(
            session,
            "get",
            next_url,
            label=f"{label} callback hop",
            headers=hop_headers,
            impersonate=auth_impersonate(),
        )
        emit(_LOGGER, "  %s callback hop: %s %s", label, current_resp.status_code, current_resp.url)
    return current_resp


def _follow_continue_url(session, url, base_headers, referer="", label="continue"):
    """GET a continue URL (resolved against auth_base) with impersonation."""
    if not url:
        return None
    chat_cfg = CFG.get("chatgpt") if isinstance(CFG, Mapping) else {}
    auth_base = (chat_cfg or {}).get("auth_base_url", endpoints.AUTH_BASE)
    chat_base = (chat_cfg or {}).get("chat_base_url", endpoints.CHATGPT_BASE)
    full_url = _absolute_url(auth_base, url)
    headers = {**base_headers, "Accept": "text/html,application/xhtml+xml"}
    if referer:
        headers["Referer"] = referer
    response = request_with_retry(
        session,
        "get",
        full_url,
        label=label,
        headers=headers,
        impersonate=auth_impersonate(),
    )
    # ``emit``, not ``print``: this line is a registered A/B mechanism marker
    # (P1-6 ``About you page prime``), and a bare ``print`` only reaches the
    # stdout mirror (``backend_stdout.jsonl``) while the runbook's ``collect``
    # reads ``sms_tool.log``.  ``emit`` feeds both channels from one call.
    emit(_LOGGER, "  %s: %s %s", label, response.status_code, response.url)
    return _resolve_auth_callback_stops(
        session,
        response,
        base_headers,
        auth_base=auth_base,
        chat_base=chat_base,
        label=label,
    )


# ── cookie helpers ────────────────────────────────────────────────────────────


def _minimal_chatgpt_cookie_header(cookie_header) -> str:
    """Return a minimal cookie header with only essential ChatGPT cookies."""
    if _NATIVE_MINIMAL_CHATGPT_COOKIE_HEADER is not None and isinstance(cookie_header, _NATIVE_SCALAR_TYPES):
        return str(_NATIVE_MINIMAL_CHATGPT_COOKIE_HEADER(cookie_header))
    keep = {
        "__Host-next-auth.csrf-token",
        "__Secure-next-auth.callback-url",
        "__Secure-next-auth.session-token",
    }
    chunked_prefixes = (
        "__Host-next-auth.csrf-token.",
        "__Secure-next-auth.callback-url.",
        "__Secure-next-auth.session-token.",
    )
    output = []
    for item in str(cookie_header or "").split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if (name in keep or name.startswith(chunked_prefixes)) and value:
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
    return _minimal_chatgpt_cookie_header("; ".join(f"{name}={value}" for name, value in items))


def _cookie_presence(session):
    """Which auth-relevant cookies the session currently holds (flags, no values).

    Moved here from ``auth_flow`` on 2026-09-12: ``account_creation`` needs it too,
    and ``auth_flow`` imports ``account_creation`` at module level, so calling it
    from there required a delayed import -- which tripped the delayed-import
    ratchet.  This module already hosts ``_cookie_header`` (the sibling utility)
    and is imported by both sides, so the cycle disappears.

    🔴 ``nextauth_state`` and ``nextauth_session`` are **not** interchangeable.
    ``next-auth.state`` is the OAuth *state* cookie; the cookie that actually
    authenticates ``GET /api/auth/session`` is the next-auth **session token**
    (``__Secure-next-auth.session-token``, chunked as ``.0/.1`` when large, or
    unprefixed in dev).  Until 2026-09-12 only the former was reported, so a
    "session callback returned 200 yet /api/auth/session is anonymous" failure
    had no readout that could distinguish "cookie never landed" from "server
    has not propagated it yet".
    """
    names = set()
    try:
        names = {str(getattr(cookie, "name", cookie) or "") for cookie in session.cookies}
    except Exception:
        try:
            names = set(session.cookies.get_dict())
        except Exception:
            names = set()
    names = {name for name in names if name}
    return {
        "oai_did": any(name.lower() == "oai-did" for name in names),
        "oai_login_csrf": any("oai-login-csrf" in name.lower() for name in names),
        "login_session": any("login_session" in name.lower() for name in names),
        "client_auth_session": any("client_auth_session" in name.lower() for name in names),
        "nextauth_state": any("next-auth.state" in name.lower() for name in names),
        "nextauth_session": any("next-auth.session-token" in name.lower() for name in names),
        "cookie_count": len(names),
    }


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
            session,
            "post",
            url,
            label=f"Email OTP validate {endpoint}",
            json=payload,
            headers=validate_headers,
            impersonate=auth_impersonate(),
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
