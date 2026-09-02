"""会话与账号产物提取（access token 探测、session payload、诊断、TOTP 绑定）。依赖 dom_fields。"""

from __future__ import annotations

import logging
import os
import random
import time

from .dom_fields import _safe_text, _session_context_closed, _session_error_marker, _terminal_session_error

from ...account_liveness import CODEX_USAGE_URL, account_chatgpt_id, quota_result_from_payload
from ..base import BrowserRegistrationError
from ..browser_session import PlaywrightBrowserSession
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)


def _browser_access_token_probe(browser: Any, account: Mapping[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    """Probe usage through the connected browser's own network exit.

    Cloud browser providers can terminate traffic in a different country than
    the worker process.  The browser-context request keeps registration and
    the post-registration AT check on the same egress.
    """
    token = str(account.get("access_token") or "").strip()
    if not token:
        return quota_result_from_payload(
            {"status_code": 0, "body": {"error": "missing_access_token"}},
            status_code=0,
            mode="browser",
            transport_ok=False,
        )
    if os.environ.get("CAMOUFOX_PROBE_TRACE"):
        try:
            page_url = str(getattr(getattr(browser, "page", None), "url", "") or "")
        except Exception:
            page_url = "<unknown>"
        logger.warning(
            "[AT_PROBE_TRACE] driver=%s page_url=%s target=%s",
            type(getattr(browser, "driver", browser)).__name__,
            page_url,
            CODEX_USAGE_URL,
        )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    account_id = account_chatgpt_id(account)
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    try:
        try:
            payload = browser.fetch_json(
                CODEX_USAGE_URL,
                timeout_ms=max(5_000, int(timeout or 30) * 1_000),
                headers=headers,
            )
        except TypeError as exc:
            # Compatibility for small third-party browser adapters that have
            # not adopted the optional headers parameter yet.
            if "headers" not in str(exc):
                raise
            payload = browser.fetch_json(
                CODEX_USAGE_URL,
                timeout_ms=max(5_000, int(timeout or 30) * 1_000),
            )
        status_code = int(payload.get("status") or payload.get("status_code") or 0) if isinstance(payload, Mapping) else 0
        if os.environ.get("CAMOUFOX_PROBE_TRACE"):
            logger.warning(
                "[AT_PROBE_TRACE] raw payload status=%s body=%s",
                status_code,
                str(payload.get("body"))[:300] if isinstance(payload, Mapping) else payload,
            )
        result = quota_result_from_payload(
            payload,
            status_code=status_code,
            mode="browser",
            account_id=account_id,
            transport_ok=200 <= status_code < 300,
        )
        error = str(result.get("error") or "")
        if token and token in error:
            error = error.replace(token, "[REDACTED]")
        result["error"] = _safe_text(error)
        return result
    except Exception as exc:
        return {
            "ok": False,
            "mode": "browser",
            "status": "unknown",
            "quota_status": "检测失败",
            "status_code": 0,
            "error": _safe_text(type(exc).__name__),
            **({"account_id": account_id} if account_id else {}),
        }


def _session_payload(
    session: PlaywrightBrowserSession,
    chat_base: str,
    email: str,
    *,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    # Selenium/Roxy performs the callback/window selection inside fetch_json;
    # keep the generic session loop origin-safe by letting that adapter own the
    # final navigation instead of forcing a second page.goto here.
    deadline = time.monotonic() + max(1, int(timeout_seconds or 90))
    last_status = 0
    last_body_keys: list[str] = []
    last_fetch_error = ""
    last_error_marker = ""
    consecutive_closed = 0
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        remaining_ms = max(1_000, min(5_000, int(max(0.1, deadline - time.monotonic()) * 1_000)))
        try:
            session_url = f"{chat_base.rstrip('/')}/api/auth/session"
            try:
                payload = session.fetch_json(session_url, timeout_ms=remaining_ms)
            except TypeError as exc:
                # Keep compatibility with small third-party adapters that still
                # expose the pre-timeout fetch_json(url) signature.
                if "timeout_ms" not in str(exc):
                    raise
                payload = session.fetch_json(session_url)
            last_fetch_error = ""
        except BrowserRegistrationError:
            raise
        except Exception as exc:
            last_fetch_error = type(exc).__name__
            if _session_context_closed(f"{type(exc).__name__}: {exc}"):
                consecutive_closed += 1
                if consecutive_closed >= 2:
                    raise BrowserRegistrationError("browser_session_context_closed", last_fetch_error) from exc
            else:
                consecutive_closed = 0
            select_page = getattr(session, "select_live_page", None)
            if callable(select_page):
                try:
                    select_page()
                except Exception:
                    pass
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1, remaining))
            continue
        last_status = int(payload.get("status") or 0) if isinstance(payload, dict) else 0
        body = payload.get("body") if isinstance(payload, dict) else {}
        if not isinstance(body, dict):
            body = {}
        last_body_keys = sorted(str(key) for key in body.keys())[:30]
        error_marker = _session_error_marker(body)
        last_error_marker = error_marker
        if _session_context_closed(error_marker):
            consecutive_closed += 1
            if consecutive_closed >= 2:
                raise BrowserRegistrationError("browser_session_context_closed", f"http_{last_status or 'unknown'}")
        else:
            consecutive_closed = 0
        terminal_error = _terminal_session_error(last_status, error_marker)
        if terminal_error:
            raise BrowserRegistrationError(terminal_error, f"http_{last_status or 'unknown'}")
        candidate = body.get("session") if isinstance(body.get("session"), dict) else body
        access_token = str(
            candidate.get("accessToken") or candidate.get("access_token") or body.get("accessToken") or ""
        ).strip()
        user = candidate.get("user") if isinstance(candidate.get("user"), dict) else body.get("user")
        user_email = str((user or {}).get("email") or "").strip().lower()
        if access_token:
            if user_email and user_email != email.lower():
                raise BrowserRegistrationError("browser_session_email_mismatch")
            return {
                "body": body,
                "access_token": access_token,
                "id_token": str(candidate.get("idToken") or candidate.get("id_token") or ""),
                "user": user or {},
                "status_code": last_status,
            }
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1, remaining))
    details = f"http_{last_status or 'unknown'}"
    if last_body_keys:
        details += ":keys=" + ",".join(last_body_keys)
    details += f":attempts={attempts}"
    if last_fetch_error:
        details += ":error=" + last_fetch_error
    state_fn = getattr(session, "context_state", None)
    if callable(state_fn):
        try:
            state = state_fn()
            details += ":host=" + str(state.get("current_host") or "")
            details += ":session_cookie=" + ("present" if state.get("session_cookie_present") else "absent")
        except Exception:
            pass
    if "chatgpt_context_unavailable" in last_error_marker:
        code = "browser_chatgpt_context_unavailable"
    elif last_status == 429:
        code = "browser_session_rate_limited"
    elif last_status >= 500:
        code = "browser_session_unavailable"
    elif last_status and last_status != 200:
        code = "browser_session_http_error"
    elif last_fetch_error and not last_status:
        code = "browser_session_request_failed"
    else:
        code = "browser_session_access_token_missing"
    raise BrowserRegistrationError(code, details)


def _browser_diagnostics(page: Any, driver: str) -> dict[str, Any]:
    diagnostics = {"driver": driver, "url_host": "", "title": ""}
    try:
        diagnostics["url_host"] = str(urlsplit(str(page.url or "")).hostname or "")
    except Exception:
        pass
    try:
        diagnostics["title"] = _safe_text(page.title())[:120]
    except Exception:
        pass
    return diagnostics


def _safe_proxy_audit(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only non-sensitive proxy-pool audit fields in registration output."""
    value = metadata if isinstance(metadata, Mapping) else {}
    return {
        "pool_index": int(value.get("pool_index", -1) or -1),
        "expected_country": str(value.get("expected_country") or "").strip().upper(),
        "actual_country": str(value.get("actual_country") or "").strip().upper(),
        "scheme": str(value.get("scheme") or "").strip().lower(),
    }


def _post_registration_dwell(config: Mapping[str, Any]) -> float:
    """Keep the connected browser alive briefly after the first good AT probe."""
    registration = config.get("registration") if isinstance(config, Mapping) else {}
    registration = registration if isinstance(registration, Mapping) else {}
    raw = str(registration.get("post_registration_dwell_seconds_range") or "0,0")
    try:
        values = [float(item.strip()) for item in raw.replace(";", ",").split(",") if item.strip()]
        lo = values[0] if values else 0.0
        hi = values[1] if len(values) > 1 else lo
    except (TypeError, ValueError):
        lo = hi = 0.0
    lo, hi = max(0.0, lo), max(0.0, hi)
    if hi < lo:
        lo, hi = hi, lo
    seconds = random.uniform(lo, hi) if hi > lo else lo
    if seconds > 0:
        time.sleep(min(300.0, seconds))
    return seconds


def _browser_failure_class(code: str) -> str:
    value = str(code or "").lower()
    if any(marker in value for marker in ("rate_limited", "rate_limit")):
        return "rate_limit"
    if any(marker in value for marker in ("existing_account", "session_account_not_linked")):
        return "account"
    if any(marker in value for marker in ("otp_timeout", "otp_rejected", "mailbox", "email_otp")):
        return "mailbox"
    if any(marker in value for marker in (
        "identity_provider", "manual_challenge", "session_email_mismatch",
        "session_access_token_missing", "session_unauthorized", "session_forbidden",
        "session_access_denied", "session_oauth_callback_failed", "session_token_refresh_failed",
        "chatgpt_context_unavailable", "profile_", "passwordless_otp", "otp_restart_state",
        "registration_state_unknown", "auth_state",
    )):
        return "auth_state"
    if "proxy_blocked" in value or "proxy_country_mismatch" in value:
        return "network"
    if any(marker in value for marker in (
        "dependency_missing", "api_key_missing", "workspace_id_missing", "profile_create_failed",
        "debug_address_missing", "session_endpoint_unavailable", "config", "unsupported",
    )):
        return "configuration"
    return "network"


def _bind_totp_in_browser(page: Any, access_token: str, device_id: str) -> dict[str, Any]:
    """Enroll and activate TOTP 2FA through the browser's fetch API.

    Routes the MFA enroll and activate HTTP requests through
    ``page.evaluate(fetch(...))`` so they carry the browser's real
    cookies, fingerprint, and Cloudflare clearance.  Returns a dict
    with ``ok``, ``totp_secret``, and optionally ``error``.
    """
    chat_base = "https://chatgpt.com"
    enroll_script = """
    async ([url, accessToken, deviceId]) => {
        const r = await fetch(url, {
            method: "POST",
            headers: {
                "Authorization": "Bearer " + accessToken,
                "oai-device-id": deviceId,
                "oai-language": "en-US",
                "Content-Type": "application/json",
                "Referer": "https://chatgpt.com/",
            },
            credentials: "include",
            body: JSON.stringify({"factor_type": "totp"}),
        });
        const data = await r.json().catch(() => ({}));
        return {status: r.status, body: data};
    }
    """
    activate_script = """
    async ([url, accessToken, deviceId, code, sessionId]) => {
        const r = await fetch(url, {
            method: "POST",
            headers: {
                "Authorization": "Bearer " + accessToken,
                "oai-device-id": deviceId,
                "oai-language": "en-US",
                "Content-Type": "application/json",
                "Referer": "https://chatgpt.com/",
            },
            credentials: "include",
            body: JSON.stringify({"code": code, "factor_type": "totp", "session_id": sessionId}),
        });
        const data = await r.json().catch(() => ({}));
        return {status: r.status, body: data};
    }
    """
    try:
        enroll_result = page.evaluate(
            enroll_script,
            [f"{chat_base}/backend-api/accounts/mfa/enroll", access_token, device_id],
        )
        if not isinstance(enroll_result, dict) or enroll_result.get("status") != 200:
            error_detail = str(enroll_result)[:300] if enroll_result else "no response"
            return {"ok": False, "error": f"browser_totp_enroll_failed: {error_detail}"}
        enroll_body = enroll_result.get("body") or {}
        secret = str(enroll_body.get("secret") or "").strip()
        session_id = str(enroll_body.get("session_id") or "").strip()
        if not secret or not session_id:
            return {"ok": False, "error": "browser_totp_enroll_missing_fields"}
        # Generate TOTP code and activate
        try:
            import pyotp
            totp_code = pyotp.TOTP(secret).now()
        except ImportError:
            return {"ok": False, "error": "pyotp_not_installed", "totp_secret": secret}

        activate_result = page.evaluate(
            activate_script,
            [f"{chat_base}/backend-api/accounts/mfa/user/activate_enrollment", access_token, device_id, totp_code, session_id],
        )
        if not isinstance(activate_result, dict) or activate_result.get("status") != 200:
            error_detail = str(activate_result)[:300] if activate_result else "no response"
            return {"ok": False, "error": f"browser_totp_activate_failed: {error_detail}", "totp_secret": secret}
        activate_body = activate_result.get("body") or {}
        if not activate_body.get("success"):
            return {"ok": False, "error": f"browser_totp_activate_not_successful: {activate_body}", "totp_secret": secret}
        return {"ok": True, "totp_secret": secret}
    except Exception as exc:
        return {"ok": False, "error": f"browser_totp_exception: {type(exc).__name__}: {exc}"}
