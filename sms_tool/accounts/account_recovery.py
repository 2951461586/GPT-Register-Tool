"""Local account liveness refresh and ordered AT recovery workflows.

The liveness probe itself is side-effect free and lives in
``account_liveness``. This module owns verified persistence, deactivation
handling, and the protocol recovery chain (OAuth refresh token, existing
ChatGPT cookie session, protocol email-OTP login, then Codex OAuth PKCE).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from .account_identity import account_identity as account_identity, resolve_account_proxy
from .account_liveness import (
    browser_fetch_for_account as browser_fetch_for_account,
    probe_account_liveness,
)
from .recovery_state import (
    _CODE_PHASE_MARKERS as _CODE_PHASE_MARKERS,
    _NO_CODE_SPENT_MARKERS as _NO_CODE_SPENT_MARKERS,
    _PRE_OTP_RETRY_MARKERS as _PRE_OTP_RETRY_MARKERS,
    _RELOGIN_CODE_RE as _RELOGIN_CODE_RE,
    _RELOGIN_STATUS_RE as _RELOGIN_STATUS_RE,
    _TRANSIENT_RELOGIN_MODES as _TRANSIENT_RELOGIN_MODES,
    _has_relogin_material as _has_relogin_material,
    _is_pre_otp_proxy_retryable,
    _is_token_revoked_drop as _is_token_revoked_drop,
    _item_is_account_deactivated as _item_is_account_deactivated,
    _local_account_data as _local_account_data,
    _looks_account_deactivated,
    _normalize_email as _normalize_email,
    _normalize_relogin_mode as _normalize_relogin_mode,
    _recoverable_on_other_proxy,
    _relogin_attempt_shape as _relogin_attempt_shape,
    _relogin_dead_end_reason as _relogin_dead_end_reason,
    _relogin_failure_shape as _relogin_failure_shape,
    _relogin_spent_email_code,
    clear_relogin_failure as _clear_relogin_failure_impl,
    health_status_code as _health_status_code_impl,
    is_permanently_deactivated as is_permanently_deactivated,
    is_transient_transport_error as is_transient_transport_error,
    local_quota_accounts as _local_quota_accounts_impl,
    mark_successful_relogin as _mark_successful_relogin_impl,
    persist_permanent_deactivation as _persist_permanent_deactivation_impl,
    persist_token_revoked_drop as _persist_token_revoked_drop_impl,
    record_relogin_failure as _record_relogin_failure_impl,
    recovery_allow_direct as _recovery_allow_direct_impl,
    recovery_proxy_candidates as _recovery_proxy_candidates_impl,
    relogin_cooldown_active as _relogin_cooldown_active_impl,
    relogin_cooldown_seconds as _relogin_cooldown_seconds_impl,
    relogin_dead_end_permanent as _relogin_dead_end_permanent_impl,
    relogin_repeat_cooldown_seconds as _relogin_repeat_cooldown_seconds_impl,
    timed_out_health_result as _timed_out_health_result_impl,
)
from ..config import CFG
from ..payment.promotion_states import AUTH_INVALID_LABEL, PROMOTION_STATE_AUTH_INVALID
from ..proxy.routing import proxy_pool_for
from ..kernel.paths import runtime_file
from ..registration.state import _stored_registration_password
from ..storage import (
    _probe_is_token_invalid,
    clear_stale_promotion_at_marker as clear_stale_promotion_at_marker,
    get_account_record,
    list_paypal_accounts,
    mark_quota_status,
    upsert_account,
)
from ..mailbox.quarantine import mailbox_relogin_allowed as mailbox_relogin_allowed
from ..providers.mailbox_graph import MailboxAuthInvalidError
from ..utils import atomic_write_text
from ..transport import endpoints
from ..kernel.native_loader import load_native_module

logger = logging.getLogger(__name__)

_NATIVE_ACCOUNT_RECOVERY = load_native_module(
    "account-recovery-rs", "account_recovery_rs", "RUST_ANYTHING_UNIT_ACCOUNT_RECOVERY"
)
_NATIVE_REDACT_RECOVERY_ERROR = getattr(_NATIVE_ACCOUNT_RECOVERY, "_redact_recovery_error", None)
_NATIVE_RELOGIN_FAILURE_QUOTA_STATUS = getattr(_NATIVE_ACCOUNT_RECOVERY, "_relogin_failure_quota_status", None)
_NATIVE_SCALAR_TYPES = (str, type(None), bool, int, float)


def relogin_web_session_account(
    account: dict[str, Any], proxy: str | None = None, timeout: int = 180
) -> dict[str, Any]:
    """Refresh a web access token from an existing ChatGPT session cookie."""
    if not isinstance(account, dict):
        return {"ok": False, "mode": "web_session", "error": "invalid_account"}
    email = str(account.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "mode": "web_session", "error": "missing_email"}
    try:
        from ..session.refresh import refresh_session_protocol

        data = dict(account)
        data["email"] = email
        result = dict(
            refresh_session_protocol(
                data,
                str(account.get("json_path") or ""),
                email,
                max(30, int(timeout or 180)),
                proxy=proxy,
                persist=False,
            )
            or {}
        )
        if not result.get("ok"):
            safe = _safe_relogin_result(result)
            safe.update({"ok": False, "mode": "web_session"})
            return safe
        _data_raw: object = result.get("data")
        return _verify_and_persist_candidate(
            account,
            _data_raw if isinstance(_data_raw, dict) else {},
            mode="web_session",
            proxy=proxy,
            timeout=timeout,
        )
    except Exception as exc:
        return {"ok": False, "mode": "web_session", "error": _redact_recovery_error(exc)}


def relogin_refresh_token_account(
    account: dict[str, Any],
    proxy: str | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    """Exchange a stored OpenAI refresh token and persist only a verified AT."""
    if not isinstance(account, dict):
        return {"ok": False, "mode": "oauth_refresh_token", "error": "invalid_account"}
    email = str(account.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "mode": "oauth_refresh_token", "error": "missing_email"}
    from ..codex.export import _openai_refresh_token, _refresh_with_openai_oauth

    _raw_auth_session: object = account.get("auth_session")
    auth_session: dict = _raw_auth_session if isinstance(_raw_auth_session, dict) else {}
    refresh_token = _openai_refresh_token(account, auth_session)
    if not refresh_token:
        return {"ok": False, "mode": "oauth_refresh_token", "error": "missing_refresh_token", "skipped": True}
    result = _refresh_with_openai_oauth(account, refresh_token, proxy=proxy)
    if not result.get("ok"):
        return {
            "ok": False,
            "mode": "oauth_refresh_token",
            "error": _redact_recovery_error(result.get("error") or "oauth_refresh_failed"),
        }
    candidate = dict(account)
    _data_raw: object = result.get("data")
    candidate.update(_data_raw if isinstance(_data_raw, dict) else {})
    candidate["email"] = email
    candidate["refresh_token_status"] = "oauth_present"
    candidate["refresh_token_updated_at"] = int(time.time())
    return _verify_and_persist_candidate(
        account,
        candidate,
        mode="oauth_refresh_token",
        proxy=proxy,
        timeout=timeout,
    )


def _passwordless_registration(account: object) -> bool:
    """True when the account was registered without a server-side password.

    ``registration_mode == "passwordless"`` means signup never set a password, so
    the ``password`` column only holds the value this tool generated locally. For
    such an account the existing-login lane must not submit it: the login-method
    probe reports ``password_form_present`` for *every* address (the sibling
    ``/log-in/password`` shell carries a form; measured 2026-10-11), so the
    password branch is chosen deterministically and
    ``/api/accounts/password/verify`` can only answer 401. The lane then fails
    before reaching the email code it was chosen to avoid spending.

    An absent mode is deliberately **not** treated as passwordless: bare-row
    callers (imports, older records) keep the previous behaviour, and only an
    explicit passwordless registration takes the code path.
    """
    if not isinstance(account, dict):
        return False
    mode = str(account.get("registration_mode") or "").strip().lower().replace("-", "_")
    if mode in {"passwordless", "passwordless_signup", "login_or_signup", "har"}:
        return True
    return bool(account.get("password_unknown"))


def relogin_chatgpt_email_account(
    account: object,
    proxy: str | None = None,
    timeout: int = 300,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Acquire a ChatGPT web AT through the passwordless email-OTP protocol."""
    if not isinstance(account, dict):
        return {"ok": False, "mode": "chatgpt_email_otp", "error": "invalid_account"}
    email = str(account.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_email"}
    try:
        import uuid

        from curl_cffi import requests as curl_requests

        from ..registration.account_creation import _auth_session_access_token, _fetch_auth_session
        from ..auth_flow import _json_or_raw
        from ..transport.auth_headers import auth_impersonate, openai_auth_headers, select_auth_fingerprint
        from ..codex.oauth import _mailbox_from_data
        from ..transport.http_client import request_with_retry
        from ..registration.orchestrator import _login_existing_account_with_email_otp
        from ..registration.sentinel_tokens import set_oai_did_cookie
        from ..session.refresh import auth_session_email

        mailbox = _mailbox_from_data(account)
        if mailbox is None:
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_mailbox"}

        select_auth_fingerprint(rotate=True)
        _chat_raw: object = CFG.get("chatgpt")
        chat_cfg: dict[str, Any] = _chat_raw if isinstance(_chat_raw, dict) else {}
        auth_base = str(chat_cfg.get("auth_base_url") or endpoints.AUTH_BASE).rstrip("/")
        chat_base = str(chat_cfg.get("chat_base_url") or endpoints.CHATGPT_BASE).rstrip("/")
        device_id = str(account.get("device_id") or uuid.uuid4())
        logging_id = str(uuid.uuid4()).replace("-", "")
        session = curl_requests.Session()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        set_oai_did_cookie(session, device_id)
        base_headers = openai_auth_headers(device_id, accept="application/json", include_trace=True)

        request_with_retry(
            session,
            "get",
            f"{chat_base}/",
            label="ChatGPT email relogin prime",
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml"},
            impersonate=auth_impersonate(),
        )
        csrf_response = request_with_retry(
            session,
            "get",
            f"{chat_base}/api/auth/csrf",
            label="ChatGPT email relogin csrf",
            headers={**base_headers, "Accept": "application/json", "Referer": f"{chat_base}/"},
            impersonate=auth_impersonate(),
        )
        csrf_token = str(_json_or_raw(csrf_response).get("csrfToken") or "").strip()
        if not csrf_token:
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_csrf_token"}

        login = _login_existing_account_with_email_otp(
            session=session,
            username=email,
            mailbox=mailbox,
            did=device_id,
            session_logging_id=logging_id,
            auth_base=auth_base,
            chat_base=chat_base,
            base_headers=base_headers,
            csrf_token=csrf_token,
            proxy=proxy,
            totp_secret=str(account.get("totp_secret") or ""),
            # The lane probes for a password step before spending an email code,
            # so hand it the password we hold: a positive verdict then becomes a
            # real login instead of "the account has a password we cannot
            # submit".  ``_stored_registration_password`` already drops a value
            # whose stored error says the verify failed.
            #
            # A passwordless-registered account holds no server-side password, so
            # it is handed an empty string on purpose: that is what makes the lane
            # take its ``allow_passwordless`` branch (True on this entry point) and
            # spend the email code instead of submitting a password the server
            # never knew. Measured 2026-10-25: two ``registration_mode:
            # passwordless`` remail accounts failed every 测活 run at
            # ``existing_login_password_verify_failed:401`` without ever
            # requesting a code.
            password="" if _passwordless_registration(account) else _stored_registration_password(email),
            otp_timeout=max(30, int(timeout or 300)),
        )
        if not login.get("ok"):
            return {
                "ok": False,
                "mode": "chatgpt_email_otp",
                "error": _redact_recovery_error(login.get("error") or "email_login_failed"),
            }

        auth_result = _fetch_auth_session(session, chat_base, base_headers)
        _raw_auth_session: object = auth_result.get("body")
        auth_session: dict = _raw_auth_session if isinstance(_raw_auth_session, dict) else {}
        access_token = str(_auth_session_access_token(auth_session) or "").strip()
        if not access_token:
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "auth_session_missing_access_token"}
        authenticated_email = auth_session_email(auth_session)
        if not authenticated_email:
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "auth_session_missing_email"}
        if authenticated_email != email:
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "auth_session_email_mismatch"}

        candidate = dict(account)
        candidate.update(
            {
                "email": email,
                "device_id": device_id,
                "access_token": access_token,
                "auth_session": auth_session,
                "cookie_header": str(auth_result.get("cookie_header") or ""),
                "refresh_token_status": "no_rt",
            }
        )
        return _verify_and_persist_candidate(
            account,
            candidate,
            mode="chatgpt_email_otp",
            proxy=proxy,
            timeout=timeout,
            persist=persist,
        )
    except MailboxAuthInvalidError:
        return {
            "ok": False,
            "mode": "chatgpt_email_otp",
            "error": "mailbox_auth_invalid",
            "mailbox_auth_invalid": True,
        }
    except Exception as exc:
        return {
            "ok": False,
            "mode": "chatgpt_email_otp",
            "error": _redact_recovery_error(exc),
        }


def _needs_passwordless_email_fallback(account: dict[str, Any], result: Any) -> bool:
    """True when a stored-password attempt bounced and direct email OTP should run."""
    if not isinstance(result, dict) or result.get("ok") or _passwordless_registration(account):
        return False
    if result.get("terminal") or _looks_account_deactivated(result):
        return False
    error = str(result.get("error") or "").lower()
    return "existing_login_password_verify_failed" in error or error == "auth_session_missing_access_token"


def _run_chatgpt_email_recovery(
    account: dict[str, Any],
    *,
    candidates: list[str | None],
    timeout: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Run ``chatgpt_email_otp`` with pre-OTP exit rotation and passwordless fallback.

    Field measurements (2026-11-25, 286-account recovery):
    - Legacy accounts without ``registration_mode`` (139/286) bounce 401 on
      ``/password/verify``, and password+2FA accounts redirect to
      ``/email-verification`` without a session; retrying directly with
      ``password_unknown=True`` recovered 175/189 Outlook accounts (92.6%).
    - Pre-OTP 429/403/502/CSRF errors spent zero email codes, so rotating to a
      different exit avoids cascading into ``codex_oauth_pkce`` on the same bad exit.
    """
    attempts: list[dict[str, Any]] = []
    pool = list(candidates) or [None]
    for extra in proxy_pool_for(CFG, "relogin"):
        if extra and extra not in pool and len(pool) < 3:
            pool.append(extra)
    cursor = 0
    result: dict[str, Any] = {}
    while cursor < len(pool):
        candidate = pool[cursor]
        result = dict(relogin_chatgpt_email_account(account, proxy=candidate, timeout=timeout) or {})
        if result.get("ok"):
            return result, attempts, cursor
        if _needs_passwordless_email_fallback(account, result):
            attempt = _safe_relogin_result(result)
            attempt.setdefault("mode", "chatgpt_email_otp")
            if cursor:
                attempt["proxy_index"] = cursor
            attempts.append(attempt)
            next_index = (cursor + 1) % len(pool) if len(pool) > 1 else cursor
            fallback_account = {**account, "password_unknown": True}
            result = dict(
                relogin_chatgpt_email_account(fallback_account, proxy=pool[next_index], timeout=timeout) or {}
            )
            if result.get("ok"):
                return result, attempts, next_index
            attempt2 = _safe_relogin_result(result)
            attempt2.setdefault("mode", "chatgpt_email_otp")
            attempt2["fallback"] = "passwordless_email_otp"
            if next_index:
                attempt2["proxy_index"] = next_index
            attempts.append(attempt2)
            return result, attempts, next_index
        attempt = _safe_relogin_result(result)
        attempt.setdefault("mode", "chatgpt_email_otp")
        if cursor:
            attempt["proxy_index"] = cursor
        attempts.append(attempt)
        if not _is_pre_otp_proxy_retryable(result):
            return result, attempts, cursor
        cursor += 1
    return result, attempts, max(0, cursor - 1)


def relogin_codex_account(
    account: dict[str, Any],
    proxy: str | None = None,
    timeout: int = 180,
    mode: str = "auto",
) -> dict[str, Any]:
    """Recover an invalid AT through the selected recovery strategy."""
    if is_permanently_deactivated(account):
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": "account_deactivated",
            "terminal": True,
            "skipped": True,
        }
    resolved_proxy = resolve_account_proxy(account, fallback_proxy=proxy, config=CFG)
    normalized_mode = _normalize_relogin_mode(mode)
    if normalized_mode == "web_session":
        return relogin_web_session_account(account, proxy=resolved_proxy, timeout=timeout)
    if normalized_mode == "chatgpt_email_otp":
        recovery_proxy, _ = _select_recovery_proxy(account, resolved_proxy)
        candidates = _recovery_proxy_candidates(resolved_proxy, recovery_proxy)
        result, attempts, proxy_index = _run_chatgpt_email_recovery(account, candidates=candidates, timeout=timeout)
        if result.get("ok"):
            success = _safe_relogin_result(result)
            if attempts:
                success["attempts"] = attempts
            if proxy_index:
                success["proxy_index"] = proxy_index
            return success
        if _looks_account_deactivated(result):
            _persist_permanent_deactivation(account, result)
            result = {**result, "terminal": True, "error": "account_deactivated"}
        return result
    if normalized_mode == "codex_oauth":
        return relogin_local_codex_account(account, proxy=resolved_proxy, timeout=timeout)
    if normalized_mode == "browser_session":
        return relogin_browser_session_account(account, proxy=resolved_proxy, timeout=timeout)

    recovery_proxy, proxy_attempts = _select_recovery_proxy(account, resolved_proxy)
    attempts: list[dict[str, Any]] = []
    strategies = (
        ("oauth_refresh_token", relogin_refresh_token_account, timeout),
        # ``web_session`` replays a session cookie and costs nothing, so it keeps
        # a bounded window (45..60s) and rotates across exits on 403/429.
        ("web_session", relogin_web_session_account, min(max(45, int(timeout or 180)), 60)),
        ("chatgpt_email_otp", relogin_chatgpt_email_account, timeout),
        ("codex_oauth_pkce", relogin_local_codex_account, timeout),
        ("browser_session", relogin_browser_session_account, timeout),
    )
    proxy_candidates = _recovery_proxy_candidates(resolved_proxy, recovery_proxy)
    retry_across_proxies = {"oauth_refresh_token", "web_session"}
    skip_strategies: set[str] = set()
    for strategy, handler, strategy_timeout in strategies:
        if strategy in skip_strategies:
            attempts.append(
                {
                    "ok": False,
                    "mode": strategy,
                    "error": "skipped_same_mailbox_otp_poll_timeout",
                    "skipped": True,
                }
            )
            continue
        result: dict[str, Any] = {}
        if strategy == "chatgpt_email_otp":
            result, strategy_attempts, proxy_index = _run_chatgpt_email_recovery(
                account, candidates=proxy_candidates, timeout=strategy_timeout
            )
            attempts.extend(strategy_attempts)
            if result.get("ok"):
                success = _safe_relogin_result(result)
                success["attempts"] = attempts
                if proxy_attempts:
                    success["proxy_attempts"] = proxy_attempts
                if proxy_index:
                    success["proxy_index"] = proxy_index
                return success
        else:
            candidates = proxy_candidates if strategy in retry_across_proxies else [recovery_proxy]
            for proxy_index, candidate in enumerate(candidates):
                result = dict(handler(account, proxy=candidate, timeout=strategy_timeout) or {})
                if result.get("ok"):
                    success = _safe_relogin_result(result)
                    success["attempts"] = attempts
                    if proxy_attempts:
                        success["proxy_attempts"] = proxy_attempts
                    if proxy_index:
                        success["proxy_index"] = proxy_index
                    return success
                attempt = _safe_relogin_result(result)
                attempt.setdefault("mode", strategy)
                if proxy_index:
                    attempt["proxy_index"] = proxy_index
                attempts.append(attempt)
                if result.get("remote_ok") and result.get("persisted") is False:
                    return {**attempt, "attempts": attempts}
                if not _recoverable_on_other_proxy(result):
                    break
        if strategy == "chatgpt_email_otp" and "otp_poll_timeout" in str(result.get("error") or ""):
            skip_strategies.add("codex_oauth_pkce")
        if str(result.get("error") or "") == "mailbox_auth_invalid":
            return {
                "ok": False,
                "mode": strategy,
                "error": "mailbox_auth_invalid",
                "mailbox_auth_invalid": True,
                "attempts": attempts,
            }
        if result.get("terminal") or _looks_account_deactivated(result):
            _persist_permanent_deactivation(account, result)
            return {
                "ok": False,
                "mode": strategy,
                "error": "account_deactivated",
                "terminal": True,
                "attempts": attempts,
                **({"proxy_attempts": proxy_attempts} if proxy_attempts else {}),
            }
    return {
        "ok": False,
        "mode": "auto",
        "error": "all_relogin_methods_failed",
        "attempts": attempts,
        **({"proxy_attempts": proxy_attempts} if proxy_attempts else {}),
    }


def relogin_local_codex_account(
    account: dict[str, Any],
    proxy: str | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    """Acquire, verify, and then persist an email-OTP OAuth access token."""
    if not isinstance(account, dict):
        return {"ok": False, "error": "invalid_account"}
    email = str(account.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "error": "missing_email"}
    if is_permanently_deactivated(account):
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": "account_deactivated",
            "terminal": True,
            "skipped": True,
        }
    try:
        from ..codex.oauth import _save_oauth_tokens, refresh_codex_oauth_session

        data = dict(account)
        data["email"] = email
        result = refresh_codex_oauth_session(
            data,
            json_path=str(account.get("json_path") or ""),
            proxy=proxy,
            timeout=max(30, int(timeout or 180)),
            force_email_otp_login=True,
            phone_pool=None,
            phone_probe_only=True,
            persist=False,
        )
        if not result.get("ok"):
            if _looks_account_deactivated(result):
                _persist_permanent_deactivation(data, result)
            safe = _safe_relogin_result(result)
            safe["ok"] = False
            return safe

        _tokens_raw: object = result.get("tokens")
        tokens: dict[str, Any] = _tokens_raw if isinstance(_tokens_raw, dict) else {}
        candidate_at = str(tokens.get("access_token") or "").strip()
        if not candidate_at:
            return {
                "ok": False,
                "mode": "codex_oauth_pkce",
                "error": "oauth_missing_access_token",
                "persisted": False,
            }
        candidate = dict(data)
        candidate["access_token"] = candidate_at
        candidate["id_token"] = str(tokens.get("id_token") or "").strip()
        probe = probe_account_liveness(candidate, proxy=proxy, timeout=min(max(10, int(timeout or 30)), 60))
        if int(probe.get("status_code") or 0) != 200:
            safe = _safe_relogin_result(result)
            safe.update(
                {
                    "ok": False,
                    "error": f"oauth_access_token_probe_failed:{probe.get('status_code') or 'unknown'}",
                    "probe": probe,
                    "persisted": False,
                }
            )
            return safe

        _mark_successful_relogin(data, probe)
        saved = _save_oauth_tokens(
            data,
            str(account.get("json_path") or ""),
            tokens,
            email,
            "codex_oauth_pkce",
            result=result,
        )
        safe = _safe_relogin_result(saved)
        persisted = bool(saved.get("persisted"))
        safe.update({"ok": persisted, "probe": probe, "persisted": persisted, "remote_ok": True})
        if not persisted:
            safe["error"] = "oauth_persistence_failed"
        return safe
    except Exception as exc:
        return {"ok": False, "error": _redact_recovery_error(exc)}


def relogin_browser_session_account(
    account: dict[str, Any],
    proxy: str | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Recover an invalid AT through a Camoufox browser session.

    Launches a headless Camoufox browser, navigates to ChatGPT, waits for
    Cloudflare to clear, and extracts the access token from the browser's
    session endpoint.  This bypasses Cloudflare 401 blocks that affect
    protocol-only requests.
    """
    if not isinstance(account, dict):
        return {"ok": False, "mode": "browser_session", "error": "invalid_account"}
    email = str(account.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "mode": "browser_session", "error": "missing_email"}
    if is_permanently_deactivated(account):
        return {
            "ok": False,
            "mode": "browser_session",
            "error": "account_deactivated",
            "terminal": True,
            "skipped": True,
        }
    try:
        from ..registration_drivers import browser_flow
        from ..registration_drivers.external_sessions import create_browser_session

        _raw_config: object = getattr(CFG, "data", None)
        config = _raw_config if isinstance(_raw_config, dict) else {}
        _chat_raw: object = config.get("chatgpt")
        chat_cfg: dict[str, Any] = _chat_raw if isinstance(_chat_raw, dict) else {}
        chat_base = str(chat_cfg.get("chat_base_url") or endpoints.CHATGPT_BASE).rstrip("/")
        auth_base = str(chat_cfg.get("auth_base_url") or endpoints.AUTH_BASE).rstrip("/")
        device_id = str(account.get("device_id") or uuid.uuid4())

        # Determine the driver to use for browser recovery.  When the
        # account was registered through a browser driver, reuse the same
        # driver and profile from the persisted browser_identity so the
        # recovery session carries the original fingerprint and cookies.
        from .account_identity import account_identity

        identity = account_identity(account)
        browser_identity = identity.get("browser_identity") or {}
        recovery_driver = str(browser_identity.get("driver") or "").strip().lower() or "camoufox"
        if not browser_identity and isinstance(config.get("registration"), dict):
            configured_driver = str(config["registration"].get("driver") or "").strip().lower().replace("-", "_")
            if configured_driver in {"cloak", "roxy", "playwright"}:
                recovery_driver = configured_driver

        browser_session = create_browser_session(
            recovery_driver,
            config=config,
            proxy=proxy,
            headless=True,
            timeout_ms=max(10_000, int(timeout) * 1000),
            locale="en-US",
            timezone_id="America/New_York",
            browser_identity=dict(browser_identity) if browser_identity else None,
        )
        with browser_session as browser:
            browser.add_device_cookie(device_id, chat_base, auth_base)
            page = browser.page
            if page is None:
                return {
                    "ok": False,
                    "mode": "browser_session",
                    "error": "browser_page_missing",
                }
            page.goto(chat_base, wait_until="domcontentloaded", timeout=int(timeout) * 1000)
            # Wait for Cloudflare challenge to clear automatically
            browser_flow.page_state._wait_for_challenge_clear(page, max_wait_seconds=30)
            # Extract session info
            session_info = browser_flow.session._session_payload(browser, chat_base, email, timeout_seconds=timeout)
            auth_body = session_info.get("body") or {}
            access_token = str(session_info.get("access_token") or "").strip()
            if not access_token:
                return {
                    "ok": False,
                    "mode": "browser_session",
                    "error": "browser_session_no_access_token",
                }
            candidate = dict(account)
            candidate.update(
                {
                    "email": email,
                    "device_id": device_id,
                    "access_token": access_token,
                    "auth_session": auth_body,
                    "cookie_header": str(browser.cookie_header() or ""),
                }
            )
            return _verify_and_persist_candidate(
                account,
                candidate,
                mode="browser_session",
                proxy=proxy,
                timeout=timeout,
                persist=True,
            )
    except Exception as exc:
        return {
            "ok": False,
            "mode": "browser_session",
            "error": _redact_recovery_error(exc),
        }


def _probe_candidate_with_transport_retry(
    verified: dict[str, Any],
    *,
    proxy: str | None,
    timeout: int,
) -> dict[str, Any]:
    """Verify a newly-acquired AT, retrying only on status_code=0 transport drops.

    Measured 2026-11-25: two accounts completed email-OTP login and received valid
    ATs, only to fail verification with ``probe_failed:unknown`` when the same
    exit dropped the immediate follow-up GET. Rotating exits on status_code == 0
    saves the already-acquired AT without spending another OTP; any HTTP answer
    (401/403/etc.) returns immediately.
    """
    probe_timeout = min(max(10, int(timeout or 30)), 60)
    probe = probe_account_liveness(verified, proxy=proxy, timeout=probe_timeout)
    if int(probe.get("status_code") or 0) != 0:
        return probe
    extras = [candidate for candidate in proxy_pool_for(CFG, "liveness") if candidate and candidate != proxy][:2]
    for fallback_proxy in extras:
        probe = probe_account_liveness(verified, proxy=fallback_proxy, timeout=probe_timeout)
        if int(probe.get("status_code") or 0) != 0:
            return probe
    return probe


def _verify_and_persist_candidate(
    account: dict[str, Any],
    candidate: dict[str, Any],
    *,
    mode: str,
    proxy: str | None,
    timeout: int,
    persist: bool = True,
) -> dict[str, Any]:
    email = str(candidate.get("email") or account.get("email") or "").strip().lower()
    access_token = str(candidate.get("access_token") or "").strip()
    if not access_token:
        return {"ok": False, "mode": mode, "error": f"{mode}_missing_access_token", "persisted": False}

    verified = dict(account)
    verified.update(candidate)
    verified["email"] = email
    if mode == "web_session":
        from ..session.refresh import auth_session_email

        _raw_auth_session: object = verified.get("auth_session")
        auth_session: dict = _raw_auth_session if isinstance(_raw_auth_session, dict) else {}
        authenticated_email = auth_session_email(auth_session)
        if not authenticated_email:
            return {"ok": False, "mode": mode, "error": "auth_session_missing_email", "persisted": False}
        if authenticated_email != email:
            return {"ok": False, "mode": mode, "error": "auth_session_email_mismatch", "persisted": False}
    probe = _probe_candidate_with_transport_retry(verified, proxy=proxy, timeout=timeout)
    if int(probe.get("status_code") or 0) != 200:
        return {
            "ok": False,
            "mode": mode,
            "error": f"{mode}_access_token_probe_failed:{probe.get('status_code') or 'unknown'}",
            "probe": probe,
            "persisted": False,
        }

    now = int(time.time())
    _mark_successful_relogin(verified, probe, now=now)
    verified["access_token_updated_at"] = now
    verified["refreshed_at"] = now
    json_path = str(verified.get("json_path") or account.get("json_path") or "").strip()
    saved_path = json_path
    if persist:
        from ..session.refresh import save_refreshed

        saved_path = save_refreshed(verified, json_path)
    return {
        "ok": True,
        "mode": mode,
        "email": email,
        "json_path": saved_path,
        "probe": probe,
        "persisted": bool(persist),
        "refresh_token_status": str(verified.get("refresh_token_status") or "no_rt"),
        **({"_verified_data": verified} if not persist else {}),
    }


def _promotion_auth_failure(data: Any) -> bool:
    """True when a persisted promotion probe recorded a dead access token."""
    if not isinstance(data, dict):
        return False
    _promotion_raw: object = data.get("promotion")
    promotion: dict[str, Any] = _promotion_raw if isinstance(_promotion_raw, dict) else {}
    state = str(promotion.get("state") or data.get("promotion_state") or "").strip().lower()
    if state:
        return state == PROMOTION_STATE_AUTH_INVALID
    if str(promotion.get("status_code") or "").strip():
        return str(promotion.get("status_code") or "").strip() == "401"
    label = str(data.get("promotion_status") or "").strip() or str(promotion.get("status") or "").strip()
    return label == AUTH_INVALID_LABEL


def _mark_successful_relogin(data: dict[str, Any], probe: dict[str, Any], *, now: int | None = None) -> None:
    _mark_successful_relogin_impl(
        data, probe, safe_result_fn=_safe_relogin_result, promotion_auth_failure_fn=_promotion_auth_failure, now=now
    )


def _select_recovery_proxy(account: dict[str, Any], proxy: str | None) -> tuple[str | None, list[dict[str, Any]]]:
    country = str(account.get("registration_country") or "").strip().upper()
    if not country:
        return proxy, []
    _proxy_raw: object = CFG.get("proxy")
    proxy_cfg: dict[str, Any] = _proxy_raw if isinstance(_proxy_raw, dict) else {}
    configured = proxy_cfg.get("pool") or []
    if isinstance(configured, str):
        configured = [configured]
    candidates: list[str] = [
        str(value)
        for value in (
            proxy,
            *configured,
            proxy_cfg.get("registration"),
            proxy_cfg.get("default"),
        )
        if str(value or "").strip()
    ]
    if not candidates:
        return proxy, []
    try:
        from ..paypal.proxy import select_proxy_from_pool

        selected, attempts = select_proxy_from_pool(candidates, country, "account_recovery")
        return (selected or proxy or str(candidates[0])), attempts
    except Exception as exc:
        return proxy or str(candidates[0]), [
            {
                "ok": False,
                "stage": "account_recovery",
                "expected_country": country,
                "error": _redact_recovery_error(exc)[:200],
            }
        ]


def _recovery_allow_direct() -> bool:
    return _recovery_allow_direct_impl(CFG)


def _recovery_proxy_candidates(affinity_proxy: str | None, selected_proxy: str | None) -> list[str | None]:
    return _recovery_proxy_candidates_impl(
        affinity_proxy,
        selected_proxy,
        allow_direct=_recovery_allow_direct(),
    )


def _health_status_code(probe: dict[str, Any], relogin: dict[str, Any]) -> str:
    return _health_status_code_impl(probe, relogin, probe_is_token_invalid=_probe_is_token_invalid)


def _local_quota_accounts(emails: list[str] | None) -> list[dict[str, Any]]:
    return _local_quota_accounts_impl(
        emails,
        list_accounts=list_paypal_accounts,
        get_record=get_account_record,
    )


def _persist_token_revoked_drop(account: dict[str, Any]) -> bool:
    return _persist_token_revoked_drop_impl(account, upsert=upsert_account, write_text=atomic_write_text)


def _persist_permanent_deactivation(account: dict[str, Any], result: dict[str, Any] | None = None) -> bool:
    return _persist_permanent_deactivation_impl(account, result, upsert=upsert_account, write_text=atomic_write_text)


def _safe_relogin_result(result: dict[str, Any] | None) -> dict[str, Any]:
    blocked = {
        "tokens",
        "access_token",
        "id_token",
        "refresh_token",
        "oauth_refresh_token",
        "data",
        "auth_session",
        "cookie_header",
        "password",
        "mailbox",
        "raw_json",
    }
    safe: dict[str, Any] = {}
    for key, value in dict(result or {}).items():
        if key in blocked:
            continue
        safe[key] = _redact_recovery_error(value) if key in {"error", "message", "last_url"} else value
    return safe


def _redact_recovery_error(value: Any) -> str:
    if _NATIVE_REDACT_RECOVERY_ERROR is not None and isinstance(value, _NATIVE_SCALAR_TYPES):
        return str(_NATIVE_REDACT_RECOVERY_ERROR(value))
    text = str(value or "")
    text = re.sub(r"((?:https?|socks5h?)://)[^@\s/]+@", r"\1[REDACTED]@", text, flags=re.I)
    text = re.sub(r"\brt_[A-Za-z0-9._~-]+", "rt_[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED_JWT]", text)
    return text[:1000]


def _timed_out_health_result(email: str, reason: str) -> dict[str, Any]:
    return _timed_out_health_result_impl(email, reason, mark_quota=mark_quota_status)


def _relogin_guard_path():
    return runtime_file(CFG, "account_relogin_guard.json")


def _relogin_cooldown_seconds() -> int:
    return _relogin_cooldown_seconds_impl(CFG)


def _relogin_cooldown_active(account: dict[str, Any]) -> bool:
    return _relogin_cooldown_active_impl(
        account, guard_path=_relogin_guard_path(), cooldown_seconds=_relogin_cooldown_seconds()
    )


def _relogin_dead_end_permanent() -> bool:
    return _relogin_dead_end_permanent_impl(CFG)


def _relogin_repeat_cooldown_seconds() -> int:
    return _relogin_repeat_cooldown_seconds_impl(CFG)


def _clear_relogin_failure(email: str) -> None:
    _clear_relogin_failure_impl(email, guard_path=_relogin_guard_path())


def _record_relogin_failure(email: str, relogin: dict[str, Any]) -> None:
    _record_relogin_failure_impl(
        email,
        relogin,
        guard_path=_relogin_guard_path(),
        cooldown_seconds=_relogin_cooldown_seconds(),
        repeat_cooldown_seconds=_relogin_repeat_cooldown_seconds(),
        dead_end_permanent=_relogin_dead_end_permanent(),
    )


def _relogin_failure_quota_status(relogin: dict[str, Any]) -> str:
    if _NATIVE_RELOGIN_FAILURE_QUOTA_STATUS is not None and isinstance(relogin, (dict, type(None))):
        try:
            return str(_NATIVE_RELOGIN_FAILURE_QUOTA_STATUS(relogin))
        except (TypeError, ValueError):
            pass
    text = json.dumps(relogin or {}, ensure_ascii=False).lower()
    if "account_deactivated" in text or "deleted or deactivated" in text:
        return "account_deactivated"
    if "add_phone" in text or "phone_verification" in text:
        return "phone_verification_required"
    if "mailbox_transport" in text or "mailbox_transport_unavailable" in text:
        return "relogin_mailbox_transport_failed"
    if "mailbox_auth_invalid" in text:
        return "mailbox_auth_invalid"
    if _relogin_spent_email_code(relogin):
        return "relogin_otp_failed"
    if "cooldown" in text:
        return "relogin_cooldown"
    return "relogin_failed"
