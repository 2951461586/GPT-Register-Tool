import base64
import contextlib
import json
import logging
import secrets
import time
import urllib.parse
from typing import Any

from ..config import CFG
from ..kernel.native_loader import load_native_module
from ..sentinel.runner import SentinelRunnerError
from .oauth_helpers import (
    AUTH_BASE,
    AUTH_URL,
    CLIENT_ID as CLIENT_ID,
    EMAIL_OTP_VALIDATE_FLOW,
    LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
    MailboxAccount as MailboxAccount,
    MailboxTokenExpiredError,
    REDIRECT_URI as REDIRECT_URI,
    SCOPE as SCOPE,
    SentinelIssueError,
    TOKEN_URL as TOKEN_URL,
    _SEND_OTP_MODE_ENUM as _SEND_OTP_MODE_ENUM,
    _as_int as _as_int,
    _b64url as _b64url,
    _codex_oauth_protocol_ready_stage as _codex_oauth_protocol_ready_stage,
    _configured_mailbox_index as _configured_mailbox_index,
    _cookie_value as _cookie_value,
    _describe_send_otp_answer as _describe_send_otp_answer,
    _detect_protocol_stage as _detect_protocol_stage,
    _emit,
    _has_callback_code,
    _is_account_deactivated_response as _is_account_deactivated_response,
    _is_terminal_account_data as _is_terminal_account_data,
    _iso_utc as _iso_utc,
    _mailbox_from_configured_pool,
    _needs_email_otp as _needs_email_otp,
    _needs_password as _needs_password,
    _needs_phone_verification as _needs_phone_verification,
    _new_oauth_request as _new_oauth_request,
    _next_url as _next_url,
    _oai_headers,
    _write_oauth_account_state as _write_oauth_account_state,
    apply_to_session as _apply_tls_fingerprint,
    atomic_write_text,
    auth_impersonate,
    build_browser_style_send_headers as _build_browser_style_send_headers,
    build_mailbox_from_data as _build_mailbox_from_data,
    complete_about_you_impl as _complete_about_you_impl,
    complete_email_otp_impl as _complete_email_otp_impl,
    endpoints,
    exchange_callback_impl as _exchange_callback_impl,
    finish_authorization_impl as _finish_authorization_impl,
    finish_phone_authorization_locked_impl as _finish_phone_authorization_locked_impl,
    follow_redirects_impl as _follow_redirects_impl,
    issue_sentinel_flow,
    mailbox_has_inbox_credentials,
    new_session as _new_protocol_session,
    parse_workspace_from_auth_cookie as _parse_workspace_from_auth_cookie_impl,
    persist_one_click_sms_failure as _persist_one_click_sms_failure_impl,
    poll_email_otp,
    random_birthdate,
    random_name,
    request_with_retry,
    resend_email_otp_impl as _resend_email_otp_impl,
    resolve_post_otp_validate_redirect_impl as _resolve_post_otp_validate_redirect_impl,
    save_oauth_tokens_impl as _save_oauth_tokens_impl,
    select_auth_fingerprint,
    select_workspace_if_needed_impl as _select_workspace_if_needed_impl,
    select_workspace_impl as _select_workspace_impl,
    send_passwordless_otp_impl as _send_passwordless_otp_impl,
    set_fingerprint_device,
    upsert_account,
)
from .phone import complete_phone_verification
from .sentinel import (
    attach_sentinel,
    import_cached_auth_cookies,
    import_cookie_header,
    load_cached_sentinel,
    with_sentinel,
)

_LOGGER = logging.getLogger(__name__)

_NATIVE_CODEX_OAUTH_JWT = load_native_module(
    "codex-oauth-jwt-rs", "codex_oauth_jwt_rs", "RUST_ANYTHING_UNIT_CODEX_OAUTH_JWT"
)
_NATIVE_JWT_SEGMENT = getattr(_NATIVE_CODEX_OAUTH_JWT, "_jwt_segment", None)
_NATIVE_SAFE_URL = getattr(_NATIVE_CODEX_OAUTH_JWT, "_safe_url", None)


def _new_codex_session(proxy=None, device_id=""):
    """Build a protocol-backend-aware session with per-account TLS permutation."""
    did = str(device_id or "").strip()
    if did:
        set_fingerprint_device(did)
    session = _new_protocol_session(proxy=str(proxy or ""))
    _apply_tls_fingerprint(session, CFG)
    return session


def _codex_sentinel(session, did, flow="authorize_continue", proxy=None):
    """Mint a fresh per-flow Sentinel pair, falling back to cache on runner error."""
    try:
        issued = issue_sentinel_flow(flow=flow, device_id=did, session=session, proxy=proxy)
        token = str(getattr(issued, "token", "") or "").strip()
        so_token = str(getattr(issued, "so_token", "") or "").strip()
        if token:
            return {"sentinel_token": token, "sentinel_so_token": so_token}
    except (SentinelIssueError, SentinelRunnerError, RuntimeError, OSError, ValueError) as exc:
        _LOGGER.debug("codex sentinel mint failed for %s (%s); falling back to cached sentinel", flow, exc)
    return load_cached_sentinel()


def refresh_codex_oauth_session(
    data,
    json_path="",
    proxy=None,
    timeout=180,
    force_email_otp_login=False,
    phone_pool=None,
    phone_probe_only=False,
    persist=True,
):
    if _is_terminal_account_data(data):
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": "account_deactivated",
            "terminal": True,
            "skipped": True,
        }
    result = collect_codex_oauth_tokens(
        data=data,
        proxy=proxy,
        timeout=timeout,
        force_email_otp_login=force_email_otp_login,
        phone_pool=phone_pool,
        phone_probe_only=phone_probe_only,
    )
    if not result.get("ok"):
        return result
    if not persist:
        return result
    return _save_oauth_tokens(
        data,
        json_path,
        result["tokens"],
        str(data.get("email") or "").strip().lower(),
        "codex_oauth_pkce",
        result=result,
    )


def collect_codex_oauth_tokens(
    data,
    session=None,
    proxy=None,
    timeout=180,
    force_email_otp_login=False,
    phone_pool=None,
    phone_probe_only=False,
    browser_headless: bool | None = None,
):
    select_auth_fingerprint(rotate=True)
    email = str(data.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "mode": "codex_oauth_pkce", "error": "missing_email"}
    oauth = _new_oauth_request()
    if session is None:
        session = _new_codex_session(proxy=proxy, device_id=data.get("device_id", ""))
        import_cookie_header(session, data.get("cookie_header", ""), "chatgpt.com")
        import_cached_auth_cookies(session)
    elif proxy:
        session.proxies = {"http": proxy, "https": proxy}

    try:
        _, current_url = _follow_redirects(session, oauth["auth_url"], proxy=proxy)
        if _has_callback_code(current_url):
            tokens = _exchange_callback(current_url, oauth, proxy=proxy)
            return {
                "ok": True,
                "mode": "codex_oauth_pkce",
                # Which of the two paths ran -- see the note on the login
                # branch below.
                "login_stage": "live_session",
                "tokens": tokens,
            }

        result = _login_and_exchange(
            session=session,
            oauth=oauth,
            email=email,
            data=data,
            current_url=current_url,
            proxy=proxy,
            timeout=timeout,
            force_email_otp_login=force_email_otp_login,
            phone_pool=phone_pool,
            phone_probe_only=phone_probe_only,
            browser_headless=browser_headless,
        )
        result.setdefault("mode", "codex_oauth_pkce")
        # ``login_stage`` is the one field that tells the two costs apart.
        # ``mode`` cannot: it is ``codex_oauth_pkce`` on every branch, so a
        # summary carrying only it could not say whether this run reused the
        # live session (free) or walked the login stage machine (pays an email
        # code).  The opt-in ``registration.obtain_refresh_token`` needs exactly
        # that distinction to be measurable -- otherwise "the switch is on and
        # every account still burned a code" is indistinguishable from "the
        # switch is on and the short circuit fired".  Recorded on failures too,
        # because walking the stages and *then* failing is the worst case: the
        # code was spent for nothing.
        result.setdefault("login_stage", "login_required")
        return result
    except Exception as exc:
        return {"ok": False, "mode": "codex_oauth_pkce", "error": str(exc)}


def _login_and_exchange(
    session,
    oauth,
    email,
    data,
    current_url,
    proxy=None,
    timeout=180,
    force_email_otp_login=False,
    phone_pool=None,
    phone_probe_only=False,
    browser_headless: bool | None = None,
):
    max_restarts = 2
    for restart_attempt in range(max_restarts + 1):
        did = str(data.get("device_id") or "").strip() or _cookie_value(session, "oai-did") or secrets.token_hex(16)
        set_fingerprint_device(did)
        with contextlib.suppress(Exception):
            session.cookies.set("oai-did", did, domain="auth.openai.com", path="/")
        sentinel = _codex_sentinel(session, did, flow="authorize_continue", proxy=proxy)
        headers = _oai_headers(did, {"Referer": current_url or AUTH_URL, "content-type": "application/json"})
        attach_sentinel(headers, sentinel)
        start_resp = session.post(
            endpoints.AUTH_BASE + "/api/accounts/authorize/continue",
            headers=headers,
            json={"username": {"value": email, "kind": "email"}},
            timeout=30,
            impersonate=auth_impersonate(),
            allow_redirects=False,
        )
        if start_resp.status_code != 200:
            body = start_resp.text[:300]
            if start_resp.status_code == 409 and "invalid_state" in body and restart_attempt < max_restarts:
                _emit(_LOGGER, f"[*] OAuth state invalid, restarting ({restart_attempt + 1}/{max_restarts})")
                with contextlib.suppress(Exception):
                    session.cookies.clear(domain="auth.openai.com")
                oauth = _new_oauth_request()
                _, current_url = _follow_redirects(session, oauth["auth_url"], proxy=proxy)
                if _has_callback_code(current_url):
                    return {"ok": True, "tokens": _exchange_callback(current_url, oauth, proxy=proxy)}
                continue
            return {
                "ok": False,
                "mode": "codex_oauth_pkce",
                "error": f"authorize_continue_failed:{start_resp.status_code}",
                "body": body,
            }
        next_url = _next_url(start_resp)
        _, current_url = _follow_redirects(session, next_url, proxy=proxy)
        if _has_callback_code(current_url):
            return {"ok": True, "tokens": _exchange_callback(current_url, oauth, proxy=proxy)}

        result = _run_protocol_login_stages(
            session=session,
            oauth=oauth,
            email=email,
            data=data,
            did=did,
            current_url=current_url,
            proxy=proxy,
            timeout=timeout,
            force_email_otp_login=force_email_otp_login,
            phone_pool=phone_pool,
            phone_probe_only=phone_probe_only,
            browser_headless=browser_headless,
        )
        if not result.get("needs_session_restart") or restart_attempt >= max_restarts:
            return result
        _emit(_LOGGER, f"[*] Session state invalid, restarting ({restart_attempt + 1}/{max_restarts})")
        with contextlib.suppress(Exception):
            session.cookies.clear(domain="auth.openai.com")
        oauth = _new_oauth_request()
        _, current_url = _follow_redirects(session, oauth["auth_url"], proxy=proxy)
        if _has_callback_code(current_url):
            return {"ok": True, "tokens": _exchange_callback(current_url, oauth, proxy=proxy)}
    return result


def _run_protocol_login_stages(
    session,
    oauth,
    email,
    data,
    did,
    current_url,
    proxy=None,
    timeout=180,
    force_email_otp_login=False,
    phone_pool=None,
    phone_probe_only=False,
    browser_headless: bool | None = None,
):
    allow_takeover = bool(force_email_otp_login or _allow_passwordless_takeover())
    stage = _detect_protocol_stage(current_url)
    data.setdefault("codex_oauth_protocol", {})
    data["codex_oauth_protocol"]["stage"] = stage
    _emit(_LOGGER, f"[*] Codex OAuth protocol stage: {stage}")

    if _codex_oauth_protocol_ready_stage(stage):
        final = _finish_authorization(
            session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool, phone_probe_only=phone_probe_only
        )
        if final.get("ok"):
            final.setdefault("protocol_stage", stage)
        return final

    if (
        force_email_otp_login
        and stage == "password"
        and _password_first_relogin_enabled()
        and str(data.get("password") or "").strip()
    ):
        # Mechanism line for the password-first relogin comparison: unconditional
        # inside the toggle's own branch.
        _emit(_LOGGER, "[*] Relogin password-first: trying the saved password before spending an email code")
        first_try = _password_login_and_exchange(
            session=session,
            oauth=oauth,
            data=data,
            did=did,
            current_url=current_url,
            proxy=proxy,
            timeout=timeout,
            phone_pool=phone_pool,
            phone_probe_only=phone_probe_only,
            browser_headless=browser_headless,
        )
        if first_try.get("ok"):
            first_try.setdefault("protocol_stage", "password")
            return first_try
        _emit(
            _LOGGER,
            "[*] Relogin password-first failed (%s); falling back to email OTP",
            first_try.get("error") or "unknown",
        )

    if force_email_otp_login and stage not in {"email_otp", "callback", "consent", "add_phone"}:
        _emit(_LOGGER, f"[*] force_email_otp_login: overriding stage '{stage}' -> 'email_otp'")
        stage = "email_otp"
    elif stage in {"email", "unknown"}:
        stage = "email_otp" if allow_takeover else stage

    if stage == "email_otp":
        email_otp_result = _passwordless_login_and_exchange(
            session=session,
            oauth=oauth,
            data=data,
            did=did,
            current_url=current_url,
            proxy=proxy,
            timeout=timeout,
            reason="email_otp_required" if _needs_email_otp(current_url) else "email_otp_forced",
            phone_pool=phone_pool,
            phone_probe_only=phone_probe_only,
            browser_headless=browser_headless,
        )
        if email_otp_result.get("ok"):
            email_otp_result.setdefault("protocol_stage", "email_otp")
            return email_otp_result
        email_otp_result.setdefault("protocol_stage", "email_otp")
        email_otp_result.setdefault(
            "fallback_from", "email_otp_forced" if force_email_otp_login else "email_otp_required"
        )
        return email_otp_result
    elif stage == "password":
        return _handle_password_stage_login(
            session=session,
            oauth=oauth,
            data=data,
            did=did,
            current_url=current_url,
            proxy=proxy,
            timeout=timeout,
            allow_takeover=allow_takeover,
            phone_pool=phone_pool,
            phone_probe_only=phone_probe_only,
            browser_headless=browser_headless,
        )
    elif stage == "add_phone":
        final = _finish_authorization(
            session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool, phone_probe_only=phone_probe_only
        )
        if final.get("ok"):
            final.setdefault("protocol_stage", "add_phone")
            return final
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": (final.get("phone_attempt") or {}).get("error") or "codex_oauth_add_phone_required",
            "last_url": final.get("last_url") or _safe_url(current_url),
            "phone_attempt": final.get("phone_attempt"),
            "protocol_stage": "add_phone",
        }

    email_otp_result = {
        "ok": False,
        "error": f"codex_oauth_protocol_stage_not_ready:{stage}",
        "last_url": _safe_url(current_url),
        "protocol_stage": stage,
    }

    final = _finish_authorization(
        session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool, phone_probe_only=phone_probe_only
    )
    if final.get("ok"):
        final.setdefault("protocol_stage", stage)
        return final

    return {
        "ok": False,
        "mode": "codex_oauth_pkce",
        "error": email_otp_result.get("error") or "oauth_callback_code_not_reached",
        "last_url": final.get("last_url") or _safe_url(current_url),
        "email_otp_attempt": email_otp_result,
        "phone_attempt": final.get("phone_attempt"),
        "protocol_stage": stage,
    }


def _handle_password_stage_login(
    *,
    session,
    oauth,
    data,
    did,
    current_url,
    proxy,
    timeout,
    allow_takeover: bool,
    phone_pool,
    phone_probe_only,
    browser_headless: bool | None,
):
    password_result = _password_login_and_exchange(
        session=session,
        oauth=oauth,
        data=data,
        did=did,
        current_url=current_url,
        proxy=proxy,
        timeout=timeout,
        phone_pool=phone_pool,
        phone_probe_only=phone_probe_only,
        browser_headless=browser_headless,
    )
    if password_result.get("ok"):
        password_result.setdefault("protocol_stage", "password")
        return password_result
    if not allow_takeover:
        return password_result
    email_otp_result = _passwordless_login_and_exchange(
        session=session,
        oauth=oauth,
        data=data,
        did=did,
        current_url=current_url,
        proxy=proxy,
        timeout=timeout,
        reason="password_login_failed",
        phone_pool=phone_pool,
        phone_probe_only=phone_probe_only,
        browser_headless=browser_headless,
    )
    if email_otp_result.get("ok"):
        email_otp_result.setdefault("protocol_stage", "email_otp")
        return email_otp_result
    email_otp_result.setdefault("protocol_stage", "email_otp")
    email_otp_result.setdefault("fallback_from", "password_login_failed")
    email_otp_result["password_attempt"] = password_result
    return email_otp_result


def _resolve_post_otp_validate_redirect(
    session,
    oauth,
    did,
    current_url: str,
    *,
    reason: str,
    proxy=None,
    phone_pool=None,
    phone_probe_only: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    return _resolve_post_otp_validate_redirect_impl(
        session,
        oauth,
        did,
        current_url,
        reason=reason,
        proxy=proxy,
        phone_pool=phone_pool,
        phone_probe_only=phone_probe_only,
        complete_about_you_fn=_complete_about_you,
        finish_authorization_fn=_finish_authorization,
        safe_url_fn=_safe_url,
    )


def _passwordless_login_and_exchange(
    session,
    oauth,
    data,
    did,
    current_url,
    proxy=None,
    timeout=180,
    reason="",
    phone_pool=None,
    phone_probe_only=False,
    browser_headless: bool | None = None,
):
    mailbox = _mailbox_from_data(data)
    if mailbox is None:
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": "passwordless_missing_mailbox",
            "fallback_from": reason,
            "last_url": _safe_url(current_url),
        }

    issued_after = int(time.time())
    send_result = _send_passwordless_otp(session, did, current_url, proxy=proxy)
    if send_result.get("hard_error"):
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": send_result.get("error", "passwordless_send_failed"),
            "fallback_from": reason,
            "last_url": _safe_url(current_url),
        }

    attempts = max(1, min(int((CFG.get("email_registration") or {}).get("max_otp_retries") or 3), 5))
    excluded_otps = {
        str(value or "").strip()
        for value in (
            data.get("registration_email_otp"),
            *(
                (data.get("excluded_email_otps") or ())
                if isinstance(data.get("excluded_email_otps"), (list, tuple, set))
                else ()
            ),
        )
        if str(value or "").strip()
    }
    last_error = ""
    last_validate_body = ""
    # Bound outside the loop: the ``continue`` paths below (a wrong or expired
    # code) skip the ``/about-you`` handling entirely, and the final return still
    # has to be able to name it.
    about_attempt = None
    for attempt in range(attempts):
        if attempt > 0:
            resend_issued_after = int(time.time())
            resend_result = _resend_email_otp(session, did, current_url)
            if int(resend_result.get("status_code") or 0) == 200:
                issued_after = resend_issued_after
            elif int(resend_result.get("status_code") or 0) == 409:
                _emit(_LOGGER, "[*] Email OTP resend already pending; keeping previous OTP search window")
        try:
            code = poll_email_otp(
                mailbox,
                subject_keyword=LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
                timeout=min(max(int(timeout or 180), 30), 300),
                issued_after_unix=issued_after,
                proxy=proxy,
                excluded_otps=excluded_otps,
            )
        except MailboxTokenExpiredError:
            return {
                "ok": False,
                "mode": "codex_oauth_pkce",
                "error": "mailbox_token_expired",
                "terminal": True,
                "fallback_from": reason,
                "last_url": _safe_url(current_url),
                "message": f"Mailbox refresh token expired for {getattr(mailbox, 'email', '')}. Re-authenticate the mailbox.",
            }
        if not code:
            last_error = "passwordless_email_otp_poll_timeout"
            continue
        validate = session.post(
            endpoints.AUTH_BASE + "/api/accounts/email-otp/validate",
            headers=with_sentinel(
                _oai_headers(
                    did, {"Referer": endpoints.AUTH_BASE + "/email-verification", "content-type": "application/json"}
                ),
                _codex_sentinel(session, did, flow=EMAIL_OTP_VALIDATE_FLOW, proxy=proxy),
            ),
            json={"code": code},
            timeout=30,
            impersonate=auth_impersonate(),
        )
        if validate.status_code != 200:
            excluded_otps.add(str(code))
            last_error = f"email_otp_validate_failed:{validate.status_code}"
            last_validate_body = validate.text[:300]
            _emit(_LOGGER, f"[*] Email OTP validate failed: {validate.status_code} {last_validate_body}")
            if _is_account_deactivated_response(validate.status_code, validate.text):
                return {
                    "ok": False,
                    "mode": "codex_oauth_pkce",
                    "error": "account_deactivated",
                    "terminal": True,
                    "fallback_from": reason,
                    "last_url": _safe_url(current_url),
                    "body": last_validate_body,
                }
            if "invalid_state" in last_validate_body:
                return {
                    "ok": False,
                    "mode": "codex_oauth_pkce",
                    "error": "email_otp_invalid_state",
                    "fallback_from": reason,
                    "last_url": _safe_url(current_url),
                    "body": last_validate_body,
                    "needs_session_restart": True,
                }
            continue
        next_url = _next_url(validate)
        _, current_url = _follow_redirects(session, next_url, proxy=proxy)
        terminal_result, about_attempt, current_url = _resolve_post_otp_validate_redirect(
            session,
            oauth,
            did,
            current_url,
            reason=reason,
            proxy=proxy,
            phone_pool=phone_pool,
            phone_probe_only=phone_probe_only,
        )
        if terminal_result is not None:
            return terminal_result
    return {
        "ok": False,
        "mode": "codex_oauth_pkce",
        "error": last_error or "passwordless_email_otp_failed",
        "fallback_from": reason,
        "last_url": _safe_url(current_url),
        "body": last_validate_body,
        **({"about_you": about_attempt} if about_attempt else {}),
    }


def _password_login_and_exchange(
    session,
    oauth,
    data,
    did,
    current_url,
    proxy=None,
    timeout=180,
    phone_pool=None,
    phone_probe_only=False,
    browser_headless: bool | None = None,
):
    password = str(data.get("password") or "").strip()
    if not password:
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": "password_login_required",
            "last_url": _safe_url(current_url),
            "message": "OpenAI routed this account to password login, but the local session data has no account password.",
        }

    response = session.post(
        endpoints.AUTH_BASE + "/api/accounts/password/verify",
        headers=with_sentinel(
            _oai_headers(did, {"Referer": current_url, "content-type": "application/json"}),
            _codex_sentinel(session, did, flow="authorize_continue", proxy=proxy),
        ),
        json={"password": password},
        timeout=30,
        impersonate=auth_impersonate(),
    )
    if response.status_code != 200:
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": f"password_verify_failed:{response.status_code}",
            "last_url": _safe_url(current_url),
            "body": response.text[:300],
        }

    from ..auth_flow import totp as _auth_totp

    next_url = _next_url(response)
    try:
        verify_body = response.json()
    except Exception:
        verify_body = {}
    if _auth_totp._is_mfa_challenge_payload(verify_body):
        mfa_sentinel = _codex_sentinel(session, did, flow="authorize_continue", proxy=proxy)
        mfa = _auth_totp._complete_existing_login_totp(
            session,
            AUTH_BASE,
            {},
            verify_body,
            did=did,
            sentinel_token=str(mfa_sentinel.get("sentinel_token") or ""),
            totp_secret=str(data.get("totp_secret") or ""),
        )
        if not mfa.get("ok"):
            return {
                "ok": False,
                "mode": "codex_oauth_pkce",
                "error": f"password_login_mfa_failed:{mfa.get('error') or 'unknown'}",
                "last_url": _safe_url(current_url),
            }
        _emit(_LOGGER, "[*] Password login: TOTP challenge completed")
        next_url = _auth_totp._response_next_url_from_data(mfa.get("data"), AUTH_BASE) or next_url
    _, current_url = _follow_redirects(session, next_url, proxy=proxy)
    if _has_callback_code(current_url):
        return {
            "ok": True,
            "tokens": _exchange_callback(current_url, oauth, proxy=proxy),
            "login_method": "password",
        }

    if _needs_email_otp(current_url):
        email_otp_result = _complete_email_otp(session, data, did, current_url, proxy=proxy, timeout=timeout)
        if not email_otp_result.get("ok"):
            return email_otp_result
        _, current_url = _follow_redirects(session, email_otp_result.get("next_url", ""), proxy=proxy)

    final = _finish_authorization(
        session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool, phone_probe_only=phone_probe_only
    )
    if final.get("ok"):
        final["login_method"] = "password"
        return final
    _raw_phone_attempt: object = final.get("phone_attempt")
    phone_attempt: dict = _raw_phone_attempt if isinstance(_raw_phone_attempt, dict) else {}
    if phone_attempt.get("error"):
        final["error"] = phone_attempt.get("error")
    final.setdefault("mode", "codex_oauth_pkce")
    final.setdefault("error", "password_login_oauth_callback_not_reached")
    return final


def _browser_style_send_otp_enabled():
    """``codex_oauth.browser_style_send_otp`` (default **True** since 2026-10-11)."""
    return _codex_oauth_cfg().get("browser_style_send_otp") is not False


def _password_first_relogin_enabled():
    """``codex_oauth.password_first_relogin`` (default **False**)."""
    return _codex_oauth_cfg().get("password_first_relogin") is True


def _browser_style_send_headers(did, sentinel_token):
    return _build_browser_style_send_headers(did, sentinel_token, attach_sentinel_fn=attach_sentinel)


def _send_passwordless_otp(session, did, current_url, proxy=None):
    return _send_passwordless_otp_impl(
        session,
        did,
        current_url,
        proxy,
        browser_style_enabled_fn=_browser_style_send_otp_enabled,
        load_cached_sentinel_fn=load_cached_sentinel,
        issue_sentinel_fn=issue_sentinel_flow,
        browser_style_headers_fn=_browser_style_send_headers,
        with_sentinel_fn=with_sentinel,
    )


def _resend_email_otp(session, did, current_url, proxy=None):
    del current_url
    return _resend_email_otp_impl(
        session,
        did,
        proxy,
        codex_sentinel_fn=_codex_sentinel,
        with_sentinel_fn=with_sentinel,
    )


def _complete_about_you(session, did, current_url, proxy=None):
    return _complete_about_you_impl(
        session,
        did,
        current_url,
        proxy,
        random_name_fn=random_name,
        random_birthdate_fn=random_birthdate,
        issue_sentinel_fn=issue_sentinel_flow,
        request_fn=request_with_retry,
        with_sentinel_fn=with_sentinel,
        follow_redirects_fn=_follow_redirects,
        safe_url_fn=_safe_url,
    )


def _finish_authorization(session, oauth, did, current_url, proxy=None, phone_pool=None, phone_probe_only=False):
    return _finish_authorization_impl(
        session,
        oauth,
        did,
        current_url,
        proxy,
        phone_pool,
        phone_probe_only,
        exchange_callback_fn=_exchange_callback,
        select_workspace_if_needed_fn=_select_workspace_if_needed,
        finish_phone_locked_fn=_finish_phone_authorization_locked,
        safe_url_fn=_safe_url,
    )


def _finish_phone_authorization_locked(session, oauth, did, current_url, proxy=None, phone_pool=None):
    return _finish_phone_authorization_locked_impl(
        session,
        oauth,
        did,
        current_url,
        proxy,
        phone_pool,
        complete_phone_fn=complete_phone_verification,
        auto_phone_fn=_auto_phone_verification,
        follow_redirects_fn=_follow_redirects,
        exchange_callback_fn=_exchange_callback,
        select_workspace_if_needed_fn=_select_workspace_if_needed,
        safe_url_fn=_safe_url,
    )


def _complete_email_otp(session, data, did, current_url, proxy=None, timeout=180):
    return _complete_email_otp_impl(
        session,
        data,
        did,
        current_url,
        proxy,
        timeout,
        mailbox_from_data_fn=_mailbox_from_data,
        poll_email_otp_fn=poll_email_otp,
        codex_sentinel_fn=_codex_sentinel,
        with_sentinel_fn=with_sentinel,
        next_url_fn=_next_url,
    )


def _select_workspace_if_needed(session, did, current_url, proxy=None):
    return _select_workspace_if_needed_impl(
        session,
        did,
        current_url,
        proxy,
        parse_workspace_fn=_parse_workspace_from_auth_cookie,
        select_workspace_fn=_select_workspace,
        follow_redirects_fn=_follow_redirects,
    )


def _select_workspace(session, did, referer, workspace_id, proxy=None, timeout=30, extra_headers=None):
    return _select_workspace_impl(
        session,
        did,
        referer,
        workspace_id,
        proxy,
        timeout,
        extra_headers,
        codex_sentinel_fn=_codex_sentinel,
        with_sentinel_fn=with_sentinel,
        next_url_fn=_next_url,
    )


def select_workspace_by_cookie(
    cookie_header, workspace_id, device_id="", proxy=None, timeout=30, referer=endpoints.AUTH_BASE + "/workspace"
):
    """Select an auth workspace using saved cookies."""
    cookie_header = str(cookie_header or "").strip()
    if not cookie_header:
        return {"ok": False, "workspace_id": str(workspace_id or "").strip(), "error": "missing_cookie_header"}
    session = _new_codex_session(proxy=proxy, device_id=device_id or "scan")
    try:
        return _select_workspace(
            session,
            device_id or "scan",
            referer,
            workspace_id,
            proxy=proxy,
            timeout=timeout,
            extra_headers={"cookie": cookie_header},
        )
    finally:
        close_fn = getattr(session, "close", None)
        if callable(close_fn):
            close_fn()


def _exchange_callback(callback_url, oauth, proxy=None):
    return _exchange_callback_impl(
        callback_url,
        oauth,
        proxy,
        new_session_fn=_new_codex_session,
        request_fn=request_with_retry,
    )


def _save_oauth_tokens(data, json_path, tokens, email, mode, result=None):
    return _save_oauth_tokens_impl(
        data,
        json_path,
        tokens,
        email,
        mode,
        result=result,
        upsert=upsert_account,
        write_text=atomic_write_text,
    )


def persist_one_click_sms_failure(data, json_path, email, result, *, upsert=None):
    return _persist_one_click_sms_failure_impl(
        data,
        json_path,
        email,
        result,
        upsert=upsert or upsert_account,
        write_text=atomic_write_text,
    )


def _follow_redirects(session, start_url, proxy=None, max_redirects=18):
    del proxy
    return _follow_redirects_impl(session, start_url, max_redirects, request_fn=request_with_retry)


def _mailbox_from_data(data):
    return _build_mailbox_from_data(
        data,
        has_credentials_fn=mailbox_has_inbox_credentials,
        configured_pool_fn=_mailbox_from_configured_pool,
    )


def _codex_oauth_cfg() -> dict[str, Any]:
    _raw: object = CFG.get("codex_oauth")
    return _raw if isinstance(_raw, dict) else {}


def _allow_passwordless_takeover():
    return bool(_codex_oauth_cfg().get("allow_passwordless_takeover", False))


def _auto_phone_verification():
    return bool(_codex_oauth_cfg().get("auto_phone_verification", False))


def _parse_workspace_from_auth_cookie(auth_cookie):
    return _parse_workspace_from_auth_cookie_impl(auth_cookie, jwt_segment_fn=_jwt_segment)


def _jwt_segment(segment):
    if _NATIVE_JWT_SEGMENT is not None and isinstance(segment, str):
        return _NATIVE_JWT_SEGMENT(segment)
    try:
        padded = segment + "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _safe_url(url):
    if _NATIVE_SAFE_URL is not None and isinstance(url, str):
        return _NATIVE_SAFE_URL(url)
    try:
        parsed = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return str(url or "")[:200]
