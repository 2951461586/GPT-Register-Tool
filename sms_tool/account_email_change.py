"""Protocol email-change workflow.

The workflow deliberately keeps mailbox allocation separate from account
selection.  Disposable providers create one mailbox per account; persistent
providers consume one credentialed mailbox per account.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .account_liveness import account_chatgpt_id, probe_account_liveness
from .account_recovery import relogin_chatgpt_email_account
from .config import CFG, ConfigInput
from .mailbox import _load_mailbox_pool, _poll_email_otp
from .mailbox_types import MailboxAccount
from .storage import (
    list_account_records,
    migrate_account_email,
)


CHANGE_EMAIL_ELIGIBILITY = "/backend-api/accounts/change_email/eligibility"
CHANGE_EMAIL_BEGIN = "/backend-api/accounts/change_email/begin"
CHANGE_EMAIL_VERIFY = "/backend-api/accounts/change_email/verify"


@dataclass(frozen=True)
class EmailChangeRequest:
    provider: str
    target_mailbox_file: str = ""
    workers: int = 4
    timeout: int = 180
    otp_timeout: int = 300
    proxy: str | None = None
    service_mode: str = "purchase"
    cfworker_domain: str = ""
    smailr_domain: str = ""


def _safe_error(value: Any) -> str:
    text = str(value or "")
    for secret in (os.environ.get("REMAIL_API_KEY", ""), os.environ.get("SMAILR_API_KEY", "")):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:300]


def _account_data(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(record.get("raw_json") or "")
    try:
        parsed = json.loads(raw) if raw else {}
    except Exception:
        parsed = {}
    data = dict(parsed) if isinstance(parsed, dict) else {}
    for key, value in record.items():
        if key not in data and value is not None:
            data[key] = value
    data["email"] = str(record.get("email") or data.get("email") or "").strip().lower()
    return data


def _mailbox_data(mailbox: MailboxAccount) -> dict[str, Any]:
    return {
        "email": mailbox.email,
        "provider": mailbox.provider,
        "source": mailbox.source,
        "token": mailbox.token,
        "password": mailbox.password,
        "login_password": mailbox.login_password,
        "refresh_token": mailbox.refresh_token,
        "access_token": mailbox.access_token,
        "client_secret": mailbox.client_secret,
        "auth_mode": mailbox.auth_mode,
        "order_no": mailbox.order_no,
        "purchase_id": mailbox.purchase_id,
    }


def _provider_name(value: str) -> str:
    name = str(value or "").strip().lower().replace("-", "_")
    aliases = {"cf_worker": "cfworker", "cloudflare": "cfworker", "i_cloud": "icloud"}
    return aliases.get(name, name)


def _build_provider_args(request: EmailChangeRequest, count: int):
    import argparse

    return argparse.Namespace(
        count=count,
        proxy=request.proxy,
        cfworker_domain=request.cfworker_domain or None,
        smailr_domain=request.smailr_domain or None,
        remail_service_mode=request.service_mode or None,
        buy_remail_mailbox=True,
        buy_cfworker_mailbox=True,
        buy_smailr_mailbox=True,
        mailbox_file=request.target_mailbox_file or None,
        chatai_mailbox_file=None,
        remail_supply=None,
        remail_email_suffix=None,
        remail_project_id=None,
        remail_product_id=None,
    )


def allocate_target_mailboxes(request: EmailChangeRequest, count: int) -> list[MailboxAccount]:
    """Allocate exactly ``count`` target mailboxes or raise a descriptive error."""
    provider = _provider_name(request.provider)
    if count < 1:
        return []
    args = _build_provider_args(request, count)
    if provider == "remail":
        from .mailbox_remail import _create_remail_mailboxes

        mailboxes = _create_remail_mailboxes(args, service_mode=request.service_mode or "purchase")
    elif provider == "cfworker":
        from .mailbox_cfworker import _create_cfworker_mailboxes

        mailboxes = _create_cfworker_mailboxes(args)
    elif provider == "smailr":
        from .mailbox_smailr import create_smailr_mailboxes

        mailboxes = create_smailr_mailboxes(count=count, domain=request.smailr_domain, proxy=request.proxy)
    elif provider in {"icloud", "outlook", "hotmail"}:
        args.buy_remail_mailbox = args.buy_cfworker_mailbox = args.buy_smailr_mailbox = False
        mailboxes = _load_mailbox_pool(args)
        mailboxes = [item for item in mailboxes if _matches_persistent_provider(item, provider)]
        if len(mailboxes) < count:
            raise ValueError(f"target mailbox credentials insufficient: required={count} available={len(mailboxes)}")
        mailboxes = mailboxes[:count]
    else:
        raise ValueError("unsupported change-email provider: " + provider)
    if len(mailboxes) != count:
        raise RuntimeError(f"provider returned {len(mailboxes)} mailbox(es), required {count}")
    return list(mailboxes)


def _matches_persistent_provider(mailbox: MailboxAccount, provider: str) -> bool:
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    actual = _provider_name(getattr(mailbox, "provider", ""))
    if provider == "icloud":
        return actual == "icloud" or domain in {"icloud.com", "me.com", "mac.com"}
    if provider in {"outlook", "hotmail"}:
        return domain in ({"outlook.com"} if provider == "outlook" else {"hotmail.com", "live.com", "msn.com"})
    return False


def _cookie_header(account: Mapping[str, Any]) -> str:
    value = str(account.get("cookie_header") or "").strip()
    if value:
        return value
    token = str(account.get("session_token") or "").strip()
    auth = account.get("auth_session") if isinstance(account.get("auth_session"), dict) else {}
    token = token or str(auth.get("sessionToken") or auth.get("session_token") or "").strip()
    return f"__Secure-next-auth.session-token={token}" if token else ""


def _session_id(account: Mapping[str, Any]) -> str:
    auth = account.get("auth_session") if isinstance(account.get("auth_session"), dict) else {}
    return str(account.get("oai_session_id") or account.get("auth_session_logging_id") or auth.get("id") or uuid.uuid4()).strip()


def _chat_headers(account: Mapping[str, Any], path: str) -> dict[str, str]:
    chat_cfg = CFG.get("chatgpt") if isinstance(CFG.get("chatgpt"), dict) else {}
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": str(chat_cfg.get("chat_base_url") or "https://chatgpt.com").rstrip("/"),
        "Referer": str(chat_cfg.get("chat_base_url") or "https://chatgpt.com").rstrip("/") + "/",
        "User-Agent": "Mozilla/5.0 Chrome/151.0.0.0 Safari/537.36",
        "OAI-Client-Build-Number": str(account.get("oai_client_build_number") or "9641653"),
        "OAI-Client-Version": str(account.get("oai_client_version") or "prod"),
        "OAI-Device-Id": str(account.get("device_id") or uuid.uuid4()),
        "OAI-Language": "zh-CN",
        "OAI-Session-Id": _session_id(account),
        "X-OpenAI-Target-Path": path,
        "X-OpenAI-Target-Route": path,
    }
    account_id = account_chatgpt_id(dict(account))
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    cookie = _cookie_header(account)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _json_response(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


def change_email_begin(account: Mapping[str, Any], target_email: str, *, timeout: int = 30, proxy: str | None = None, transport: Callable[..., Any] | None = None) -> dict[str, Any]:
    return _change_email_request(account, CHANGE_EMAIL_BEGIN, {"email": target_email}, timeout=timeout, proxy=proxy, transport=transport)


def change_email_verify(account: Mapping[str, Any], target_email: str, code: str, *, timeout: int = 30, proxy: str | None = None, transport: Callable[..., Any] | None = None) -> dict[str, Any]:
    return _change_email_request(account, CHANGE_EMAIL_VERIFY, {"email": target_email, "code": code}, timeout=timeout, proxy=proxy, transport=transport)


def check_change_email_eligibility(account: Mapping[str, Any], *, timeout: int = 30, proxy: str | None = None, transport: Callable[..., Any] | None = None) -> dict[str, Any]:
    return _change_email_request(account, CHANGE_EMAIL_ELIGIBILITY, None, timeout=timeout, proxy=proxy, transport=transport)


def _change_email_request(account: Mapping[str, Any], path: str, payload: dict[str, Any] | None, *, timeout: int, proxy: str | None, transport: Callable[..., Any] | None) -> dict[str, Any]:
    if not _cookie_header(account):
        return {"ok": False, "status_code": 0, "error": "missing_session_cookie"}
    if transport is None:
        from curl_cffi import requests as curl_requests

        chat_cfg = CFG.get("chatgpt") if isinstance(CFG.get("chatgpt"), dict) else {}
        base = str(chat_cfg.get("chat_base_url") or "https://chatgpt.com").rstrip("/")
        session = curl_requests.Session()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        method = "get" if payload is None else "post"
        response = getattr(session, method)(base + path, headers=_chat_headers(account, path), json=payload, timeout=max(5, int(timeout or 30)), impersonate="chrome110")
    else:
        response = transport(path, payload, _chat_headers(account, path), timeout, proxy)
    body = _json_response(response)
    status = int(getattr(response, "status_code", 0) or 0)
    ok = 200 <= status < 300 and body.get("success", True) is not False
    return {"ok": ok, "status_code": status, "success": body.get("success"), "eligible": body.get("eligible"), "eligibility_type": body.get("eligibility_type"), "error": "" if ok else _safe_error(body.get("error") or body.get("message") or f"http_{status}")}


def change_one_account(account: Mapping[str, Any], target: MailboxAccount, request: EmailChangeRequest, *, transport: Callable[..., Any] | None = None, relogin: Callable[..., dict[str, Any]] | None = None, liveness: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    old_email = str(account.get("email") or "").strip().lower()
    target_email = str(target.email or "").strip().lower()
    if not old_email or not target_email:
        return {"ok": False, "email": old_email, "error": "missing_email"}
    if old_email == target_email:
        return {"ok": False, "email": old_email, "target_email": target_email, "error": "target_email_same_as_current"}
    relogin_fn = relogin or relogin_chatgpt_email_account
    try:
        old_login = relogin_fn(dict(account), proxy=request.proxy, timeout=request.timeout, persist=False)
    except TypeError:
        old_login = relogin_fn(dict(account), proxy=request.proxy, timeout=request.timeout)
    if not old_login.get("ok"):
        return {"ok": False, "email": old_email, "target_email": target_email, "stage": "old_login", "error": _safe_error(old_login.get("error") or "old_login_failed")}
    active_account = old_login.get("_verified_data") if isinstance(old_login.get("_verified_data"), dict) else dict(account)
    eligibility = check_change_email_eligibility(active_account, timeout=request.timeout, proxy=request.proxy, transport=transport)
    eligibility_type = str(eligibility.get("eligibility_type") or "").strip().lower()
    if not eligibility.get("ok") or eligibility.get("eligible") is False:
        return {"ok": False, "email": old_email, "target_email": target_email, "stage": "eligibility", "error": eligibility.get("error") or "change_email_ineligible"}
    if eligibility_type and eligibility_type not in {"password", "email", "otp"}:
        return {"ok": False, "email": old_email, "target_email": target_email, "stage": "eligibility", "error": "social_account_change_not_supported"}
    issued_after = int(time.time())
    begun = change_email_begin(active_account, target_email, timeout=request.timeout, proxy=request.proxy, transport=transport)
    if not begun.get("ok"):
        return {"ok": False, "email": old_email, "target_email": target_email, "stage": "begin", "error": begun.get("error") or "change_email_begin_failed"}
    try:
        code = _poll_email_otp(target, subject_keyword="", timeout=max(30, int(request.otp_timeout or 300)), issued_after_unix=issued_after, proxy=request.proxy)
    except Exception as exc:
        return {"ok": False, "email": old_email, "target_email": target_email, "stage": "otp", "error": _safe_error(exc)}
    if not code:
        return {"ok": False, "email": old_email, "target_email": target_email, "stage": "otp", "error": "otp_timeout"}
    verified = change_email_verify(active_account, target_email, code, timeout=request.timeout, proxy=request.proxy, transport=transport)
    if not verified.get("ok"):
        return {"ok": False, "email": old_email, "target_email": target_email, "stage": "verify", "error": verified.get("error") or "change_email_verify_failed"}
    candidate = dict(active_account)
    candidate.update({"email": target_email, "mailbox": _mailbox_data(target), "mailbox_provider": target.provider, "mailbox_source": target.source, "mailbox_token": target.token})
    try:
        login = relogin_fn(candidate, proxy=request.proxy, timeout=request.timeout, persist=False)
    except TypeError:
        login = relogin_fn(candidate, proxy=request.proxy, timeout=request.timeout)
    if not login.get("ok"):
        return {"ok": False, "email": old_email, "target_email": target_email, "stage": "relogin", "error": _safe_error(login.get("error") or "relogin_failed")}
    verified_data = login.get("_verified_data") if isinstance(login.get("_verified_data"), dict) else dict(candidate)
    probe = liveness or probe_account_liveness
    live = probe(verified_data, proxy=request.proxy, timeout=min(60, max(10, request.timeout)))
    if int(live.get("status_code") or 0) != 200:
        return {"ok": False, "email": old_email, "target_email": target_email, "stage": "liveness", "error": "liveness_failed", "liveness": {"ok": bool(live.get("ok")), "status_code": live.get("status_code")}}
    migrated = migrate_account_email(old_email, target_email, verified_data, runtime_config=CFG)
    if not migrated:
        return {"ok": False, "email": old_email, "target_email": target_email, "stage": "storage", "error": "storage_migration_failed"}
    return {"ok": True, "email": old_email, "target_email": target_email, "stage": "completed", "status_code": 200}


def change_email_batch(accounts: Iterable[Mapping[str, Any]], request: EmailChangeRequest, *, allocate: Callable[[EmailChangeRequest, int], list[MailboxAccount]] = allocate_target_mailboxes, transport: Callable[..., Any] | None = None, relogin: Callable[..., dict[str, Any]] | None = None, liveness: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    selected = [dict(item) for item in accounts if str(item.get("email") or "").strip()]
    if not selected:
        return {"ok": True, "total": 0, "success": 0, "failed": 0, "results": []}
    mailboxes = allocate(request, len(selected))
    ordered: list[dict[str, Any] | None] = [None] * len(selected)
    workers = max(1, min(int(request.workers or 1), 16, len(selected)))
    def run(index: int):
        try:
            return index, change_one_account(selected[index], mailboxes[index], request, transport=transport, relogin=relogin, liveness=liveness)
        except Exception as exc:
            return index, {"ok": False, "email": selected[index].get("email", ""), "stage": "unexpected", "error": _safe_error(exc)}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for future in as_completed([executor.submit(run, i) for i in range(len(selected))]):
            index, result = future.result()
            ordered[index] = result
    results = [item for item in ordered if item is not None]
    success = sum(bool(item.get("ok")) for item in results)
    return {"ok": success == len(results), "total": len(results), "success": success, "failed": len(results) - success, "provider": _provider_name(request.provider), "results": results}


def load_change_email_accounts(emails: Iterable[str] | None = None) -> list[dict[str, Any]]:
    requested = {str(item or "").strip().lower() for item in (emails or []) if str(item or "").strip()}
    rows = list_account_records(runtime_config=CFG)
    result = []
    for row in rows:
        data = _account_data(row)
        if not requested or data.get("email") in requested:
            result.append(data)
    return result
