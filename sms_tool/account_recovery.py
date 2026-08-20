"""Local account liveness refresh and ordered AT recovery workflows.

The liveness probe itself is side-effect free and lives in
``account_liveness``. This module owns verified persistence, deactivation
handling, and the protocol recovery chain (OAuth refresh token, existing
ChatGPT cookie session, protocol email-OTP login, then Codex OAuth PKCE).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .account_liveness import probe_account_liveness
from .config import CFG
from .storage import (
    clear_stale_promotion_at_marker,
    get_account_record,
    list_paypal_accounts,
    mark_quota_status,
    upsert_account,
)


def refresh_local_quota_statuses(
    emails: list[str] | None = None,
    workers: int = 4,
    proxy: str | None = None,
    timeout: int = 30,
    relogin_on_401: bool = False,
    relogin_timeout: int = 180,
    relogin_mode: str = "auto",
) -> dict[str, Any]:
    accounts = _local_quota_accounts(emails)
    run_id = uuid.uuid4().hex
    _emit_account_batch_event(
        run_id,
        "batch_started",
        "running",
        total=len(accounts),
        detail="账号测活开始",
    )
    # The liveness probe is a single light GET, so a modestly higher ceiling keeps
    # a full-pool scan responsive; heavy 401 relogins only run for invalid tokens.
    max_workers = max(1, min(int(workers or 1), 16, len(accounts) or 1))
    ordered: list[dict[str, Any] | None] = [None] * len(accounts)

    def run(index: int, account: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        email = str(account.get("email") or "").strip()
        try:
            if is_permanently_deactivated(account):
                probe = {
                    "ok": False,
                    "mode": "local",
                    "status": "account_deactivated",
                    "quota_status": "account_deactivated",
                    "error": "account_deactivated",
                    "terminal": True,
                }
            else:
                probe = probe_account_liveness(account, proxy=proxy, timeout=timeout)
            relogin: dict[str, Any] = {}
            if relogin_on_401 and _probe_is_token_invalid(probe) and email:
                relogin = relogin_codex_account(
                    account,
                    proxy=proxy,
                    timeout=max(int(relogin_timeout or timeout or 180), int(timeout or 30)),
                    mode=relogin_mode,
                )
                if relogin.get("ok"):
                    probe = dict(relogin.get("probe") or {})
                    if email:
                        try:
                            clear_stale_promotion_at_marker(email)
                        except Exception:
                            pass
            status = str(probe.get("quota_status") or probe.get("status") or "未知")
            if relogin and not relogin.get("ok"):
                status = _relogin_failure_quota_status(relogin)
            persisted = mark_quota_status(email, status, quota_result=probe) if email else False
            probe_ok = bool(probe.get("ok"))
            result = {
                "ok": probe_ok and bool(persisted),
                "email": email,
                "quota_status": status,
                "probe": probe,
                **({"relogin": relogin} if relogin else {}),
                "probe_ok": probe_ok,
                "persisted": bool(persisted),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "email": email,
                "quota_status": "检测失败",
                "probe": {"ok": False, "error": str(exc)[:200]},
                "probe_ok": False,
                "persisted": False,
            }
        _emit_account_batch_event(
            run_id,
            "account_completed",
            "completed" if result.get("ok") else "failed",
            account_ref=email,
            total=len(accounts),
            detail=str(result.get("quota_status") or "检测完成"),
        )
        return index, result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run, index, account) for index, account in enumerate(accounts)]
        for future in as_completed(futures):
            index, result = future.result()
            ordered[index] = result
    results = [item for item in ordered if item is not None]
    success = sum(1 for item in results if item.get("ok"))
    persisted = sum(1 for item in results if item.get("persisted"))
    account_deactivated = sum(1 for item in results if _item_is_account_deactivated(item))
    at_invalid = sum(
        1
        for item in results
        if not _item_is_account_deactivated(item) and _probe_is_token_invalid(item.get("probe"))
    )
    probe_failed = sum(
        1
        for item in results
        if not item.get("probe_ok")
        and not _item_is_account_deactivated(item)
        and not _probe_is_token_invalid(item.get("probe"))
    )
    relogin_results = [item.get("relogin") for item in results if isinstance(item.get("relogin"), dict)]
    relogin_success = sum(1 for item in relogin_results if item.get("ok"))
    relogin_deactivated = sum(1 for item in relogin_results if _looks_account_deactivated(item))
    _emit_account_batch_event(
        run_id,
        "batch_completed",
        "completed",
        total=len(results),
        detail=f"完成 {len(results)} 个账号",
    )
    return {
        "ok": success == len(results),
        "mode": "local",
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
        "persisted": persisted,
        "persist_failed": len(results) - persisted,
        "at_invalid": at_invalid,
        "account_deactivated": account_deactivated,
        "probe_failed": probe_failed,
        "relogin_attempted": len(relogin_results),
        "relogin_success": relogin_success,
        "relogin_failed": len(relogin_results) - relogin_success,
        "relogin_account_deactivated": relogin_deactivated,
        "results": results,
    }


def _emit_account_batch_event(
    run_id: str,
    stage: str,
    status: str,
    *,
    account_ref: str = "",
    total: int = 0,
    detail: str = "",
) -> None:
    try:
        from .desktop_ipc import emit_event

        emit_event({
            "domain": "account_scan",
            "run_id": run_id,
            "account_ref": account_ref,
            "stage": stage,
            "status": status,
            "total": int(total or 0),
            "detail": detail,
        })
    except Exception:
        pass


def relogin_web_session_account(account: dict[str, Any], proxy: str | None = None, timeout: int = 180) -> dict[str, Any]:
    """Refresh a web access token from an existing ChatGPT session cookie."""
    if not isinstance(account, dict):
        return {"ok": False, "mode": "web_session", "error": "invalid_account"}
    email = str(account.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "mode": "web_session", "error": "missing_email"}
    try:
        from .session_refresh import _refresh_session_protocol

        data = dict(account)
        data["email"] = email
        result = dict(_refresh_session_protocol(
            data,
            str(account.get("json_path") or ""),
            email,
            max(30, int(timeout or 180)),
            proxy=proxy,
            persist=False,
        ) or {})
        if not result.get("ok"):
            safe = _safe_relogin_result(result)
            safe.update({"ok": False, "mode": "web_session"})
            return safe
        return _verify_and_persist_candidate(
            account,
            result.get("data") if isinstance(result.get("data"), dict) else {},
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
    from .codex_export import _openai_refresh_token, _refresh_with_openai_oauth

    auth_session = account.get("auth_session") if isinstance(account.get("auth_session"), dict) else {}
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
    candidate.update(result.get("data") if isinstance(result.get("data"), dict) else {})
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


def relogin_chatgpt_email_account(
    account: dict[str, Any],
    proxy: str | None = None,
    timeout: int = 300,
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

        from .account_creation import _auth_session_access_token, _fetch_auth_session
        from .auth_flow import _json_or_raw
        from .auth_headers import auth_impersonate, openai_auth_headers, select_auth_fingerprint
        from .codex_oauth import _mailbox_from_data
        from .http_client import request_with_retry
        from .registration import _login_existing_account_with_email_otp
        from .sentinel_tokens import _extract_sentinel, _sentinel_device_id, _set_oai_did_cookie
        from .session_refresh import _auth_session_email

        mailbox = _mailbox_from_data(account)
        if mailbox is None:
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_mailbox"}

        select_auth_fingerprint(rotate=True)
        sentinel = _extract_sentinel(proxy=proxy, force_fresh=True, persist=False)
        if not isinstance(sentinel, dict) or not sentinel.get("sentinel_token"):
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "sentinel_extract_failed"}

        chat_cfg = CFG.get("chatgpt") if isinstance(CFG.get("chatgpt"), dict) else {}
        auth_base = str(chat_cfg.get("auth_base_url") or "https://auth.openai.com").rstrip("/")
        chat_base = str(chat_cfg.get("chat_base_url") or "https://chatgpt.com").rstrip("/")
        device_id = _sentinel_device_id(sentinel) or str(uuid.uuid4())
        logging_id = str(uuid.uuid4()).replace("-", "")
        session = curl_requests.Session()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        _set_oai_did_cookie(session, device_id)
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
            sentinel_token=str(sentinel.get("sentinel_token") or ""),
            sentinel_so_token=str(sentinel.get("sentinel_so_token") or ""),
            totp_secret=str(account.get("totp_secret") or ""),
        )
        if not login.get("ok"):
            return {
                "ok": False,
                "mode": "chatgpt_email_otp",
                "error": _redact_recovery_error(login.get("error") or "email_login_failed"),
            }

        auth_result = _fetch_auth_session(session, chat_base, base_headers)
        auth_session = auth_result.get("body") if isinstance(auth_result.get("body"), dict) else {}
        access_token = str(_auth_session_access_token(auth_session) or "").strip()
        if not access_token:
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "auth_session_missing_access_token"}
        authenticated_email = _auth_session_email(auth_session)
        if not authenticated_email:
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "auth_session_missing_email"}
        if authenticated_email != email:
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "auth_session_email_mismatch"}

        candidate = dict(account)
        candidate.update({
            "email": email,
            "device_id": device_id,
            "access_token": access_token,
            "auth_session": auth_session,
            "cookie_header": str(auth_result.get("cookie_header") or ""),
            "refresh_token_status": "no_rt",
        })
        return _verify_and_persist_candidate(
            account,
            candidate,
            mode="chatgpt_email_otp",
            proxy=proxy,
            timeout=timeout,
        )
    except Exception as exc:
        return {
            "ok": False,
            "mode": "chatgpt_email_otp",
            "error": _redact_recovery_error(exc),
        }


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
    normalized_mode = _normalize_relogin_mode(mode)
    if normalized_mode == "web_session":
        return relogin_web_session_account(account, proxy=proxy, timeout=timeout)
    if normalized_mode == "codex_oauth":
        return relogin_local_codex_account(account, proxy=proxy, timeout=timeout)

    recovery_proxy, proxy_attempts = _select_recovery_proxy(account, proxy)
    attempts: list[dict[str, Any]] = []
    strategies = (
        ("oauth_refresh_token", relogin_refresh_token_account, timeout),
        ("web_session", relogin_web_session_account, min(max(15, int(timeout or 180)), 30)),
        ("chatgpt_email_otp", relogin_chatgpt_email_account, timeout),
        ("codex_oauth_pkce", relogin_local_codex_account, timeout),
    )
    for strategy, handler, strategy_timeout in strategies:
        result = dict(handler(account, proxy=recovery_proxy, timeout=strategy_timeout) or {})
        if result.get("ok"):
            success = _safe_relogin_result(result)
            success["attempts"] = attempts
            if proxy_attempts:
                success["proxy_attempts"] = proxy_attempts
            return success
        attempt = _safe_relogin_result(result)
        attempt.setdefault("mode", strategy)
        attempts.append(attempt)
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
        from .codex_oauth import _save_oauth_tokens, refresh_codex_oauth_session

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

        tokens = result.get("tokens") if isinstance(result.get("tokens"), dict) else {}
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
            safe.update({
                "ok": False,
                "error": f"oauth_access_token_probe_failed:{probe.get('status_code') or 'unknown'}",
                "probe": probe,
                "persisted": False,
            })
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
        safe.update({"ok": True, "probe": probe, "persisted": True})
        return safe
    except Exception as exc:
        return {"ok": False, "error": _redact_recovery_error(exc)}


def _verify_and_persist_candidate(
    account: dict[str, Any],
    candidate: dict[str, Any],
    *,
    mode: str,
    proxy: str | None,
    timeout: int,
) -> dict[str, Any]:
    email = str(candidate.get("email") or account.get("email") or "").strip().lower()
    access_token = str(candidate.get("access_token") or "").strip()
    if not access_token:
        return {"ok": False, "mode": mode, "error": f"{mode}_missing_access_token", "persisted": False}

    verified = dict(account)
    verified.update(candidate)
    verified["email"] = email
    if mode == "web_session":
        from .session_refresh import _auth_session_email

        auth_session = verified.get("auth_session") if isinstance(verified.get("auth_session"), dict) else {}
        authenticated_email = _auth_session_email(auth_session)
        if not authenticated_email:
            return {"ok": False, "mode": mode, "error": "auth_session_missing_email", "persisted": False}
        if authenticated_email != email:
            return {"ok": False, "mode": mode, "error": "auth_session_email_mismatch", "persisted": False}
    probe = probe_account_liveness(
        verified,
        proxy=proxy,
        timeout=min(max(10, int(timeout or 30)), 60),
    )
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
    from .session_refresh import _save_refreshed

    saved_path = _save_refreshed(verified, json_path)
    return {
        "ok": True,
        "mode": mode,
        "email": email,
        "json_path": saved_path,
        "probe": probe,
        "persisted": True,
        "refresh_token_status": str(verified.get("refresh_token_status") or "no_rt"),
    }


def _mark_successful_relogin(data: dict[str, Any], probe: dict[str, Any], *, now: int | None = None) -> None:
    """Replace stale 401 metadata after a newly acquired AT passes HTTP 200."""
    timestamp = int(now or time.time())
    data["success"] = True
    if str(data.get("status") or "").strip().lower() in {
        "at_invalid",
        "access_token_invalid",
        "token_invalidated",
    }:
        data["status"] = "registered"
    error = str(data.get("error") or "").strip().lower()
    if any(marker in error for marker in (
        "401",
        "unauthorized",
        "token_invalid",
        "token_expired",
        "could not validate your token",
        "oauth_refresh_http_401",
    )):
        data.pop("error", None)
    # A previous promotion probe can persist "AT失效". A verified replacement
    # AT makes that marker stale; keep its detailed result for later inspection
    # but stop surfacing the authentication failure in the account list.
    if str(data.get("promotion_status") or "").strip() == "AT失效":
        data["promotion_status"] = ""
    promotion = data.get("promotion") if isinstance(data.get("promotion"), dict) else {}
    if str(promotion.get("status") or "").strip() == "AT失效":
        promotion["status"] = ""
        data["promotion"] = promotion
    account_scan = data.get("account_scan") if isinstance(data.get("account_scan"), dict) else {}
    account_scan.update({
        "ok": True,
        "scan_status": "alive",
        "token_probe": _safe_relogin_result(probe),
    })
    data["account_scan"] = account_scan
    data["account_scan_status"] = "alive"
    data["account_scan_updated_at"] = timestamp
    # A verified replacement AT must also clear the quota-side 401 marker.
    # Otherwise JIT payment/account-pool filters continue to reject the account
    # even though the newly persisted token has passed the canonical probe.
    quota = data.get("quota") if isinstance(data.get("quota"), dict) else {}
    quota_status = str(probe.get("quota_status") or "").strip()
    if not quota_status or quota_status in {"401失效", "token_invalid", "HTTP 401"}:
        quota_status = "可用"
    quota["status"] = quota_status
    quota["updated_at"] = timestamp
    quota["last_result"] = {
        key: value
        for key, value in _safe_relogin_result(probe).items()
        if key not in {"body", "access_token", "authorization", "cookie", "cookie_header"}
    }
    data["quota"] = quota
    data["quota_status"] = quota_status
    data["quota_updated_at"] = timestamp


def _select_recovery_proxy(account: dict[str, Any], proxy: str | None) -> tuple[str | None, list[dict[str, Any]]]:
    country = str(account.get("registration_country") or "").strip().upper()
    if not country:
        return proxy, []
    proxy_cfg = CFG.get("proxy") if isinstance(CFG.get("proxy"), dict) else {}
    configured = proxy_cfg.get("pool") or []
    if isinstance(configured, str):
        configured = [configured]
    candidates = [
        value
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
        from .paypal_proxy import select_proxy_from_pool

        selected, attempts = select_proxy_from_pool(candidates, country, "account_recovery")
        return (selected or proxy or str(candidates[0])), attempts
    except Exception as exc:
        return proxy or str(candidates[0]), [{
            "ok": False,
            "stage": "account_recovery",
            "expected_country": country,
            "error": _redact_recovery_error(exc)[:200],
        }]


def is_permanently_deactivated(account: dict[str, Any]) -> bool:
    if not isinstance(account, dict):
        return False
    values = [account.get("status"), account.get("error"), account.get("account_scan_status")]
    terminal = account.get("terminal_failure")
    if isinstance(terminal, dict):
        values.extend((terminal.get("code"), terminal.get("reason")))
    raw_json = str(account.get("raw_json") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                values.extend((parsed.get("status"), parsed.get("error"), parsed.get("account_scan_status")))
        except Exception:
            pass
    return _looks_account_deactivated(values)


def _local_quota_accounts(emails: list[str] | None) -> list[dict[str, Any]]:
    requested = [_normalize_email(email) for email in (emails or []) if _normalize_email(email)]
    if not requested:
        requested = [
            _normalize_email(row.get("email"))
            for row in list_paypal_accounts()
            if _normalize_email(row.get("email"))
        ]
    accounts = []
    seen = set()
    for email in requested:
        if email in seen:
            continue
        seen.add(email)
        record = get_account_record(email)
        accounts.append(_local_account_data(record) if record else {"email": email})
    return accounts


def _local_account_data(record: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    raw_json = str((record or {}).get("raw_json") or "")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                data.update(parsed)
        except Exception:
            pass
    for key, value in (record or {}).items():
        if value not in (None, ""):
            data[key] = value
    return data


def _persist_permanent_deactivation(account: dict[str, Any], result: dict[str, Any] | None = None) -> bool:
    del result
    data = _local_account_data(account)
    email = str(data.get("email") or "").strip().lower()
    if not email:
        return False
    now = int(time.time())
    data.update({
        "email": email,
        "success": False,
        "status": "account_deactivated",
        "error": "account_deactivated",
        "account_scan_status": "account_deactivated",
        "terminal_failure": {
            "code": "account_deactivated",
            "reason": "account_deactivated",
            "updated_at": now,
        },
    })
    json_path = str(data.get("json_path") or account.get("json_path") or "").strip()
    if json_path:
        try:
            Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return upsert_account(data, json_path=json_path)


def _safe_relogin_result(result: dict[str, Any] | None) -> dict[str, Any]:
    blocked = {
        "tokens", "access_token", "id_token", "refresh_token", "oauth_refresh_token",
        "data", "auth_session", "cookie_header", "password", "mailbox", "raw_json",
    }
    safe: dict[str, Any] = {}
    for key, value in dict(result or {}).items():
        if key in blocked:
            continue
        safe[key] = _redact_recovery_error(value) if key in {"error", "message", "last_url"} else value
    return safe


def _redact_recovery_error(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"((?:https?|socks5h?)://)[^@\s/]+@", r"\1[REDACTED]@", text, flags=re.I)
    text = re.sub(r"\brt_[A-Za-z0-9._~-]+", "rt_[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED_JWT]", text)
    return text[:1000]


def _looks_account_deactivated(value: Any) -> bool:
    text = json.dumps(value or {}, ensure_ascii=False).lower()
    return any(marker in text for marker in (
        "account_deactivated",
        "account_deatived",
        "deleted or deactivated",
        "account has been deleted",
        "account has been deactivated",
    ))


def _probe_is_token_invalid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        status_code = int(value.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    return status_code == 401 or str(value.get("status") or "").strip().lower() == "token_invalid"


def _item_is_account_deactivated(value: Any) -> bool:
    if not isinstance(value, dict):
        return _looks_account_deactivated(value)
    return _looks_account_deactivated(value.get("probe")) or _looks_account_deactivated(value.get("relogin"))


def _normalize_relogin_mode(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"web", "web_session", "session", "chatgpt_session"}:
        return "web_session"
    if text in {"codex", "codex_oauth", "oauth", "pkce"}:
        return "codex_oauth"
    return "auto"


def _relogin_failure_quota_status(relogin: dict[str, Any]) -> str:
    text = json.dumps(relogin or {}, ensure_ascii=False).lower()
    if "account_deactivated" in text or "deleted or deactivated" in text:
        return "账号停用"
    if "add_phone" in text or "phone_verification" in text:
        return "需手机验证"
    if "mailbox" in text or "email_otp" in text or "otp" in text:
        return "收信/OTP失败"
    return "重登失败"


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()
