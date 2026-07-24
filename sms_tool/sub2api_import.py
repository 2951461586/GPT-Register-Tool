import json
import random
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests as curl_requests

from .agent_identity import (
    create_agent_identity,
    is_agent_identity,
    load_agent_identity,
    validate_agent_identity,
    write_agent_identity,
)
from .codex_export import build_codex_json
from .config import CFG
from .cpa_import import _load_cpa_source, _write_cpa_json
from .storage import get_account_record, upsert_account


DEFAULT_GROUP_NAME = "codex"


def import_sub2api_session(
    email="",
    session_file="",
    export_dir="",
    refresh=True,
    proxy=None,
    timeout=300,
    api_url="",
    api_token="",
    login_email="",
    login_password="",
    group_name="",
    group_ids=None,
    proxy_name="",
    proxy_id=None,
    priority=None,
    concurrency=None,
    auth_mode="",
    verify_after_import=None,
):
    cfg = _resolve_sub2api_config(
        api_url=api_url,
        api_token=api_token,
        login_email=login_email,
        login_password=login_password,
        group_name=group_name,
        group_ids=group_ids,
        proxy_name=proxy_name,
        proxy_id=proxy_id,
        priority=priority,
        concurrency=concurrency,
        auth_mode=auth_mode,
        verify_after_import=verify_after_import,
    )
    target_email = (email or "").strip().lower()
    if not cfg["origin"]:
        return {"ok": False, "email": target_email, "error": "missing_sub2api_url"}

    prepared = _prepare_sub2api_import_data(
        target_email,
        session_file=session_file,
        export_dir=export_dir,
        auth_mode=cfg.pop("auth_mode"),
        proxy=proxy,
        timeout=timeout,
    )
    if not prepared.get("ok"):
        return {
            "ok": False,
            "email": target_email,
            "error": prepared.get("error", "missing_codex_source_json"),
            "message": prepared.get("message", ""),
            "source": prepared.get("source", {}),
        }

    token_data = prepared["data"]
    warnings = list(prepared.get("warnings", []))
    path = prepared.get("path", "")
    resolved_email = prepared.get("email") or target_email
    if prepared.get("auth_mode") == "agent_identity" and cfg.get("verify_after_import"):
        warnings.append("agent_identity_execution_probe_skipped")
    export_result = {
        "ok": True,
        "email": resolved_email,
        "path": path,
        "mode": prepared.get("mode", "codex_session_json"),
        "auth_mode": prepared.get("auth_mode", "oauth"),
        "source_path": prepared.get("source_path", ""),
        "source_mode": prepared.get("source_mode", ""),
        "refresh_token_status": prepared.get("refresh_token_status", "no_rt"),
        "warnings": warnings,
    }
    upload_result = upload_to_sub2api(token_data, **cfg)
    _record_sub2api_import(
        export_result.get("email", target_email),
        path,
        upload_result,
        auth_mode=export_result.get("auth_mode", ""),
    )
    return {
        "ok": upload_result.get("ok", False),
        "email": export_result.get("email", target_email),
        "path": path,
        "sub2api": upload_result,
        "export": export_result,
        "refresh_token_status": export_result["refresh_token_status"],
        "warnings": warnings,
    }


def import_sub2api_sessions(
    emails,
    export_dir="",
    workers=1,
    refresh=True,
    proxy=None,
    timeout=300,
    api_url="",
    api_token="",
    login_email="",
    login_password="",
    group_name="",
    group_ids=None,
    proxy_name="",
    proxy_id=None,
    priority=None,
    concurrency=None,
    auth_mode="",
    verify_after_import=None,
):
    emails = [str(email or "").strip() for email in emails if str(email or "").strip()]
    ordered = [None] * len(emails)
    max_workers = max(1, min(int(workers or 1), 10, len(emails) or 1))

    def _run(index, item_email):
        return index, import_sub2api_session(
            email=item_email,
            export_dir=export_dir,
            refresh=refresh,
            proxy=proxy,
            timeout=timeout,
            api_url=api_url,
            api_token=api_token,
            login_email=login_email,
            login_password=login_password,
            group_name=group_name,
            group_ids=group_ids,
            proxy_name=proxy_name,
            proxy_id=proxy_id,
            priority=priority,
            concurrency=concurrency,
            auth_mode=auth_mode,
            verify_after_import=verify_after_import,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_run, i, item_email) for i, item_email in enumerate(emails)]
        for future in as_completed(futures):
            index, result = future.result()
            ordered[index] = result

    results = [result for result in ordered if result is not None]
    ok_count = sum(1 for result in results if result.get("ok"))
    return {
        "ok": ok_count == len(emails),
        "total": len(emails),
        "success": ok_count,
        "failed": len(emails) - ok_count,
        "results": results,
    }


def _prepare_sub2api_import_data(email, session_file="", export_dir="", auth_mode="auto", proxy=None, timeout=300):
    mode = _normalize_auth_mode(auth_mode)
    direct_identity = _load_direct_agent_identity(session_file)
    if direct_identity.get("ok"):
        if mode == "oauth":
            return {"ok": False, "error": "agent_identity_not_allowed_in_oauth_mode"}
        data = direct_identity["data"]
        identity = data["agent_identity"]
        return {
            "ok": True,
            "data": data,
            "email": str(identity.get("email") or email).strip().lower(),
            "path": direct_identity.get("path", ""),
            "mode": "agent_identity_json",
            "auth_mode": "agent_identity",
            "source_path": direct_identity.get("path", ""),
            "source_mode": "agent_identity_json",
            "refresh_token_status": "not_required",
            "warnings": [],
        }

    if mode != "oauth" and email:
        existing = load_agent_identity(email, export_dir=export_dir)
        if existing.get("ok"):
            data = existing["data"]
            identity = data["agent_identity"]
            return {
                "ok": True,
                "data": data,
                "email": str(identity.get("email") or email).strip().lower(),
                "path": existing.get("path", ""),
                "mode": "agent_identity_json",
                "auth_mode": "agent_identity",
                "source_path": existing.get("path", ""),
                "source_mode": "existing_agent_identity",
                "refresh_token_status": "not_required",
                "warnings": [],
            }

    source_result = _load_cpa_source(email, session_file=session_file, export_dir=export_dir)
    if not source_result.get("ok"):
        return {
            "ok": False,
            "error": source_result.get("error", "missing_codex_source_json"),
            "message": source_result.get("message", ""),
            "source": source_result,
        }
    token_data, warnings = build_codex_json(source_result["data"])
    if not token_data.get("email"):
        token_data["email"] = email
    plan_type = str(token_data.get("plan_type") or "").strip().lower()
    use_agent_identity = mode == "agent_identity" or (mode == "auto" and plan_type == "free")
    if use_agent_identity:
        if plan_type and plan_type != "free":
            if mode == "agent_identity":
                return {"ok": False, "error": "agent_identity_requires_free_plan", "plan_type": plan_type}
        else:
            created = create_agent_identity(source_result["data"], proxy=proxy, timeout=min(max(int(timeout or 30), 1), 60))
            if created.get("ok"):
                persisted = write_agent_identity(created["data"], export_dir=export_dir)
                if persisted.get("ok"):
                    identity = persisted["data"]["agent_identity"]
                    return {
                        "ok": True,
                        "data": persisted["data"],
                        "email": str(identity.get("email") or email).strip().lower(),
                        "path": persisted["path"],
                        "mode": "agent_identity_json",
                        "auth_mode": "agent_identity",
                        "source_path": source_result.get("path", ""),
                        "source_mode": source_result.get("mode", ""),
                        "refresh_token_status": "not_required",
                        "warnings": created.get("warnings", []),
                    }
                elif mode == "agent_identity" and not _is_agent_registry_disabled(persisted):
                    return persisted
            elif mode == "agent_identity" and not _is_agent_registry_disabled(created):
                return created
            # Fall back to oauth when agent identity fails.
            # This covers both auth_mode="auto" (always fall back) and
            # auth_mode="agent_identity" when the error is HTTP 403
            # ("Agent registry is not enabled") — a permanent condition
            # that won't be resolved by retrying.
            fallback_warning = (
                f"agent_identity_fallback_to_oauth: {created.get('error', 'unknown')}"
                if not created.get("ok")
                else f"agent_identity_persist_failed: {persisted.get('error', 'unknown')}"
            )
            warnings.append(fallback_warning)

    if not token_data.get("access_token"):
        return {"ok": False, "error": "missing_access_token"}
    path = _write_cpa_json(token_data, export_dir)
    return {
        "ok": True,
        "data": token_data,
        "email": str(token_data.get("email") or email).strip().lower(),
        "path": path,
        "mode": "codex_session_json",
        "auth_mode": "oauth",
        "source_path": source_result.get("path", ""),
        "source_mode": source_result.get("mode", ""),
        "refresh_token_status": "oauth_present" if str(token_data.get("refresh_token") or "").strip() else "no_rt",
        "warnings": warnings,
    }


def _load_direct_agent_identity(session_file):
    path = Path(str(session_file or "").strip()) if str(session_file or "").strip() else None
    if not path or not path.is_file():
        return {"ok": False, "error": "agent_identity_not_found"}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"ok": False, "error": "invalid_session_json"}
    if not is_agent_identity(data):
        return {"ok": False, "error": "not_agent_identity"}
    result = validate_agent_identity(data)
    result["path"] = str(path)
    return result


def _normalize_auth_mode(value):
    text = str(value or "auto").strip().lower().replace("-", "_")
    if text in {"agentidentity", "agent_identity", "agent"}:
        return "agent_identity"
    if text in {"oauth", "token", "session"}:
        return "oauth"
    return "auto"


def _is_agent_registry_disabled(result):
    """Check if an agent identity error result is HTTP 403 'Agent registry is
    not enabled' — a permanent condition that means the account will never
    support Agent Registry, so the caller should fall back to oauth."""
    if not isinstance(result, dict):
        return False
    error = str(result.get("error") or "").strip().lower()
    message = str(result.get("message") or "").strip().lower()
    return error == "agent_registration_http_403" or "agent registry is not enabled" in message


def upload_to_sub2api(
    token_data,
    origin="",
    api_token="",
    login_email="",
    login_password="",
    group_name="",
    group_ids=None,
    proxy_name="",
    proxy_id=None,
    priority=None,
    concurrency=None,
    verify_after_import=True,
):
    if not origin:
        return {"ok": False, "error": "missing_sub2api_url"}
    agent_identity_payload = is_agent_identity(token_data)

    token_result = _resolve_sub2api_token(origin, api_token, login_email, login_password)
    if not token_result.get("ok"):
        return token_result
    token = token_result["token"]

    groups_result = _resolve_group_ids(origin, token, group_name=group_name, group_ids=group_ids)
    if not groups_result.get("ok"):
        return groups_result
    resolved_proxy_id = _resolve_proxy_id(origin, token, proxy_name=proxy_name, proxy_id=proxy_id)
    if isinstance(resolved_proxy_id, dict) and not resolved_proxy_id.get("ok"):
        return resolved_proxy_id

    payload = _build_sub2api_payload(
        token_data,
        group_ids=groups_result.get("group_ids", []),
        proxy_id=resolved_proxy_id,
        priority=priority,
        concurrency=concurrency,
    )
    response = _request_json(origin, "/api/v1/admin/accounts/import/codex-session", token=token, method="POST", body=payload)
    if not response.get("ok"):
        return response

    data = response.get("data")
    failed = _as_int((data or {}).get("failed")) if isinstance(data, dict) else 0
    created = _as_int((data or {}).get("created")) if isinstance(data, dict) else 0
    updated = _as_int((data or {}).get("updated")) if isinstance(data, dict) else 0
    import_ok = failed == 0 and (created + updated > 0 or not isinstance(data, dict))
    ok = import_ok
    verification = {"ok": False, "skipped": True, "reason": "verification_disabled"}
    if import_ok and agent_identity_payload:
        account_ids = _imported_account_ids(data)
        if not account_ids:
            verification = {"ok": False, "error": "sub2api_import_missing_account_id"}
        else:
            verification = _verify_sub2api_account_config(
                origin,
                token,
                account_ids[0],
                group_ids=groups_result.get("group_ids", []),
                proxy_id=resolved_proxy_id,
            )
        ok = bool(verification.get("ok"))
    elif import_ok and _as_bool(verify_after_import, default=True):
        account_ids = _imported_account_ids(data)
        if not account_ids:
            verification = {"ok": False, "error": "sub2api_import_missing_account_id"}
        else:
            verification = _probe_sub2api_account(origin, token, account_ids[0])
        ok = bool(verification.get("ok"))
    return {
        "ok": ok,
        "mode": "codex_session_import",
        "target": "sub2api",
        "status_code": response.get("status_code", 0),
        "created": created,
        "updated": updated,
        "failed": failed,
        "group_ids": groups_result.get("group_ids", []),
        "proxy_id": resolved_proxy_id,
        "data": data,
        "verification": verification,
        **({} if ok else {"error": verification.get("error") if import_ok else _sub2api_import_error(data)}),
    }


def _verify_sub2api_account_config(origin, token, account_id, group_ids=None, proxy_id=None):
    account_id = _as_int(account_id)
    if account_id <= 0:
        return {"ok": False, "error": "invalid_sub2api_account_id"}
    response = _request_json(origin, f"/api/v1/admin/accounts/{account_id}", token=token, method="GET", timeout=30)
    if not response.get("ok"):
        return {
            "ok": False,
            "account_id": account_id,
            "error": response.get("error", "sub2api_account_read_failed"),
        }
    account = response.get("data") if isinstance(response.get("data"), dict) else {}
    actual_group_ids = _sub2api_account_group_ids(account)
    expected_group_ids = sorted({_as_int(value) for value in (group_ids or []) if _as_int(value) > 0})
    actual_proxy_id = _as_int(account.get("proxy_id") or (account.get("proxy") or {}).get("id"))
    expected_proxy_id = _as_int(proxy_id)
    mismatches = []
    if expected_group_ids and not set(expected_group_ids).issubset(set(actual_group_ids)):
        mismatches.append("group_ids")
    if expected_proxy_id > 0 and actual_proxy_id != expected_proxy_id:
        mismatches.append("proxy_id")
    return {
        "ok": not mismatches,
        "structural_only": True,
        "execution_tested": False,
        "account_id": account_id,
        "status": str(account.get("status") or ""),
        "group_ids": actual_group_ids,
        "proxy_id": actual_proxy_id or None,
        **({"error": "sub2api_remote_config_mismatch", "mismatches": mismatches} if mismatches else {}),
    }


def _sub2api_account_group_ids(account):
    values = account.get("group_ids") or account.get("groups") or []
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    result = []
    for value in values:
        group_id = _as_int(value.get("id")) if isinstance(value, dict) else _as_int(value)
        if group_id > 0 and group_id not in result:
            result.append(group_id)
    return sorted(result)


def _imported_account_ids(data):
    if not isinstance(data, dict):
        return []
    account_ids = []
    for item in data.get("items") or []:
        if not isinstance(item, dict) or str(item.get("action") or "").lower() not in {"created", "updated"}:
            continue
        account_id = _as_int(item.get("account_id"))
        if account_id > 0 and account_id not in account_ids:
            account_ids.append(account_id)
    return account_ids


def _probe_sub2api_account(origin, token, account_id, timeout=90):
    account_id = _as_int(account_id)
    if account_id <= 0:
        return {"ok": False, "error": "invalid_sub2api_account_id"}
    account = _request_json(origin, f"/api/v1/admin/accounts/{account_id}", token=token, method="GET", timeout=30)
    if not account.get("ok"):
        return {
            "ok": False,
            "account_id": account_id,
            "error": account.get("error", "sub2api_account_lookup_failed"),
            "status_code": account.get("status_code", 0),
        }
    tested = _request_sub2api_test(origin, token, account_id, timeout=timeout)
    tested["account_id"] = account_id
    tested["account_found"] = True
    return tested


def _request_sub2api_test(origin, token, account_id, timeout=90):
    url = _join_url(origin, f"/api/v1/admin/accounts/{int(account_id)}/test")
    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    if token:
        if _looks_like_sub2api_admin_key(token):
            headers["x-api-key"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
    try:
        response = curl_requests.request(
            "POST",
            url,
            headers=headers,
            data=b"{}",
            timeout=timeout,
            impersonate="chrome110",
        )
    except Exception as exc:
        return {"ok": False, "error": "sub2api_account_test_request_failed", "message": str(exc)}
    if response.status_code < 200 or response.status_code >= 300:
        return {
            "ok": False,
            "error": f"sub2api_account_test_http_{response.status_code}",
            "status_code": response.status_code,
        }

    final_event = None
    error_message = ""
    for raw_line in str(response.text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        encoded = line[5:].strip()
        if not encoded or encoded == "[DONE]":
            continue
        try:
            event = json.loads(encoded)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").strip().lower()
        if event_type == "test_complete":
            final_event = event
        elif event_type in {"error", "test_error", "test_failed"}:
            error_message = str(event.get("message") or event.get("error") or "")[:300]
    if isinstance(final_event, dict) and final_event.get("success") is True:
        return {"ok": True, "status_code": response.status_code, "event": "test_complete"}
    return {
        "ok": False,
        "error": "sub2api_account_test_failed",
        "status_code": response.status_code,
        **({"message": error_message} if error_message else {}),
    }


def fetch_sub2api_auth_files(api_url="", api_token="", login_email="", login_password="", timeout=30):
    cfg = _resolve_sub2api_config(
        api_url=api_url,
        api_token=api_token,
        login_email=login_email,
        login_password=login_password,
    )
    if not cfg["origin"]:
        return {"ok": False, "error": "missing_sub2api_url"}
    token_result = _resolve_sub2api_token(cfg["origin"], cfg["api_token"], cfg["login_email"], cfg["login_password"])
    if not token_result.get("ok"):
        return token_result

    items = []
    page = 1
    page_size = 100
    while True:
        path = f"/api/v1/admin/accounts?page={page}&page_size={page_size}&platform=openai&type=oauth"
        result = _request_json(cfg["origin"], path, token=token_result["token"], method="GET", timeout=timeout)
        if not result.get("ok"):
            return result
        data = result.get("data")
        batch, total, pages = _parse_sub2api_account_page(data)
        items.extend(batch)
        if pages and page >= pages:
            break
        if not pages and (len(items) >= total > 0 or len(batch) < page_size):
            break
        page += 1
        if page > 1000:
            break

    return {
        "ok": True,
        "status_code": 200,
        "files": [_sub2api_account_to_auth_file(item) for item in items],
    }


def _build_sub2api_payload(token_data, group_ids=None, proxy_id=None, priority=None, concurrency=None):
    identity = token_data.get("agent_identity") if isinstance(token_data.get("agent_identity"), dict) else {}
    email = str(token_data.get("email") or identity.get("email") or "").strip()
    payload = {
        "content": json.dumps(token_data, ensure_ascii=False, separators=(",", ":")),
        "group_ids": [int(value) for value in (group_ids or []) if _as_int(value) > 0],
        "auto_pause_on_expired": True,
        "update_existing": True,
    }
    if email:
        payload["name"] = email
    expires_at = _extract_expires_at(token_data)
    if expires_at:
        payload["expires_at"] = expires_at
    resolved_priority = _as_int(priority)
    if resolved_priority > 0:
        payload["priority"] = resolved_priority
    resolved_concurrency = _as_int(concurrency)
    if resolved_concurrency >= 0:
        payload["concurrency"] = resolved_concurrency
    resolved_proxy_id = _as_int(proxy_id)
    if resolved_proxy_id > 0:
        payload["proxy_id"] = resolved_proxy_id
    return payload


def _resolve_sub2api_config(
    api_url="",
    api_token="",
    login_email="",
    login_password="",
    group_name="",
    group_ids=None,
    proxy_name="",
    proxy_id=None,
    priority=None,
    concurrency=None,
    auth_mode="",
    verify_after_import=None,
):
    section = CFG.get("sub2api") if isinstance(CFG.get("sub2api"), dict) else {}
    legacy = CFG.get("sub2api_mode") if isinstance(CFG.get("sub2api_mode"), dict) else {}
    source = {**legacy, **section}
    resolved_group_ids = group_ids if group_ids is not None else source.get("group_ids") or source.get("group_id")
    return {
        "origin": _normalize_sub2api_origin(
            api_url
            or source.get("api_url")
            or source.get("base_url")
            or source.get("url")
            or ""
        ),
        "api_token": str(api_token or source.get("api_token") or source.get("token") or source.get("access_token") or "").strip(),
        "login_email": str(login_email or source.get("email") or source.get("login_email") or "").strip(),
        "login_password": str(login_password or source.get("password") or source.get("login_password") or "").strip(),
        "group_name": str(group_name or source.get("group_name") or source.get("group") or DEFAULT_GROUP_NAME).strip(),
        "group_ids": resolved_group_ids,
        "proxy_name": str(proxy_name or source.get("proxy_name") or source.get("default_proxy_name") or "").strip(),
        "proxy_id": proxy_id if proxy_id is not None else source.get("proxy_id"),
        "priority": priority if priority is not None else source.get("priority", 1),
        "concurrency": concurrency if concurrency is not None else source.get("concurrency", 10),
        "auth_mode": _normalize_auth_mode(auth_mode or source.get("auth_mode") or "auto"),
        "verify_after_import": _as_bool(
            verify_after_import if verify_after_import is not None else source.get("verify_after_import"),
            default=True,
        ),
    }


def _normalize_sub2api_origin(api_url):
    raw = str(api_url or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    path = parsed.path.rstrip("/")
    for marker in ("/api/v1", "/api"):
        index = path.lower().find(marker)
        if index >= 0:
            path = path[:index]
            break
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def _resolve_sub2api_token(origin, api_token="", login_email="", login_password=""):
    token = str(api_token or "").strip()
    if token and not (_looks_like_sub2api_api_key(token) and login_email and login_password):
        return {"ok": True, "token": token}
    if not login_email:
        return {"ok": False, "error": "missing_sub2api_token_or_email"}
    if not login_password:
        return {"ok": False, "error": "missing_sub2api_password"}
    result = _request_json(
        origin,
        "/api/v1/auth/login",
        method="POST",
        body={"email": login_email, "password": login_password},
    )
    if not result.get("ok"):
        return result
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    token = str(data.get("access_token") or data.get("accessToken") or "").strip()
    if not token:
        return {"ok": False, "error": "sub2api_login_missing_access_token", "data": data}
    return {"ok": True, "token": token}


def _looks_like_sub2api_api_key(token):
    return str(token or "").strip().lower().startswith("sk-")


def _looks_like_sub2api_admin_key(token):
    return str(token or "").strip().lower().startswith("admin-")


def _resolve_group_ids(origin, token, group_name="", group_ids=None):
    parsed_ids = _parse_int_list(group_ids)
    if parsed_ids:
        return {"ok": True, "group_ids": parsed_ids}

    names = _parse_name_list(group_name) or [DEFAULT_GROUP_NAME]
    result = _request_json(origin, "/api/v1/admin/groups/all", token=token, method="GET")
    if not result.get("ok"):
        return result
    groups = _as_list(result.get("data"))
    matched, missing = [], []
    for name in names:
        group = next(
            (
                item for item in groups
                if str(item.get("name") or "").strip().lower() == name.lower()
                and str(item.get("platform") or "openai").strip().lower() in {"", "openai"}
            ),
            None,
        )
        if not group:
            missing.append(name)
            continue
        group_id = _as_int(group.get("id"))
        if group_id > 0:
            matched.append(group_id)
    if missing:
        return {"ok": False, "error": "sub2api_group_not_found", "missing": missing}
    if not matched:
        return {"ok": False, "error": "missing_sub2api_group_ids"}
    return {"ok": True, "group_ids": matched}


def _resolve_proxy_id(origin, token, proxy_name="", proxy_id=None):
    parsed_id = _as_int(proxy_id)
    if parsed_id > 0:
        return parsed_id
    parsed_ids = _parse_int_list(proxy_id)
    if parsed_ids:
        return random.choice(parsed_ids)
    name = str(proxy_name or "").strip()
    if not name:
        return None
    result = _request_json(origin, "/api/v1/admin/proxies/all?with_count=true", token=token, method="GET")
    if not result.get("ok"):
        return result
    proxies = _as_list(result.get("data"))
    lowered = name.lower()
    matches = [
        item for item in proxies
        if str(item.get("name") or "").strip().lower() == lowered
        or str(item.get("id") or "").strip() == name
    ]
    if len(matches) != 1:
        return {"ok": False, "error": "sub2api_proxy_not_found" if not matches else "sub2api_proxy_ambiguous", "proxy_name": name}
    return _as_int(matches[0].get("id")) or None


def _request_json(origin, path, token="", method="GET", body=None, timeout=30):
    url = _join_url(origin, path)
    headers = {"Accept": "application/json"}
    if token:
        if _looks_like_sub2api_admin_key(token):
            headers["x-api-key"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        response = curl_requests.request(
            method.upper(),
            url,
            headers=headers,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            timeout=timeout,
            impersonate="chrome110",
        )
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text[:500]}
        if response.status_code < 200 or response.status_code >= 300:
            return {
                "ok": False,
                "status_code": response.status_code,
                "error": _sub2api_error_text(payload, response.status_code),
                "data": payload,
            }
        data = _unwrap_sub2api_response(payload)
        return {"ok": True, "status_code": response.status_code, "data": data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _unwrap_sub2api_response(payload):
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return payload.get("data")
    return payload


def _parse_sub2api_account_page(data):
    if isinstance(data, dict):
        items = data.get("items") or data.get("rows") or data.get("accounts") or data.get("data") or []
        return _as_list(items), _as_int(data.get("total")), _as_int(data.get("pages"))
    return _as_list(data), 0, 0


def _sub2api_account_to_auth_file(item):
    item = item if isinstance(item, dict) else {}
    credentials = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    email = _normalize_email(
        credentials.get("email")
        or extra.get("email")
        or item.get("email")
        or item.get("name")
        or item.get("username")
    )
    error_message = str(item.get("error_message") or item.get("error") or item.get("temp_unschedulable_reason") or "").strip()
    probe = {}
    if re.search(r"\b401\b|unauthorized|invalid_grant|refresh_token|token", error_message, re.I):
        probe["status_code"] = 401
    return {
        **item,
        "email": email,
        "status": item.get("status") or "",
        "message": error_message,
        "error": error_message,
        "probe": probe,
    }


def _record_sub2api_import(email, path, upload_result, auth_mode=""):
    target_email = str(email or "").strip().lower()
    if not target_email:
        return
    data = {}
    record = get_account_record(target_email)
    raw_json = str(record.get("raw_json") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                data.update(parsed)
        except Exception:
            pass
    data.setdefault("email", target_email)
    data["sub2api_import"] = {
        "ok": bool(upload_result.get("ok")),
        "path": path,
        "mode": upload_result.get("mode", ""),
        "auth_mode": str(auth_mode or ""),
        "status_code": upload_result.get("status_code", 0),
        "created": upload_result.get("created", 0),
        "updated": upload_result.get("updated", 0),
        "failed": upload_result.get("failed", 0),
        "verified": bool((upload_result.get("verification") or {}).get("ok")),
        "updated_at": int(time.time()),
    }
    if upload_result.get("error"):
        data["sub2api_import"]["error"] = upload_result.get("error", "")
    upsert_account(data, json_path=record.get("json_path", ""))


def _extract_expires_at(token_data):
    value = (
        token_data.get("expires_at")
        or token_data.get("expiresAt")
        or token_data.get("expires")
        or token_data.get("expired")
    )
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    try:
        normalized = text.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp())
    except Exception:
        return 0


def _join_url(origin, path):
    return str(origin or "").rstrip("/") + "/" + str(path or "").lstrip("/")


def _parse_name_list(value):
    if isinstance(value, (list, tuple)):
        source = value
    else:
        source = re.split(r"[\r\n,;，；]+", str(value or ""))
    names, seen = [], set()
    for item in source:
        text = str(item or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            names.append(text)
    return names


def _parse_int_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        source = value
    else:
        source = re.split(r"[\r\n,;，；]+", str(value or ""))
    result = []
    for item in source:
        text = str(item or "").strip().lstrip("#")
        parsed = _as_int(text)
        if parsed > 0 and parsed not in result:
            result.append(parsed)
    return result


def _as_list(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _as_int(value):
    try:
        return int(value)
    except Exception:
        return 0


def _as_bool(value, default=False):
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _normalize_email(value):
    text = str(value or "").strip().lower()
    return text if re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", text) else ""


def _sub2api_error_text(payload, status_code):
    if isinstance(payload, dict):
        for key in ("message", "error", "detail", "reason", "raw"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value[:500]
    return f"SUB2API HTTP {status_code}"


def _sub2api_import_error(data):
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            return str(first.get("message") or first or "sub2api_import_failed")[:500]
    return "sub2api_import_failed"
