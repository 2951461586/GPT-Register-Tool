"""Pure OAuth helpers, token persistence, mailbox rehydration, and stage utilities.

Extracted from :mod:`sms_tool.codex.oauth` so stateless PKCE/URL/stage/cookie
parsing, JSON+SQLite token persistence, and protocol step implementations stay
cleanly separated from top-level OAuth login orchestration.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable

from ..kernel.sanitizer import load_native_module, sanitize_text
from ..mailbox.router import (
    MailboxAccount,
    MailboxTokenExpiredError as MailboxTokenExpiredError,
    mailbox_has_inbox_credentials as mailbox_has_inbox_credentials,
    poll_email_otp as poll_email_otp,
)
from ..observability.operator_output import emit as _emit
from ..providers import mailbox_gmail
from ..sentinel.client import (
    EMAIL_OTP_VALIDATE_FLOW,
    SentinelIssueError as SentinelIssueError,
    issue_sentinel_flow as issue_sentinel_flow,
)
from ..storage import upsert_account
from ..transport import endpoints
from ..transport.auth_headers import (
    auth_impersonate,
    openai_auth_headers_lower,
    select_auth_fingerprint as select_auth_fingerprint,
    set_fingerprint_device as set_fingerprint_device,
)
from ..transport.endpoints import AUTH_BASE
from ..transport.http_backend import new_session as new_session
from ..transport.http_client import request_with_retry as request_with_retry
from ..transport.http_utils import (
    absolute_url,
    create_account_continue_url as _create_account_continue_url,
    is_user_already_exists as _is_user_already_exists,
)
from ..transport.tls_fingerprint import apply_to_session as apply_to_session
from ..utils import (
    atomic_write_text,
    random_birthdate as random_birthdate,
    random_name as random_name,
)

_LOGGER = logging.getLogger(__name__)

AUTH_URL = endpoints.AUTH_BASE + "/oauth/authorize"
TOKEN_URL = endpoints.AUTH_BASE + "/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"
LOGIN_EMAIL_OTP_SUBJECT_KEYWORD = "login code"

_SEND_OTP_MODE_ENUM = frozenset({"passwordless_signup", "passwordless_login", "login_challenge"})

_NATIVE_CODEX_OAUTH_STAGE = load_native_module(
    "codex-oauth-stage-rs",
    "codex_oauth_stage_rs",
    "RUST_ANYTHING_UNIT_CODEX_OAUTH_STAGE",
)
_NATIVE_DETECT_PROTOCOL_STAGE = getattr(_NATIVE_CODEX_OAUTH_STAGE, "_detect_protocol_stage", None)


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _iso_utc(epoch_seconds):
    return (
        datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _oai_headers(did, extra=None):
    return openai_auth_headers_lower(did, extra=extra)


def _next_url(response):
    try:
        body = response.json()
    except (ValueError, TypeError, AttributeError):
        body = {}
    return absolute_url(
        endpoints.AUTH_BASE, body.get("continue_url") or response.headers.get("Location") or response.url
    )


def _needs_email_otp(url):
    value = str(url or "").lower()
    return "email-verification" in value or "email-otp" in value


def _needs_password(url):
    value = str(url or "").lower()
    return "/log-in/password" in value or "/login/password" in value or value.endswith("/password")


def _needs_phone_verification(url):
    value = str(url or "").lower()
    return "/add-phone" in value or "phone-verification" in value


def _has_callback_code(url):
    text = str(url or "")
    return "code=" in text and "state=" in text


def _detect_protocol_stage(url):
    if _NATIVE_DETECT_PROTOCOL_STAGE is not None and (url is None or isinstance(url, str)):
        return _NATIVE_DETECT_PROTOCOL_STAGE(url)
    value = str(url or "")
    lower = value.lower()
    if _has_callback_code(value):
        return "callback"
    if _needs_phone_verification(lower):
        return "add_phone"
    if _needs_email_otp(lower):
        return "email_otp"
    if _needs_password(lower):
        return "password"
    if lower.endswith("/consent") or lower.endswith("/workspace"):
        return "consent"
    if "/authorize" in lower or "/oauth/" in lower:
        return "email"
    return "unknown"


def _codex_oauth_protocol_ready_stage(stage):
    return stage in {"callback", "consent"}


def _is_account_deactivated_response(status_code, text):
    body = str(text or "").lower()
    return int(status_code or 0) in (403, 404) and (
        "account_deactivated" in body
        or "deleted or deactivated" in body
        or "account has been deleted" in body
        or "account has been deactivated" in body
    )


def _cookie_value(session, name) -> str:
    cookies = getattr(session, "cookies", None)
    getter = getattr(cookies, "get", None)
    if not callable(getter):
        return ""
    try:
        return str(getter(name) or "")
    except (KeyError, ValueError, RuntimeError):
        return ""


def parse_workspace_from_auth_cookie(auth_cookie, *, jwt_segment_fn: Callable[[str], dict]):
    if not auth_cookie or "." not in auth_cookie:
        return []
    parts = auth_cookie.split(".")
    for segment in parts[1:2] + parts[:1]:
        claims = jwt_segment_fn(segment)
        workspaces = claims.get("workspaces") or []
        if isinstance(workspaces, list) and workspaces:
            return workspaces
    return []


def _is_terminal_account_data(data):
    if not isinstance(data, dict):
        return False
    _raw_terminal: object = data.get("terminal_failure")
    terminal: dict = _raw_terminal if isinstance(_raw_terminal, dict) else {}
    text = " ".join(
        str(value or "")
        for value in (
            data.get("status"),
            data.get("error"),
            data.get("account_scan_status"),
            terminal.get("code"),
            terminal.get("reason"),
        )
    ).lower()
    return any(
        marker in text
        for marker in (
            "account_deactivated",
            "account_deatived",
            "deleted or deactivated",
            "account has been deleted",
            "account has been deactivated",
        )
    )


def _describe_send_otp_answer(response):
    """One log line about what the send answered; never echoes server strings."""
    try:
        body = response.json()
    except (ValueError, TypeError, AttributeError):
        body = {}
    _raw_state: object = body.get("oai-client-auth-session") if isinstance(body, dict) else None
    state = _raw_state if isinstance(_raw_state, dict) else {}
    pending = state.get("passwordless_email_otp_send_pending")
    mode = str(state.get("email_verification_mode") or "")
    return "pending=%s mode=%s" % (
        str(pending).lower() if isinstance(pending, bool) else "unknown",
        mode if mode in _SEND_OTP_MODE_ENUM else ("unknown" if not mode else "other"),
    )


def build_browser_style_send_headers(did, sentinel_token, *, attach_sentinel_fn):
    headers: dict[str, Any] = _oai_headers(
        did,
        {
            "Referer": f"{AUTH_BASE}/email-verification",
            "Origin": AUTH_BASE,
            "Accept": "*/*",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "x-access-flow-invocation-id": str(uuid.uuid4()),
            "x-openai-document-navigation-id": str(uuid.uuid4()),
        },
    )
    attach_sentinel_fn(headers, {"sentinel_token": sentinel_token})
    headers.pop("content-type", None)
    headers["Content-Type"] = None
    return headers


def _new_oauth_request():
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return {
        "state": state,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
        "auth_url": f"{AUTH_URL}?{urllib.parse.urlencode(params)}",
    }


def _write_oauth_account_state(data, json_path, *, upsert, write_text=atomic_write_text):
    """Report both local writes independently; neither can roll back the other."""
    session_saved = not bool(json_path)
    if json_path:
        try:
            write_text(json_path, json.dumps(data, ensure_ascii=False, indent=2))
            session_saved = True
        except (OSError, TypeError, ValueError):
            session_saved = False
    try:
        account_saved = bool(upsert(data, json_path=json_path))
    except Exception:
        account_saved = False
    outcome: dict[str, Any] = {
        "session_saved": session_saved,
        "account_saved": account_saved,
        "persisted": session_saved and account_saved,
    }
    if not session_saved:
        outcome["error_code"] = "session_write_failed"
    elif not account_saved:
        outcome["error_code"] = "account_write_failed"
    return outcome


def save_oauth_tokens_impl(
    data,
    json_path,
    tokens,
    email,
    mode,
    result=None,
    *,
    upsert=upsert_account,
    write_text=atomic_write_text,
):
    now = int(time.time())
    expires_in = _as_int(tokens.get("expires_in")) or 0
    refreshed = dict(data)
    refreshed["email"] = email
    refreshed["success"] = True
    refreshed["access_token"] = tokens.get("access_token", "")
    refreshed["id_token"] = tokens.get("id_token", "")
    refreshed["oauth_refresh_token"] = tokens.get("refresh_token", "")
    refreshed["refresh_token_status"] = "oauth_present"
    refreshed["refresh_token_updated_at"] = now
    refreshed["refreshed_at"] = now
    refreshed.pop("error", None)
    if str(refreshed.get("status") or "").lower() == "failed":
        refreshed.pop("status", None)
    refreshed["codex_oauth"] = {
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "mode": mode,
        "updated_at": now,
    }
    _raw_result: object = result
    result_map: dict = _raw_result if isinstance(_raw_result, dict) else {}
    _raw_phone_attempt: object = result_map.get("phone_attempt")
    phone_attempt: dict = _raw_phone_attempt if isinstance(_raw_phone_attempt, dict) else {}
    if phone_attempt:
        refreshed["phone"] = phone_attempt.get("phone", refreshed.get("phone", ""))
        _raw_response: object = refreshed.get("response")
        response: dict = _raw_response if isinstance(_raw_response, dict) else {}
        response["phone_verification"] = phone_attempt
        response["codex_oauth"] = {
            "ok": True,
            "mode": mode,
            "has_access_token": bool(tokens.get("access_token")),
            "has_refresh_token": bool(tokens.get("refresh_token")),
            "phone_verified": bool(phone_attempt.get("ok")),
        }
        refreshed["response"] = response
    if expires_in:
        refreshed["oauth_expires_at"] = _iso_utc(now + expires_in)
    persistence = _write_oauth_account_state(refreshed, json_path, upsert=upsert, write_text=write_text)
    output = {
        "ok": True,
        "mode": mode,
        "email": email,
        "json_path": json_path,
        "refresh_token_status": "oauth_present",
        "persisted": persistence["persisted"],
        "persistence": persistence,
    }
    if phone_attempt:
        output["phone"] = phone_attempt.get("phone", "")
        output["phone_attempt"] = phone_attempt
    return output


def persist_one_click_sms_failure(
    data,
    json_path,
    email,
    result,
    *,
    upsert=upsert_account,
    write_text=atomic_write_text,
):
    """Record a failed OAuth/SMS attempt without changing its remote outcome."""
    refreshed = dict(data or {})
    refreshed["email"] = email
    refreshed["success"] = bool(refreshed.get("access_token"))
    refreshed["error"] = sanitize_text(result.get("error") or "one_click_sms_failed")[:200]
    refreshed["refresh_token_status"] = str(refreshed.get("refresh_token_status") or "no_rt")
    refreshed["refresh_token_updated_at"] = int(time.time())
    _raw_response: object = refreshed.get("response")
    response: dict = _raw_response if isinstance(_raw_response, dict) else {}
    public_result = {
        key: value for key, value in result.items() if key in {"ok", "mode", "status", "phone_verification_required"}
    }
    public_result["error"] = refreshed["error"]
    _raw_tokens: object = result.get("tokens")
    tokens: dict = _raw_tokens if isinstance(_raw_tokens, dict) else {}
    if tokens:
        public_result["has_access_token"] = bool(tokens.get("access_token"))
        public_result["has_refresh_token"] = bool(tokens.get("refresh_token"))
    _raw_phone_attempt: object = result.get("phone_attempt")
    phone_attempt: dict = _raw_phone_attempt if isinstance(_raw_phone_attempt, dict) else {}
    if phone_attempt:
        public_phone = {
            key: value
            for key, value in phone_attempt.items()
            if key in {"ok", "phone", "provider", "activation_id", "reuse_count", "max_reuse_count", "remaining"}
        }
        if phone_attempt.get("error"):
            public_phone["error"] = sanitize_text(phone_attempt["error"])[:200]
        public_result["phone_attempt"] = public_phone
        refreshed["phone"] = public_phone.get("phone", refreshed.get("phone", ""))
        response["phone_verification"] = public_phone
    response["codex_oauth"] = public_result
    refreshed["response"] = response

    return _write_oauth_account_state(refreshed, json_path, upsert=upsert, write_text=write_text)


@lru_cache(maxsize=1)
def _configured_mailbox_index():
    """Load the configured mailbox pool once per recovery process."""
    try:
        from ..mailbox.router import load_mailbox_pool

        return {
            str(getattr(candidate, "email", "") or "").strip().lower(): candidate
            for candidate in load_mailbox_pool()
            if str(getattr(candidate, "email", "") or "").strip()
        }
    except Exception:
        return {}


def _mailbox_from_configured_pool(email):
    """Return a matching configured mailbox without copying credentials into data."""
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    return _configured_mailbox_index().get(normalized)


def build_mailbox_from_data(data, *, has_credentials_fn, configured_pool_fn):
    _raw_mailbox: object = data.get("mailbox")
    mailbox: dict = _raw_mailbox if isinstance(_raw_mailbox, dict) else {}
    email = str(mailbox.get("email") or data.get("email") or "").strip()
    refresh_token = str(mailbox.get("refresh_token") or data.get("mailbox_refresh_token") or "").strip()
    provider = str(mailbox.get("provider") or data.get("mailbox_provider") or "").strip()
    source = str(mailbox.get("source") or data.get("mailbox_source") or "").strip()
    mailbox_token = str(mailbox.get("token") or data.get("mailbox_token") or "").strip()
    if not email:
        return None
    mailbox_password = mailbox.get("password") if "password" in mailbox else data.get("password")
    result = MailboxAccount(
        email=email,
        password=str(mailbox_password or "").strip(),
        login_password=str(mailbox.get("login_password") or "").strip(),
        refresh_token=refresh_token,
        access_token=str(mailbox.get("access_token") or "").strip(),
        token=mailbox_token,
        client_secret=str(mailbox.get("client_secret") or "").strip(),
        auth_mode=str(mailbox.get("auth_mode") or "").strip(),
        sender_name=str(mailbox.get("sender_name") or "").strip(),
        source=source,
        provider=provider,
        order_no=str(mailbox.get("order_no") or "").strip(),
        purchase_id=str(mailbox.get("purchase_id") or "").strip(),
        project_name=str(mailbox.get("project_name") or "").strip(),
        price=str(mailbox.get("price") or "").strip(),
        purchase_total_cost=str(mailbox.get("purchase_total_cost") or "").strip(),
        balance_after=str(mailbox.get("balance_after") or "").strip(),
    )
    from ..mailbox.quarantine import is_mailbox_quarantined

    if is_mailbox_quarantined(result):
        return None
    if not has_credentials_fn(result):
        if provider == "icloud_url" or source.lower() in {
            "icloud_url",
            "icloud",
            "token_file",
            "mailbox_file",
        }:
            fallback = configured_pool_fn(email)
            if fallback is not None and has_credentials_fn(fallback):
                return fallback
        if mailbox_gmail.is_gmail_mailbox(result):
            from ..mailbox.router import mailbox_from_config

            fallback = mailbox_from_config(argparse.Namespace(email=email))
            if fallback is not None and has_credentials_fn(fallback):
                return fallback
        return None
    return result


def exchange_callback_impl(callback_url, oauth, proxy, *, new_session_fn, request_fn):
    parsed = urllib.parse.urlparse(callback_url)
    query = urllib.parse.parse_qs(parsed.query)
    code = (query.get("code") or [""])[0]
    state = (query.get("state") or [""])[0]
    if not code:
        raise RuntimeError("oauth_callback_missing_code")
    if state != oauth["state"]:
        raise RuntimeError("oauth_state_mismatch")
    session = new_session_fn(proxy=proxy)
    try:
        response = request_fn(
            session,
            "post",
            TOKEN_URL,
            label="OAuth token exchange",
            attempts=5,
            retry_delay=5,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": oauth["redirect_uri"],
                "code_verifier": oauth["code_verifier"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            impersonate=auth_impersonate(),
        )
    finally:
        close_fn = getattr(session, "close", None)
        if callable(close_fn):
            close_fn()
    if response.status_code != 200:
        raise RuntimeError(f"oauth_token_exchange_failed:{response.status_code}:{response.text[:300]}")
    body = response.json()
    if not body.get("access_token") or not body.get("refresh_token"):
        raise RuntimeError("oauth_token_response_missing_access_or_refresh_token")
    return body


def follow_redirects_impl(session, start_url, max_redirects, *, request_fn):
    current_url = absolute_url(endpoints.AUTH_BASE, start_url)
    response = None
    for _ in range(max_redirects):
        if not current_url:
            return response, current_url
        response = request_fn(
            session,
            "get",
            current_url,
            label="OAuth redirect",
            attempts=5,
            retry_delay=5,
            allow_redirects=False,
            impersonate=auth_impersonate(),
        )
        if response.status_code not in (301, 302, 303, 307, 308):
            return response, current_url
        location = response.headers.get("Location", "")
        if not location:
            return response, current_url
        current_url = urllib.parse.urljoin(current_url, location)
        if _has_callback_code(current_url):
            return response, current_url
    return response, current_url


def complete_about_you_impl(
    session,
    did,
    current_url,
    proxy,
    *,
    random_name_fn,
    random_birthdate_fn,
    issue_sentinel_fn,
    request_fn,
    with_sentinel_fn,
    follow_redirects_fn,
    safe_url_fn,
):
    first, last = random_name_fn()
    full_name = f"{first} {last}".strip()
    birthdate = random_birthdate_fn()
    try:
        sentinel = issue_sentinel_fn(
            flow="oauth_create_account",
            device_id=did,
            session=session,
            proxy=proxy,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"about_you_sentinel_failed:{exc}",
            "last_url": safe_url_fn(current_url),
        }

    response = request_fn(
        session,
        "post",
        endpoints.AUTH_BASE + "/api/accounts/create_account",
        label="Complete about-you",
        json={"name": full_name, "birthdate": birthdate},
        headers=with_sentinel_fn(
            _oai_headers(did, {"Referer": endpoints.AUTH_BASE + "/about-you", "content-type": "application/json"}),
            {"sentinel_token": sentinel.token, "sentinel_so_token": sentinel.so_token},
        ),
        impersonate=auth_impersonate(),
    )
    try:
        body = response.json()
    except (ValueError, TypeError):
        body = {"_raw": str(getattr(response, "text", "") or "")[:300]}
    _emit(_LOGGER, f"  About-you: {response.status_code}")
    detail = {
        "status_code": int(getattr(response, "status_code", 0) or 0),
        "last_url": safe_url_fn(current_url),
    }
    if _is_user_already_exists(body):
        _raw_error: object = body.get("error")
        error: dict = _raw_error if isinstance(_raw_error, dict) else {}
        recovery = error.get("userAlreadyExistsRecovery")
        action = str((recovery or {}).get("action") or "") if isinstance(recovery, dict) else ""
        return {
            **detail,
            "ok": False,
            "error": "about_you_existing_account_user_already_exists" + (f":{action}" if action else ""),
            "recovery_action": action,
            "redirect_uri": _create_account_continue_url(body),
        }
    next_url = _create_account_continue_url(body)
    if not next_url:
        raw = body.get("_raw") if isinstance(body, dict) else None
        digest = {"raw": str(raw)} if raw else {"keys": sorted(body)[:12] if isinstance(body, dict) else []}
        return {
            **detail,
            "ok": False,
            "error": "about_you_no_continue_url",
            "body_digest": digest,
        }
    _, landed = follow_redirects_fn(session, next_url, proxy=proxy)
    return {**detail, "ok": True, "url": landed or next_url, "continued": True}


def finish_phone_authorization_locked_impl(
    session,
    oauth,
    did,
    current_url,
    proxy,
    phone_pool,
    *,
    complete_phone_fn,
    auto_phone_fn,
    follow_redirects_fn,
    exchange_callback_fn,
    select_workspace_if_needed_fn,
    safe_url_fn,
):
    phone_result = complete_phone_fn(
        session,
        did,
        current_url,
        proxy=proxy,
        enabled=bool(phone_pool) or auto_phone_fn(),
        phone_pool=phone_pool,
    )
    if phone_result.get("ok"):
        current_url = phone_result.get("url") or phone_result.get("next_url") or current_url
        try:
            _, current_url = follow_redirects_fn(session, current_url, proxy=proxy)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"phone_verified_oauth_redirect_failed:{exc}",
                "last_url": safe_url_fn(current_url),
                "phone_attempt": phone_result,
            }
        if _has_callback_code(current_url):
            try:
                tokens = exchange_callback_fn(current_url, oauth, proxy=proxy)
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"phone_verified_oauth_token_exchange_failed:{exc}",
                    "last_url": safe_url_fn(current_url),
                    "phone_attempt": phone_result,
                }
            return {"ok": True, "tokens": tokens, "phone_attempt": phone_result}
        workspace_result = select_workspace_if_needed_fn(session, did, current_url, proxy=proxy)
        if workspace_result.get("ok"):
            current_url = workspace_result.get("url", current_url)
            if _has_callback_code(current_url):
                try:
                    tokens = exchange_callback_fn(current_url, oauth, proxy=proxy)
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": f"phone_verified_oauth_token_exchange_failed:{exc}",
                        "last_url": safe_url_fn(current_url),
                        "phone_attempt": phone_result,
                    }
                return {"ok": True, "tokens": tokens, "phone_attempt": phone_result}
    return {"ok": False, "last_url": safe_url_fn(current_url), "phone_attempt": phone_result}


def finish_authorization_impl(
    session,
    oauth,
    did,
    current_url,
    proxy,
    phone_pool,
    phone_probe_only,
    *,
    exchange_callback_fn,
    select_workspace_if_needed_fn,
    finish_phone_locked_fn,
    safe_url_fn,
):
    if _has_callback_code(current_url):
        return {"ok": True, "tokens": exchange_callback_fn(current_url, oauth, proxy=proxy)}

    workspace_result = select_workspace_if_needed_fn(session, did, current_url, proxy=proxy)
    if workspace_result.get("ok"):
        current_url = workspace_result.get("url", current_url)
        if _has_callback_code(current_url):
            return {"ok": True, "tokens": exchange_callback_fn(current_url, oauth, proxy=proxy)}

    if _needs_phone_verification(current_url):
        if phone_probe_only:
            return {
                "ok": False,
                "error": "add_phone_required",
                "phone_verification_required": True,
                "last_url": safe_url_fn(current_url),
                "phone_attempt": {
                    "ok": False,
                    "error": "add_phone_required",
                    "message": "OAuth scan detected phone verification; SMS sending is disabled in probe mode.",
                },
            }
        if phone_pool:
            _emit(_LOGGER, "[*] Waiting for single-phone OAuth lane...")
            with phone_pool.lock:
                return finish_phone_locked_fn(session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool)
        return finish_phone_locked_fn(session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool)

    return {"ok": False, "last_url": safe_url_fn(current_url)}


def complete_email_otp_impl(
    session,
    data,
    did,
    current_url,
    proxy,
    timeout,
    *,
    mailbox_from_data_fn,
    poll_email_otp_fn,
    codex_sentinel_fn,
    with_sentinel_fn,
    next_url_fn,
):
    mailbox = mailbox_from_data_fn(data)
    if mailbox is None:
        return {"ok": False, "mode": "codex_oauth_pkce", "error": "email_otp_required_missing_mailbox"}
    issued_after = int(time.time())
    with contextlib.suppress(Exception):
        session.post(
            endpoints.AUTH_BASE + "/api/accounts/email-otp/send",
            headers=_oai_headers(did, {"Referer": current_url, "content-type": "application/json"}),
            json={},
            timeout=30,
            impersonate=auth_impersonate(),
        )
    code = poll_email_otp_fn(
        mailbox,
        subject_keyword=LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
        timeout=min(max(int(timeout or 180), 30), 300),
        issued_after_unix=issued_after,
        proxy=proxy,
    )
    if not code:
        return {"ok": False, "mode": "codex_oauth_pkce", "error": "email_otp_poll_timeout"}
    validate = session.post(
        endpoints.AUTH_BASE + "/api/accounts/email-otp/validate",
        headers=with_sentinel_fn(
            _oai_headers(
                did, {"Referer": endpoints.AUTH_BASE + "/email-verification", "content-type": "application/json"}
            ),
            codex_sentinel_fn(session, did, flow=EMAIL_OTP_VALIDATE_FLOW, proxy=proxy),
        ),
        json={"code": code},
        timeout=30,
        impersonate=auth_impersonate(),
    )
    if validate.status_code != 200:
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": f"email_otp_validate_failed:{validate.status_code}",
            "body": validate.text[:300],
        }
    return {"ok": True, "next_url": next_url_fn(validate)}


def select_workspace_impl(
    session,
    did,
    referer,
    workspace_id,
    proxy,
    timeout,
    extra_headers,
    *,
    codex_sentinel_fn,
    with_sentinel_fn,
    next_url_fn,
):
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return {"ok": False, "error": "missing_workspace_id"}
    headers = with_sentinel_fn(
        _oai_headers(
            did, {"Referer": referer or endpoints.AUTH_BASE + "/workspace", "content-type": "application/json"}
        ),
        codex_sentinel_fn(session, did, flow="authorize_continue", proxy=proxy),
    )
    if isinstance(extra_headers, dict):
        headers.update(extra_headers)
    response = session.post(
        endpoints.AUTH_BASE + "/api/accounts/workspace/select",
        headers=headers,
        json={"workspace_id": workspace_id},
        timeout=timeout,
        impersonate=auth_impersonate(),
    )
    if response.status_code != 200:
        return {
            "ok": False,
            "workspace_id": workspace_id,
            "status": response.status_code,
            "body": (response.text or "")[:300],
            "error": f"workspace_select_failed:{response.status_code}",
        }
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "status": response.status_code,
        "next_url": next_url_fn(response),
    }


def select_workspace_if_needed_impl(
    session,
    did,
    current_url,
    proxy,
    *,
    parse_workspace_fn,
    select_workspace_fn,
    follow_redirects_fn,
):
    if not current_url or not (current_url.endswith("/consent") or current_url.endswith("/workspace")):
        return {"ok": False}
    workspaces = parse_workspace_fn(_cookie_value(session, "oai-client-auth-session"))
    if not workspaces:
        return {"ok": False}
    workspace_id = ""
    for item in workspaces:
        title = str(item.get("title") or item.get("name") or "")
        if item.get("is_personal") or "Personal" in title:
            workspace_id = str(item.get("id") or "")
            break
    workspace_id = workspace_id or str((workspaces[0] or {}).get("id") or "")
    if not workspace_id:
        return {"ok": False}
    selected = select_workspace_fn(session, did, current_url, workspace_id, proxy=proxy)
    if not selected.get("ok"):
        return selected
    _, final_url = follow_redirects_fn(session, selected.get("next_url") or current_url, proxy=proxy)
    return {"ok": True, "url": final_url, "workspace_id": workspace_id}


def send_passwordless_otp_impl(
    session,
    did,
    current_url,
    proxy,
    *,
    browser_style_enabled_fn,
    load_cached_sentinel_fn,
    issue_sentinel_fn,
    browser_style_headers_fn,
    with_sentinel_fn,
):
    browser_style = browser_style_enabled_fn()
    sentinel = load_cached_sentinel_fn()
    browser_headers = None
    if browser_style:
        _emit(_LOGGER, "[*] Passwordless browser-style send-otp")
        try:
            issued = issue_sentinel_fn(flow="authorize_continue", device_id=did, session=session, proxy=proxy)
        except Exception as exc:
            return {"ok": False, "hard_error": True, "error": f"passwordless_send_sentinel_failed:{exc}"}
        browser_headers = browser_style_headers_fn(did, issued.token)
    for endpoint in (
        endpoints.AUTH_BASE + "/api/accounts/passwordless/send-otp",
        endpoints.AUTH_BASE + "/api/accounts/email-otp/send",
    ):
        try:
            if browser_style:
                response = session.post(
                    endpoint,
                    headers=browser_headers,
                    timeout=30,
                    impersonate=auth_impersonate(),
                )
            else:
                response = session.post(
                    endpoint,
                    headers=with_sentinel_fn(
                        _oai_headers(did, {"Referer": current_url, "content-type": "application/json"}),
                        sentinel,
                    ),
                    json={},
                    timeout=30,
                    impersonate=auth_impersonate(),
                )
            if response.status_code == 200:
                _emit(
                    _LOGGER,
                    f"[*] Passwordless OTP send accepted: {endpoint.rsplit('/', 1)[-1]} "
                    f"{_describe_send_otp_answer(response)}",
                )
                return {"ok": True, "endpoint": endpoint}
            _emit(_LOGGER, f"[*] Passwordless OTP send skipped: {endpoint.rsplit('/', 1)[-1]} {response.status_code}")
            if response.status_code == 409:
                _emit(_LOGGER, "[*] Passwordless OTP send already pending; continuing to mailbox polling")
                return {"ok": True, "endpoint": endpoint, "status_code": response.status_code}
            if response.status_code not in (400, 404, 405):
                return {
                    "ok": False,
                    "hard_error": True,
                    "error": f"passwordless_send_failed:{response.status_code}",
                    "body": response.text[:300],
                }
        except Exception as exc:
            return {"ok": False, "hard_error": True, "error": f"passwordless_send_error:{exc}"}
    return {"ok": False, "error": "passwordless_send_unavailable"}


def resend_email_otp_impl(session, did, proxy, *, codex_sentinel_fn, with_sentinel_fn):
    sentinel = codex_sentinel_fn(session, did, flow="authorize_continue", proxy=proxy)
    try:
        response = session.post(
            endpoints.AUTH_BASE + "/api/accounts/email-otp/resend",
            headers=with_sentinel_fn(
                _oai_headers(
                    did, {"Referer": endpoints.AUTH_BASE + "/email-verification", "content-type": "application/json"}
                ),
                sentinel,
            ),
            json={},
            timeout=20,
            impersonate=auth_impersonate(),
        )
        _emit(_LOGGER, f"[*] Email OTP resend: {response.status_code}")
        return {"ok": response.status_code in (200, 409), "status_code": response.status_code}
    except Exception:
        return {"ok": False, "status_code": 0}


def resolve_post_otp_validate_redirect_impl(
    session,
    oauth,
    did,
    current_url: str,
    *,
    reason: str,
    proxy=None,
    phone_pool=None,
    phone_probe_only: bool = False,
    complete_about_you_fn,
    finish_authorization_fn,
    safe_url_fn,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    about_attempt = None
    if current_url.endswith("/about-you"):
        about_attempt = complete_about_you_fn(session, did, current_url, proxy=proxy)
        if about_attempt.get("ok"):
            current_url = about_attempt.get("url") or current_url
        elif about_attempt.get("error"):
            return (
                {
                    "ok": False,
                    "mode": "codex_oauth_pkce",
                    "error": about_attempt.get("error") or "passwordless_about_you_required",
                    "fallback_from": reason,
                    "last_url": safe_url_fn(current_url),
                    "about_you": about_attempt,
                },
                about_attempt,
                current_url,
            )
    final = finish_authorization_fn(
        session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool, phone_probe_only=phone_probe_only
    )
    if final.get("ok"):
        final["login_method"] = "passwordless_email_otp"
        final["fallback_from"] = reason
        if about_attempt and about_attempt.get("ok"):
            final["completed_about_you"] = True
        return final, about_attempt, current_url
    if final.get("phone_attempt"):
        phone_error = (final.get("phone_attempt") or {}).get("error", "phone_verification_failed")
        return (
            {
                "ok": False,
                "mode": "codex_oauth_pkce",
                "error": phone_error,
                "fallback_from": reason,
                "last_url": final.get("last_url") or safe_url_fn(current_url),
                "phone_attempt": final.get("phone_attempt"),
            },
            about_attempt,
            current_url,
        )
    if current_url.endswith("/about-you"):
        failure: dict[str, Any] = {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": "passwordless_about_you_required",
            "fallback_from": reason,
            "last_url": safe_url_fn(current_url),
        }
        if about_attempt:
            failure["about_you"] = about_attempt
        return failure, about_attempt, current_url
    return None, about_attempt, current_url
