"""Local account liveness refresh and ordered AT recovery workflows.

The liveness probe itself is side-effect free and lives in
``account_liveness``. This module owns verified persistence, deactivation
handling, and the protocol recovery chain (OAuth refresh token, existing
ChatGPT cookie session, protocol email-OTP login, then Codex OAuth PKCE).
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .account_identity import account_identity, resolve_account_proxy
from .account_liveness import browser_fetch_for_account, probe_account_liveness
from .config import CFG
from .http_client import is_transient_transport_error
from .proxy_routing import proxy_pool_for
from .paths import runtime_file
from .storage import (
    clear_stale_promotion_at_marker,
    get_account_record,
    list_paypal_accounts,
    mark_quota_status,
    upsert_account,
)
from .mailbox_quarantine import mailbox_relogin_allowed
from .providers.mailbox_graph import MailboxAuthInvalidError


def refresh_local_quota_statuses(
    emails: list[str] | None = None,
    workers: int = 4,
    proxy: str | None = None,
    timeout: int = 30,
    relogin_on_401: bool = False,
    relogin_timeout: int = 180,
    relogin_mode: str = "auto",
    batch_timeout: int = 900,
    account_timeout: int = 360,
) -> dict[str, Any]:
    if relogin_on_401:
        _refresh_mailbox_quarantine_state()
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
    # A normal liveness probe is a single HTTP request. Browser sessions are a
    # bounded fallback for 401/Cloudflare responses and must not consume the
    # whole batch's worker pool while they start and tear down.
    browser_slots = threading.BoundedSemaphore(max(1, min(2, max_workers)))
    # Recovery is materially heavier than the probe (mailbox/OAuth/browser).
    # Keep it in a separate two-slot lane so enabling the 401 checkbox cannot
    # starve lightweight probes for the rest of the batch.
    relogin_slots = threading.BoundedSemaphore(2)
    batch_deadline = time.monotonic() + max(30, int(batch_timeout or 900))
    ordered: list[dict[str, Any] | None] = [None] * len(accounts)
    snapshot_path = runtime_file(CFG, "account_liveness_batches") / f"{run_id}.json"

    def persist_snapshot(*, terminal: bool = False) -> None:
        """Persist completed rows while workers are still running.

        The WPF process can be killed at its outer deadline; keeping this
        snapshot beside runtime state makes the already completed probes
        recoverable instead of losing the whole batch envelope.
        """
        rows = [item for item in ordered if item is not None]
        timed_out_rows = [item for item in rows if item.get("timed_out")]
        payload = {
            "run_id": run_id,
            "total": len(accounts),
            "completed": max(0, len(rows) - len(timed_out_rows)),
            "unfinished": len(timed_out_rows) + max(0, len(accounts) - len(rows)),
            "partial": bool(timed_out_rows or len(rows) < len(accounts)),
            "terminal": bool(terminal),
            "updated_at": int(time.time()),
            "results": rows,
        }
        try:
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            temp = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temp.replace(snapshot_path)
        except OSError:
            pass

    def run(index: int, account: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        email = str(account.get("email") or "").strip()
        account_deadline = min(batch_deadline, time.monotonic() + max(30, int(account_timeout or 360)))
        try:
            if time.monotonic() >= account_deadline:
                return index, _timed_out_health_result(email, "account_timeout_before_probe")
            probe_timeout = max(5, min(int(timeout or 30), int(account_deadline - time.monotonic())))
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
                # Fast path: use the canonical lightweight quota endpoint for
                # every account, including accounts that have a browser
                # identity. This avoids opening a browser for healthy tokens.
                probe = _probe_liveness_with_retries(
                    account, proxy=proxy, timeout=probe_timeout, browser_fetch=None
                )
                if not isinstance(probe, dict):
                    probe = {
                        "ok": False,
                        "status": "unknown",
                        "quota_status": "检测失败",
                        "error": "invalid_probe_result",
                    }
                browser_identity = account_identity(account).get("browser_identity") or {}
                if browser_identity and _needs_browser_fallback(probe):
                    remaining = max(0.0, account_deadline - time.monotonic())
                    acquired = browser_slots.acquire(timeout=min(1.0, remaining))
                    if acquired:
                        try:
                            with browser_fetch_for_account(account, proxy=proxy, timeout=probe_timeout) as browser_fetch:
                                if browser_fetch is not None:
                                    browser_probe = probe_account_liveness(
                                        account,
                                        proxy=proxy,
                                        timeout=probe_timeout,
                                        browser_fetch=browser_fetch,
                                    )
                                    if browser_probe.get("ok") or int(browser_probe.get("status_code") or 0) != 0:
                                        probe = browser_probe
                                else:
                                    probe = {**probe, "browser_fallback": "unavailable"}
                        finally:
                            browser_slots.release()
                    else:
                        probe = {**probe, "browser_fallback": "concurrency_limited"}
            initial_status = str(probe.get("quota_status") or probe.get("status") or "unknown")
            liveness_401 = _probe_is_token_invalid(probe)
            relogin_attempted = False
            mailbox_auth_invalid = False
            if email and relogin_on_401 and liveness_401:
                # Persist the probe before the optional recovery path. A stuck
                # OTP/browser recovery must never hide the fact that the AT
                # probe already completed.
                mark_quota_status(email, initial_status, quota_result=probe)
            relogin: dict[str, Any] = {}
            if relogin_on_401 and liveness_401 and email and not mailbox_relogin_allowed():
                relogin = {"ok": False, "mode": "disabled", "error": "mailbox_pool_repair_required"}
            elif relogin_on_401 and liveness_401 and email and _relogin_cooldown_active(account):
                relogin = {"ok": False, "mode": "cooldown", "error": "relogin_cooldown"}
            elif relogin_on_401 and liveness_401 and email and time.monotonic() < account_deadline:
                if relogin_slots.acquire(blocking=False):
                    try:
                        relogin_attempted = True
                        remaining = max(30, int(account_deadline - time.monotonic()))
                        relogin = relogin_codex_account(
                            account,
                            proxy=proxy,
                            timeout=min(max(int(relogin_timeout or timeout or 180), int(timeout or 30)), remaining),
                            mode=relogin_mode,
                        )
                        if relogin.get("ok"):
                            probe = dict(relogin.get("probe") or {})
                            if email:
                                try:
                                    _clear_promotion_marker_after_probe(email)
                                except Exception:
                                    pass
                        elif relogin:
                            _record_relogin_failure(email, relogin)
                        mailbox_auth_invalid = str(relogin.get("error") or "") == "mailbox_auth_invalid"
                    finally:
                        relogin_slots.release()
                else:
                    relogin = {"ok": False, "mode": "concurrency_limited", "error": "relogin_concurrency_limited"}
            if time.monotonic() >= account_deadline and not probe.get("ok"):
                probe = {**probe, "status": "timeout", "error": probe.get("error") or "account_timeout"}
            status = str(probe.get("quota_status") or probe.get("status") or "未知")
            if relogin and not relogin.get("ok"):
                status = _relogin_failure_quota_status(relogin)
            persisted = mark_quota_status(email, status, quota_result=probe) if email else False
            if email and isinstance(probe, dict) and probe.get("ok"):
                _clear_promotion_marker_after_probe(email)
            probe_ok = bool(probe.get("ok"))
            result = {
                "ok": probe_ok and bool(persisted),
                "email": email,
                "quota_status": status,
                "probe": probe,
                **({"relogin": relogin} if relogin else {}),
                "probe_ok": probe_ok,
                "persisted": bool(persisted),
                "health_status": _health_status_code(probe, relogin),
                "liveness_401": bool(liveness_401),
                "relogin_attempted": bool(relogin_attempted),
                "mailbox_auth_invalid": bool(mailbox_auth_invalid),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "email": email,
                "quota_status": "检测失败",
                "probe": {"ok": False, "error": str(exc)[:200]},
                "probe_ok": False,
                "persisted": False,
                "health_status": "probe_failed",
                "liveness_401": False,
                "relogin_attempted": False,
                "mailbox_auth_invalid": False,
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

    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = [executor.submit(run, index, account) for index, account in enumerate(accounts)]
    try:
        for future in as_completed(futures, timeout=max(30, int(batch_timeout or 900))):
            index, result = future.result()
            ordered[index] = result
            persist_snapshot()
    except TimeoutError:
        for index, future in enumerate(futures):
            if ordered[index] is None:
                future.cancel()
                ordered[index] = _timed_out_health_result(
                    str(accounts[index].get("email") or "").strip(), "batch_timeout"
                )
        persist_snapshot()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    results = [item for item in ordered if item is not None]
    success = sum(1 for item in results if item.get("ok"))
    persisted = sum(1 for item in results if item.get("persisted"))
    account_deactivated = sum(1 for item in results if _item_is_account_deactivated(item))
    # Count the original liveness result, even when an optional relogin later
    # replaces the probe with a fresh HTTP 200 result.
    at_invalid = sum(1 for item in results if item.get("liveness_401"))
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
    mailbox_auth_invalid = sum(1 for item in results if item.get("mailbox_auth_invalid"))
    relogin_attempted = sum(1 for item in results if item.get("relogin_attempted"))
    _emit_account_batch_event(
        run_id,
        "batch_completed",
        "completed",
        total=len(results),
        detail=f"完成 {sum(1 for item in results if not item.get('timed_out'))} 个账号",
    )
    persist_snapshot(terminal=True)
    return {
        "ok": success == len(results) and len(results) == len(accounts),
        "mode": "local",
        "total": len(results),
        "completed": len(results) - sum(1 for item in results if item.get("timed_out")),
        "success": success,
        "failed": len(results) - success,
        "persisted": persisted,
        "persist_failed": len(results) - persisted,
        "at_invalid": at_invalid,
        "account_deactivated": account_deactivated,
        "probe_failed": probe_failed,
        "relogin_attempted": relogin_attempted,
        "relogin_success": relogin_success,
        "relogin_failed": len(relogin_results) - relogin_success,
        "relogin_account_deactivated": relogin_deactivated,
        "liveness_401": at_invalid,
        "mailbox_auth_invalid": mailbox_auth_invalid,
        "results": results,
        "timed_out": sum(1 for item in results if item.get("timed_out") or item.get("health_status") in {"batch_timeout", "account_timeout"}),
        "batch_timed_out": sum(1 for item in results if item.get("health_status") == "batch_timeout"),
        "account_timed_out": sum(1 for item in results if item.get("health_status") == "account_timeout"),
        "partial": len(results) < len(accounts) or any(item.get("timed_out") for item in results),
        "unfinished": sum(1 for item in results if item.get("timed_out")) + max(0, len(accounts) - len(results)),
        "snapshot_path": str(snapshot_path),
    }


def _probe_liveness_with_retries(
    account: dict[str, Any],
    *,
    proxy: str | None,
    timeout: int,
    browser_fetch: Any = None,
) -> dict[str, Any]:
    """Retry transport-only failures against the configured liveness pool."""
    has_affinity = bool((account.get("identity_context") or {}).get("proxy_affinity"))
    configured = proxy_pool_for(CFG, "liveness")
    candidates = configured[:3] if configured and not has_affinity else [None]
    if not candidates:
        candidates = [proxy]
    last: dict[str, Any] = {}
    for candidate in candidates:
        effective_proxy = None if has_affinity else (candidate or proxy)
        last = probe_account_liveness(
            account, proxy=effective_proxy, timeout=timeout, browser_fetch=browser_fetch
        )
        if int(last.get("status_code") or 0) != 0 or last.get("ok"):
            return last
        if not is_transient_transport_error(last.get("error")):
            return last
    return last


def _needs_browser_fallback(probe: dict[str, Any]) -> bool:
    """Return true only for auth/challenge failures a browser can address."""
    if not isinstance(probe, dict) or probe.get("ok"):
        return False
    try:
        status_code = int(probe.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code in {0, 401, 403} or str(probe.get("status") or "").strip().lower() in {"unknown", "token_invalid"}:
        return True
    error = str(probe.get("error") or "").lower()
    return any(marker in error for marker in ("cloudflare", "challenge", "cf-", "invalid_probe_result"))


def _clear_promotion_marker_after_probe(email: str) -> None:
    """Clear a stale promotion 401 while remaining compatible with test seams."""
    try:
        clear_stale_promotion_at_marker(email, verified_at=int(time.time()))
    except TypeError:
        # Older injected test doubles only accept the original email argument.
        try:
            clear_stale_promotion_at_marker(email)
        except Exception:
            pass
    except Exception:
        pass


def _refresh_mailbox_quarantine_state() -> None:
    """Prune records for credentials removed or replaced in the repaired pool."""
    try:
        from .mailbox import _load_mailbox_pool

        _load_mailbox_pool()
    except Exception:
        pass


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

        from .account_creation import _auth_session_access_token, _fetch_auth_session
        from .auth_flow import _json_or_raw
        from .auth_headers import auth_impersonate, openai_auth_headers, select_auth_fingerprint
        from .codex_oauth import _mailbox_from_data
        from .http_client import request_with_retry
        from .registration import _login_existing_account_with_email_otp
        from .sentinel_tokens import _set_oai_did_cookie
        from .session_refresh import _auth_session_email

        mailbox = _mailbox_from_data(account)
        if mailbox is None:
            return {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_mailbox"}

        select_auth_fingerprint(rotate=True)
        chat_cfg = CFG.get("chatgpt") if isinstance(CFG.get("chatgpt"), dict) else {}
        auth_base = str(chat_cfg.get("auth_base_url") or "https://auth.openai.com").rstrip("/")
        chat_base = str(chat_cfg.get("chat_base_url") or "https://chatgpt.com").rstrip("/")
        device_id = str(account.get("device_id") or uuid.uuid4())
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
            totp_secret=str(account.get("totp_secret") or ""),
            otp_timeout=max(30, int(timeout or 300)),
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
        result = relogin_chatgpt_email_account(account, proxy=recovery_proxy, timeout=timeout)
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
        ("web_session", relogin_web_session_account, min(max(15, int(timeout or 180)), 30)),
        ("chatgpt_email_otp", relogin_chatgpt_email_account, timeout),
        ("codex_oauth_pkce", relogin_local_codex_account, timeout),
        ("browser_session", relogin_browser_session_account, timeout),
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
        from .registration_drivers.external_sessions import create_browser_session
        from .registration_drivers.browser_flow import _wait_for_challenge_clear

        config = CFG.data if hasattr(CFG, "data") else {}
        chat_cfg = config.get("chatgpt", {}) if isinstance(config.get("chatgpt"), dict) else {}
        chat_base = str(chat_cfg.get("chat_base_url") or "https://chatgpt.com").rstrip("/")
        auth_base = str(chat_cfg.get("auth_base_url") or "https://auth.openai.com").rstrip("/")
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
            page.goto(chat_base, wait_until="domcontentloaded", timeout=int(timeout) * 1000)
            # Wait for Cloudflare challenge to clear automatically
            _wait_for_challenge_clear(page, max_wait_seconds=30)
            # Extract session info
            from .registration_drivers.browser_flow import _session_payload
            session_info = _session_payload(browser, chat_base, email, timeout_seconds=timeout)
            auth_body = session_info.get("body") or {}
            access_token = str(session_info.get("access_token") or "").strip()
            if not access_token:
                return {
                    "ok": False,
                    "mode": "browser_session",
                    "error": "browser_session_no_access_token",
                }
            candidate = dict(account)
            candidate.update({
                "email": email,
                "device_id": device_id,
                "access_token": access_token,
                "auth_session": auth_body,
                "cookie_header": str(browser.cookie_header() or ""),
            })
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
    saved_path = json_path
    if persist:
        from .session_refresh import _save_refreshed

        saved_path = _save_refreshed(verified, json_path)
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


def _timed_out_health_result(email: str, reason: str) -> dict[str, Any]:
    """Return a stable result for an account skipped by a health deadline."""
    probe = {
        "ok": False,
        "mode": "local",
        "status": "timeout",
        "quota_status": "health_timeout",
        "error": str(reason or "health_timeout"),
    }
    persisted = mark_quota_status(email, probe["quota_status"], quota_result=probe) if email else False
    return {
        "ok": False,
        "email": email,
        "quota_status": probe["quota_status"],
        "probe": probe,
        "probe_ok": False,
        "persisted": bool(persisted),
        "health_status": "batch_timeout" if reason == "batch_timeout" else "account_timeout",
        "timed_out": True,
        "timeout_reason": str(reason or "health_timeout"),
        "liveness_401": False,
        "relogin_attempted": False,
        "mailbox_auth_invalid": False,
    }


def _relogin_guard_path():
    return runtime_file(CFG, "account_relogin_guard.json")


def _relogin_cooldown_seconds() -> int:
    health = CFG.get("account_health") if isinstance(CFG.get("account_health"), dict) else {}
    try:
        return max(60, int(health.get("relogin_cooldown_seconds") or 1800))
    except (TypeError, ValueError):
        return 1800


def _relogin_cooldown_active(account: dict[str, Any]) -> bool:
    email = _normalize_email(account.get("email"))
    if not email:
        return False
    # Existing pools may only contain a localized/legacy label. OTP is kept as
    # an ASCII marker so known mailbox failures do not immediately loop again.
    quota = account.get("quota") if isinstance(account.get("quota"), dict) else {}
    status = str(account.get("quota_status") or quota.get("status") or "").strip().lower()
    try:
        updated_at = int(account.get("quota_updated_at") or quota.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0
    # Legacy localized OTP labels are only a cooldown signal when they were
    # written inside the current cooldown window. Never turn an old historical
    # failure into a permanent skip.
    if updated_at and time.time() - updated_at < _relogin_cooldown_seconds():
        if status in {"relogin_cooldown", "relogin_otp_failed"} or ("otp" in status and any(marker in status for marker in ("fail", "失", "ʧ"))):
            return True
    try:
        data = json.loads(_relogin_guard_path().read_text(encoding="utf-8"))
        until = float((data.get(email) or {}).get("cooldown_until") or 0)
        return until > time.time()
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _record_relogin_failure(email: str, relogin: dict[str, Any]) -> None:
    if not email or str(relogin.get("mode") or "").lower() == "cooldown":
        return
    text = json.dumps(relogin, ensure_ascii=False).lower()
    if not any(marker in text for marker in ("otp", "mailbox", "email")):
        return
    path = _relogin_guard_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError, TypeError):
        data = {}
    key = _normalize_email(email)
    data[key] = {
        "failure_class": "relogin_otp_failed",
        "last_error": str(relogin.get("error") or "relogin_failed")[:200],
        "cooldown_until": int(time.time() + _relogin_cooldown_seconds()),
        "updated_at": int(time.time()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
    temp.replace(path)


def _health_status_code(probe: dict[str, Any], relogin: dict[str, Any]) -> str:
    if _item_is_account_deactivated({"probe": probe, "relogin": relogin}):
        return "account_deactivated"
    if relogin and not relogin.get("ok"):
        if str(relogin.get("mode") or "") == "cooldown":
            return "relogin_cooldown"
        if str(relogin.get("error") or "") == "mailbox_pool_repair_required":
            return "mailbox_relogin_blocked"
        if str(relogin.get("error") or "") == "mailbox_auth_invalid":
            return "mailbox_auth_invalid"
        if str(relogin.get("error") or "") == "relogin_concurrency_limited":
            return "relogin_concurrency_limited"
        text = json.dumps(relogin, ensure_ascii=False).lower()
        if "mailbox_transport" in text or "mailbox_transport_unavailable" in text:
            return "relogin_mailbox_transport_failed"
        if "otp" in text or "mailbox" in text:
            return "relogin_otp_failed"
        return "relogin_failed"
    if _probe_is_token_invalid(probe):
        return "token_invalid"
    if probe.get("status") == "timeout":
        return "probe_timeout"
    if probe.get("ok"):
        return "active"
    return "probe_failed"


def _normalize_relogin_mode(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"web", "web_session", "session", "chatgpt_session"}:
        return "web_session"
    if text in {"email_otp", "chatgpt_email_otp", "passwordless", "passwordless_email"}:
        return "chatgpt_email_otp"
    if text in {"codex", "codex_oauth", "oauth", "pkce"}:
        return "codex_oauth"
    if text in {"browser", "browser_session", "camoufox"}:
        return "browser_session"
    return "auto"


def _relogin_failure_quota_status(relogin: dict[str, Any]) -> str:
    text = json.dumps(relogin or {}, ensure_ascii=False).lower()
    if "account_deactivated" in text or "deleted or deactivated" in text:
        return "account_deactivated"
    if "add_phone" in text or "phone_verification" in text:
        return "phone_verification_required"
    if "mailbox_transport" in text or "mailbox_transport_unavailable" in text:
        return "relogin_mailbox_transport_failed"
    if "mailbox_auth_invalid" in text:
        return "mailbox_auth_invalid"
    if "mailbox" in text or "email_otp" in text or "otp" in text:
        return "relogin_otp_failed"
    if "cooldown" in text:
        return "relogin_cooldown"
    return "relogin_failed"


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()
