import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as curl_requests

from .config import CFG
from .session_refresh import _load_seed_session
from .storage import get_account_record, list_paypal_accounts, mark_k12_status
from .k12_export import build_k12_cpa_json, export_k12_cpa_json
from .k12_identity import (
    _extract_access_token,
    _extract_account_id_from_data,
    _extract_refresh_token,
    _extract_user_id_from_data,
    _jwt_claims,
    _token_account_id,
    _token_user_id,
)
from . import k12_client as _k12_client
from . import k12_invite as _k12_invite
from . import k12_verify as _k12_verify


DEFAULT_WORKSPACE_ID = "631e1603-06cf-4f0b-b79b-d09fbfcfe98d"


def parse_workspace_ids(value="", file_path=""):
    chunks = []
    if value:
        chunks.append(str(value))
    if file_path:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"workspace file not found: {file_path}")
        chunks.append(path.read_text(encoding="utf-8-sig"))
    if not chunks:
        configured = ((CFG.get("k12") or {}).get("workspace_ids") or "").strip()
        chunks.append(configured or DEFAULT_WORKSPACE_ID)

    ids = []
    seen = set()
    for chunk in chunks:
        for raw in str(chunk or "").replace(",", "\n").splitlines():
            ws_id = raw.strip()
            if not ws_id or ws_id.startswith("#") or ws_id in seen:
                continue
            seen.add(ws_id)
            ids.append(ws_id)
    return ids


def normalize_k12_route(route):
    value = str(route or "").strip().lower()
    if value in {"leave", "exit", "quit", "remove", "delete", "workspace_leave"}:
        return "leave"
    if value in {"accept", "join", "invite_accept"}:
        return "accept"
    return "request"


# Token/session identity helpers moved to sms_tool.k12_identity.


# K12 CPA export helpers moved to sms_tool.k12_export.



def _extract_cookie_header(data):
    if not isinstance(data, dict):
        return ""
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    session = auth_session.get("session") if isinstance(auth_session.get("session"), dict) else {}
    candidates = (
        data.get("cookie_header"),
        data.get("cookies"),
        auth_session.get("cookie_header"),
        auth_session.get("cookies"),
        session.get("cookie_header"),
        session.get("cookies"),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _account_from_row(row):
    raw = {}
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except Exception:
        raw = {}
    access_token = str(row.get("access_token") or "").strip() or _extract_access_token(raw)
    mailbox = raw.get("mailbox") if isinstance(raw.get("mailbox"), dict) else {}
    return {
        "email": str(row.get("email") or raw.get("email") or "").strip().lower(),
        "access_token": access_token,
        "device_id": str(row.get("device_id") or raw.get("device_id") or "").strip(),
        "cookie_header": str(row.get("cookie_header") or "").strip() or _extract_cookie_header(raw),
        "account_id": _extract_account_id_from_data(raw),
        "user_id": _extract_user_id_from_data(raw),
        "raw": raw,
        "json_path": str(row.get("json_path") or raw.get("json_path") or "").strip(),
        "mailbox": mailbox,
    }


def load_k12_accounts(emails=None, session_file=""):
    accounts = []
    seen = set()

    if session_file:
        data, _ = _load_seed_session(session_file=session_file)
        email = str(data.get("email") or "").strip().lower()
        token = _extract_access_token(data)
        if email and token:
            row = get_account_record(email)
            raw = {}
            try:
                raw = json.loads((row or {}).get("raw_json") or "{}")
            except Exception:
                raw = {}
            merged = {**raw, **data} if isinstance(raw, dict) else dict(data)
            mailbox = merged.get("mailbox") if isinstance(merged.get("mailbox"), dict) else {}
            accounts.append({
                "email": email,
                "access_token": token,
                "device_id": str(data.get("device_id") or raw.get("device_id") or "").strip(),
                "cookie_header": str(data.get("cookie_header") or _extract_cookie_header(raw)).strip(),
                "account_id": _extract_account_id_from_data(merged),
                "user_id": _extract_user_id_from_data(merged),
                "raw": merged,
                "json_path": str((row or {}).get("json_path") or data.get("json_path") or "").strip(),
                "mailbox": mailbox,
            })
            seen.add(email)

    for email in emails or []:
        email = str(email or "").strip().lower()
        if not email or email in seen:
            continue
        row = get_account_record(email)
        account = _account_from_row(row) if row else {}
        if account.get("email") and account.get("access_token"):
            accounts.append(account)
            seen.add(account["email"])

    if not emails and not session_file:
        for row in list_paypal_accounts():
            full_row = get_account_record(row.get("email", "")) or row
            account = _account_from_row(full_row)
            email = account.get("email", "")
            if not email or email in seen or not account.get("access_token"):
                continue
            accounts.append(account)
            seen.add(email)

    return accounts


def _refresh_access_token_from_cookie(account, proxy=None, timeout=30):
    _k12_client.curl_requests = curl_requests
    return _k12_client._refresh_access_token_from_cookie(account, proxy=proxy, timeout=timeout)


def _fetch_auth_session_from_cookie(account, proxy=None, timeout=30):
    _k12_client.curl_requests = curl_requests
    return _k12_client._fetch_auth_session_from_cookie(account, proxy=proxy, timeout=timeout)


def _delete_workspace_user(account, workspace_id="", proxy=None, timeout=30, max_retries=0, retry_backoff=5):
    _k12_client.curl_requests = curl_requests
    return _k12_client._delete_workspace_user(
        account,
        workspace_id=workspace_id,
        proxy=proxy,
        timeout=timeout,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        fetch_auth_session_func=_fetch_auth_session_from_cookie,
    )


def _verify_workspace_session(account, expected_workspace_id, proxy=None, timeout=30):
    return _k12_verify._verify_workspace_session(
        account,
        expected_workspace_id,
        proxy=proxy,
        timeout=timeout,
        fetch_auth_session_func=_fetch_auth_session_from_cookie,
    )

def _post_workspace_invite(account, workspace_id, route="request", proxy=None, timeout=30, max_retries=0, retry_backoff=5):
    _k12_client.curl_requests = curl_requests
    return _k12_client._post_workspace_invite(
        account,
        workspace_id,
        route=route,
        proxy=proxy,
        timeout=timeout,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        refresh_access_token_func=_refresh_access_token_from_cookie,
    )


def _mailbox_from_account(*args, **kwargs):
    return _k12_invite._mailbox_from_account(*args, **kwargs)

def _message_text(*args, **kwargs):
    return _k12_invite._message_text(*args, **kwargs)

def _extract_invite_url(*args, **kwargs):
    return _k12_invite._extract_invite_url(*args, **kwargs)

def _workspace_id_from_invite_url(*args, **kwargs):
    return _k12_invite._workspace_id_from_invite_url(*args, **kwargs)

def _poll_k12_invite_url(*args, **kwargs):
    return _k12_invite._poll_k12_invite_url(*args, **kwargs)

def _open_invite_url_with_session(*args, **kwargs):
    return _k12_invite._open_invite_url_with_session(*args, **kwargs)

def _accept_invite_after_request(account, workspace_ids, proxy=None, timeout=30, invite_timeout=240, max_retries=0, retry_backoff=5):
    return _k12_invite._accept_invite_after_request(
        account,
        workspace_ids,
        proxy=proxy,
        timeout=timeout,
        invite_timeout=invite_timeout,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        post_workspace_invite_func=_post_workspace_invite,
    )

def run_k12_batch(accounts, workspace_ids, route="request", workers=4, proxy=None, timeout=30, max_retries=0, retry_backoff=5, auto_accept=False, invite_timeout=240, export_dir="", verify_session=False):
    route = normalize_k12_route(route)
    accounts = [account for account in accounts or [] if account.get("email") and account.get("access_token")]
    workspace_ids = [str(ws or "").strip() for ws in workspace_ids or [] if str(ws or "").strip()]
    if route == "leave" and not workspace_ids:
        workspace_ids = [""]
    jobs = [(account, ws) for account in accounts for ws in workspace_ids]
    workers = max(1, min(int(workers or 1), 20, len(jobs) or 1))

    print(f"[*] K12 workspace {route}: {len(accounts)} account(s), {len(workspace_ids)} workspace(s), workers={workers}, retries={int(max_retries or 0)}, auto_accept={bool(auto_accept)}, verify_session={bool(verify_session)}")
    if not jobs:
        return {"ok": False, "total": 0, "success": 0, "failed": 0, "results": []}

    ordered = [None] * len(jobs)
    def _run(index, account, workspace_id):
        if route == "leave":
            result = _delete_workspace_user(
                account,
                workspace_id,
                proxy=proxy,
                timeout=timeout,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )
        else:
            result = _post_workspace_invite(
                account,
                workspace_id,
                route=route,
                proxy=proxy,
                timeout=timeout,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )
        request_ok = bool(result.get("ok"))
        if request_ok:
            mark_k12_status(
                account.get("email", ""),
                workspace_id=result.get("workspace_id") or workspace_id,
                status="k12_left" if route == "leave" else ("k12_requested" if route == "request" else "k12_joined"),
                result=result,
                access_token=account.get("access_token", ""),
            )
        elif "token_invalidated" in (str(result.get("body") or "") + " " + str(result.get("error") or "")).lower():
            mark_k12_status(
                account.get("email", ""),
                workspace_id=workspace_id,
                status="at_invalid",
                result=result,
                access_token=account.get("access_token", ""),
            )
        if request_ok and auto_accept and route == "request":
            result["auto_accept"] = _accept_invite_after_request(
                account,
                [workspace_id],
                proxy=proxy,
                timeout=timeout,
                invite_timeout=invite_timeout,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )
            result["ok"] = bool((result.get("auto_accept") or {}).get("ok"))
        final_joined = bool(result.get("ok")) and (auto_accept or route == "accept")
        if final_joined and verify_session:
            verification = _verify_workspace_session(
                account,
                workspace_id,
                proxy=proxy,
                timeout=timeout,
            )
            result["workspace_verify"] = verification
            if not verification.get("ok"):
                result["ok"] = False
                result["error"] = verification.get("error") or "workspace_verify_failed"
                mark_k12_status(
                    account.get("email", ""),
                    workspace_id=workspace_id,
                    status="k12_verify_failed",
                    result=result,
                    access_token=account.get("access_token", ""),
                )
        final_joined = bool(result.get("ok")) and (auto_accept or route == "accept")
        if final_joined:
            cpa_export = export_k12_cpa_json(account, workspace_id, export_dir=export_dir)
            result["cpa_export"] = cpa_export
            if cpa_export.get("ok"):
                print(f"    K12 CPA JSON: {cpa_export.get('path')}")
            else:
                result["ok"] = False
                result["error"] = cpa_export.get("error", "k12_cpa_export_failed")
        if result.get("ok"):
            mark_k12_status(
                account.get("email", ""),
                workspace_id=result.get("workspace_id") or workspace_id,
                status="k12_left" if route == "leave" else ("k12_joined" if (auto_accept or route == "accept") else "k12_requested"),
                result=result,
                access_token=account.get("access_token", ""),
            )
        status = result.get("status", 0)
        label = "OK" if result.get("ok") else "FAIL"
        shown_workspace = str(result.get("workspace_id") or workspace_id or "current")
        print(f"[{index + 1}/{len(jobs)}] {label} {account.get('email')} -> {shown_workspace[:8]} {route} HTTP {status}")
        return index, result

    if workers <= 1:
        for i, (account, workspace_id) in enumerate(jobs):
            _, result = _run(i, account, workspace_id)
            ordered[i] = result
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_run, i, account, workspace_id) for i, (account, workspace_id) in enumerate(jobs)]
            for future in as_completed(futures):
                i, result = future.result()
                ordered[i] = result

    results = [result for result in ordered if result is not None]
    success = sum(1 for result in results if result.get("ok"))
    summary = {
        "ok": success == len(results),
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
        "route": route,
        "workspace_ids": workspace_ids,
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary
