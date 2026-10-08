"""Read-only "is this mailbox new to OpenAI?" probe.

Answers whether an address is new or already registered, using the same
``signin -> authorize`` handshake the registration lane uses. It stops at the
authorize landing: it never posts ``authorize/continue``, never registers a
password, never sends an OTP and never creates an account, so it does not change
account state.

Decisive cases
--------------
* auth landing ``/create-account[/password]`` -> ``unregistered``
* auth landing ``/log-in[...]`` or page type ``login_password`` / ``mfa_challenge``
  -> ``registered``

Limitations (reported, not guessed)
-----------------------------------
* An ``/email-verification`` landing is only decisive once the follow-up
  ``authorize/continue`` names the verification mode, and that step may dispatch
  an OTP email. This probe therefore returns ``unknown`` there instead of
  spending a code. ``verification_mode`` is still surfaced when the authorize
  response already carried it.
* A network/transport failure is ``unknown`` with an ``error`` field: "not
  probed" and "registered" are different answers and are never conflated.

The reference implementation (``gpt-reg-review`` ``registration_probe.py``)
posts the continue step and accepts the possible OTP; this project keeps the
read-only boundary and reports the ambiguous landing instead.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode, urlparse

from . import config as config_module
from . import endpoints
from .auth_flow import deps, password_step, steps

STATUS_REGISTERED = "registered"
STATUS_UNREGISTERED = "unregistered"
STATUS_UNKNOWN = "unknown"

#: Page types that can only belong to an account that already exists.
_REGISTERED_PAGE_TYPES = {"login_password", "mfa_challenge"}
#: The server's own name for the passwordless *signup* branch.
_MODE_PASSWORDLESS_SIGNUP = "passwordless_signup"


def _normalize_email(value: object) -> str:
    return str(value or "").strip().lower()


def classify_registration_landing(
    url: str,
    *,
    page_type: str = "",
    verification_mode: str = "",
) -> tuple[str, str]:
    """Classify an authorize landing into ``(status, reason)``.

    ``page_type`` comes from an authorize JSON body (``page.type`` /
    ``page_type``); ``verification_mode`` from ``page.payload``.
    """
    normalized_page_type = str(page_type or "").strip().lower()
    normalized_mode = str(verification_mode or "").strip().lower()

    if steps._is_signup_password_step(url):
        return STATUS_UNREGISTERED, "create_account_password"
    if normalized_page_type in _REGISTERED_PAGE_TYPES:
        return STATUS_REGISTERED, normalized_page_type
    if steps._is_existing_login_redirect(url) or password_step._is_login_password_step(url):
        return STATUS_REGISTERED, "login_password"
    if steps._is_email_verification_step(url):
        if normalized_mode == _MODE_PASSWORDLESS_SIGNUP:
            return STATUS_UNREGISTERED, "passwordless_signup"
        if normalized_mode:
            return STATUS_REGISTERED, normalized_mode
        return STATUS_UNKNOWN, "email_verification_without_mode"
    if steps._is_chatgpt_auth_login_landing(url):
        return STATUS_UNKNOWN, "chatgpt_auth_login_landing"
    if normalized_page_type:
        return STATUS_UNKNOWN, f"unclassified_page_type:{normalized_page_type}"
    return STATUS_UNKNOWN, "unclassified_landing"


def _seed_device_cookie(session: Any, device_id: str) -> None:
    """Bind ``oai-did`` on the probe session (both ChatGPT and OpenAI domains)."""
    if not device_id:
        return
    for domain in (".openai.com", "chatgpt.com"):
        try:
            session.cookies.set("oai-did", device_id, domain=domain, path="/")
        except Exception:
            continue


def _result(email: str, status: str, **extra: object) -> dict:
    payload = {"email": email, "status": status, "checked_at": time.time()}
    payload.update(extra)
    return payload


def _extract_page_signals(response: object) -> tuple[str, str]:
    """Best-effort ``(page_type, verification_mode)`` from an authorize response."""
    try:
        payload = deps._json_or_raw(response, limit=4000)
    except Exception:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    page_type = password_step._login_password_page_type(payload)
    page = payload.get("page")
    page_payload = page.get("payload") if isinstance(page, dict) else {}
    payload_map = page_payload if isinstance(page_payload, dict) else {}
    mode = str(payload_map.get("email_verification_mode") or "").strip()
    return page_type, mode


def _run_read_only_handshake(
    email: str,
    *,
    session: object,
    device_id: str,
    session_logging_id: str,
    chat_base: str,
    auth_base: str,
) -> dict:
    """Drive prime -> csrf -> signin -> authorize and return the landing."""
    base_headers = deps.openai_auth_headers(
        device_id,
        referer=f"{chat_base}/",
        origin=chat_base,
        session_id=session_logging_id,
    )
    deps.request_with_retry(
        session,
        "get",
        f"{auth_base}/create-account",
        label="Probe prime",
        headers={**base_headers, "Accept": "text/html,application/xhtml+xml"},
        impersonate=deps.auth_impersonate(),
    )
    csrf_response = deps.request_with_retry(
        session,
        "get",
        f"{chat_base}/api/auth/csrf",
        label="Probe csrf",
        headers=deps.nextauth_headers(
            device_id, session_id=session_logging_id, referer=f"{chat_base}/", origin=chat_base
        ),
        impersonate=deps.auth_impersonate(),
    )
    csrf_body = deps._json_or_raw(csrf_response, limit=1000)
    csrf_token = str((csrf_body or {}).get("csrfToken") or "").strip()
    if not csrf_token:
        return {"error": "csrf_missing"}

    attempts = steps._signup_signin_attempts() or [{}]
    attempt = attempts[0]
    screen_hint = str(attempt.get("screen_hint") or "")
    prompt = str(attempt.get("prompt") or "")
    locale = str(attempt.get("locale") or "")
    signin_url = steps._openai_signin_url(
        chat_base, device_id, session_logging_id, email, screen_hint=screen_hint, prompt=prompt, locale=locale
    )
    signin_response = deps.request_with_retry(
        session,
        "post",
        signin_url,
        label="Probe signin",
        data=urlencode({"csrfToken": csrf_token, "callbackUrl": f"{chat_base}/", "json": "true"}),
        headers={
            **base_headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": chat_base,
            "Referer": f"{chat_base}/",
        },
        impersonate=deps.auth_impersonate(),
    )
    signin_body = deps._json_or_raw(signin_response, limit=1000)
    auth_url = str(
        (signin_body or {}).get("url")
        or getattr(signin_response, "headers", {}).get("location")
        or getattr(signin_response, "url", "")
        or ""
    )
    auth_url = steps._ensure_authorize_context(
        auth_url, device_id, session_logging_id, email, screen_hint=screen_hint, prompt=prompt, locale=locale
    )
    if not auth_url:
        return {"error": "auth_session_url_missing"}

    authorize_response = deps.request_with_retry(
        session,
        "get",
        auth_url,
        label="Probe authorize",
        headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": f"{chat_base}/"},
        allow_redirects=True,
        impersonate=deps.auth_impersonate(),
    )
    current_url = str(getattr(authorize_response, "url", "") or "")
    location = str(
        getattr(authorize_response, "headers", {}).get("location")
        or getattr(authorize_response, "headers", {}).get("Location")
        or ""
    )
    if location and urlparse(current_url).path.rstrip("/") in {"", "/api/accounts/authorize"}:
        current_url = deps._absolute_url(auth_base, location)
    page_type, verification_mode = _extract_page_signals(authorize_response)
    return {"url": current_url, "page_type": page_type, "verification_mode": verification_mode}


def probe_registration(
    email: str,
    *,
    proxy: str = "",
    session: Any | None = None,
    device_id: str = "",
    config: object | None = None,
) -> dict:
    """Return ``registered`` / ``unregistered`` / ``unknown`` for one address.

    Read-only: stops at the authorize landing. Accepts an optional ``session``
    so a caller (or test) can supply its own transport; otherwise one is built
    for ``proxy``.
    """
    address = _normalize_email(email)
    if not address:
        return _result("", STATUS_UNKNOWN, error="empty_email")

    # ``Mapping``, not ``dict``: the default branch returns
    # ``current_config_data()``, whose tree is frozen into ``mappingproxy``
    # (``config._freeze``) -- a ``dict`` check silently dropped the configured
    # ``chatgpt.auth_base_url`` / ``chat_base_url`` and used the hard-coded
    # endpoints instead (2026-10-07 P1-D class).
    merged = config if isinstance(config, Mapping) else config_module.current_config_data()
    chatgpt_cfg = merged.get("chatgpt") if isinstance(merged, Mapping) else {}
    chatgpt_cfg = chatgpt_cfg if isinstance(chatgpt_cfg, Mapping) else {}
    chat_base = str(chatgpt_cfg.get("chat_base_url") or endpoints.CHATGPT_BASE).rstrip("/")
    auth_base = str(chatgpt_cfg.get("auth_base_url") or endpoints.AUTH_BASE).rstrip("/")

    resolved_device_id = str(device_id or "").strip() or str(uuid.uuid4())
    session_logging_id = str(uuid.uuid4())
    owned_session = session is None
    active_session = session
    started = time.perf_counter()
    try:
        if active_session is None:
            from .registration_handlers import _new_registration_session

            active_session = _new_registration_session(proxy)
        _seed_device_cookie(active_session, resolved_device_id)
        landing = _run_read_only_handshake(
            address,
            session=active_session,
            device_id=resolved_device_id,
            session_logging_id=session_logging_id,
            chat_base=chat_base,
            auth_base=auth_base,
        )
        if landing.get("error"):
            return _result(
                address,
                STATUS_UNKNOWN,
                error=str(landing["error"]),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        status, reason = classify_registration_landing(
            str(landing.get("url") or ""),
            page_type=str(landing.get("page_type") or ""),
            verification_mode=str(landing.get("verification_mode") or ""),
        )
        return _result(
            address,
            status,
            reason=reason,
            landing=landing.get("url") or "",
            page_type=landing.get("page_type") or "",
            verification_mode=landing.get("verification_mode") or "",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:
        return _result(
            address,
            STATUS_UNKNOWN,
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    finally:
        if owned_session and active_session is not None:
            try:
                active_session.close()
            except Exception:
                pass


__all__ = [
    "STATUS_REGISTERED",
    "STATUS_UNKNOWN",
    "STATUS_UNREGISTERED",
    "classify_registration_landing",
    "probe_registration",
]
