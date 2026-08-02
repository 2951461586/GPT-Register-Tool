"""Local account liveness refresh and explicit OAuth recovery workflows.

The liveness probe itself is side-effect free and lives in
``account_liveness``. This module owns persistence, deactivation handling, and
the explicitly requested email-OTP relogin path.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .account_liveness import probe_account_liveness
from .storage import get_account_record, list_paypal_accounts, mark_quota_status, upsert_account


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
    max_workers = max(1, min(int(workers or 1), 8, len(accounts) or 1))
    ordered: list[dict[str, Any] | None] = [None] * len(accounts)

    def run(index: int, account: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        email = str(account.get("email") or "").strip()
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
        if relogin_on_401 and str(probe.get("status") or "") == "token_invalid" and email:
            relogin = relogin_codex_account(
                account,
                proxy=proxy,
                timeout=max(int(relogin_timeout or timeout or 180), int(timeout or 30)),
                mode=relogin_mode,
            )
            if relogin.get("ok"):
                probe = dict(relogin.get("probe") or {})
        status = str(probe.get("quota_status") or probe.get("status") or "未知")
        if relogin and not relogin.get("ok"):
            status = _relogin_failure_quota_status(relogin)
        persisted = mark_quota_status(email, status, quota_result=probe) if email else False
        return index, {
            "ok": bool(persisted),
            "email": email,
            "quota_status": status,
            "probe": probe,
            **({"relogin": relogin} if relogin else {}),
            "persisted": bool(persisted),
        }

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run, index, account) for index, account in enumerate(accounts)]
        for future in as_completed(futures):
            index, result = future.result()
            ordered[index] = result
    results = [item for item in ordered if item is not None]
    success = sum(1 for item in results if item.get("ok"))
    return {
        "ok": success == len(results),
        "mode": "local",
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
        "results": results,
    }


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
        ) or {})
        result["mode"] = "web_session"
        result["ok"] = bool(result.get("ok"))
        return result
    except Exception as exc:
        return {"ok": False, "mode": "web_session", "error": str(exc)}


def relogin_codex_account(
    account: dict[str, Any],
    proxy: str | None = None,
    timeout: int = 180,
    mode: str = "auto",
) -> dict[str, Any]:
    """Refresh an AT through an explicit OAuth recovery mode."""
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
    return relogin_local_codex_account(account, proxy=proxy, timeout=timeout)


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
        return {"ok": False, "error": str(exc)}


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
    return {
        key: value
        for key, value in dict(result or {}).items()
        if key not in {"tokens", "access_token", "id_token", "refresh_token"}
    }


def _looks_account_deactivated(value: Any) -> bool:
    text = json.dumps(value or {}, ensure_ascii=False).lower()
    return any(marker in text for marker in (
        "account_deactivated",
        "account_deatived",
        "deleted or deactivated",
        "account has been deleted",
        "account has been deactivated",
    ))


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
