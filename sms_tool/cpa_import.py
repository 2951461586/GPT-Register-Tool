import base64
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from curl_cffi import CurlMime
from curl_cffi import requests as curl_requests

from .codex_export import build_codex_json
from .config import CFG
from .paths import output_dir
from .session_refresh import _load_seed_session
from .storage import get_account_record, upsert_account


CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_QUOTA_HEADERS = {
    "Authorization": "Bearer $TOKEN$",
    "Content-Type": "application/json",
    "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
}


def import_cpa_session(
    email="",
    session_file="",
    export_dir="",
    refresh=True,
    proxy=None,
    timeout=300,
    api_url="",
    api_token="",
):
    target_url, token = _resolve_cpa_config(api_url=api_url, api_token=api_token)
    target_email = (email or "").strip().lower()
    if not target_url:
        return {"ok": False, "email": target_email, "error": "missing_cpa_api_url"}
    if not token:
        return {"ok": False, "email": target_email, "error": "missing_cpa_api_token"}

    source_result = _load_cpa_source(target_email, session_file=session_file, export_dir=export_dir)
    if not source_result.get("ok"):
        return {
            "ok": False,
            "email": target_email,
            "error": source_result.get("error", "missing_cpa_source_json"),
            "message": source_result.get("message", ""),
            "source": source_result,
        }

    token_data, warnings = build_codex_json(source_result["data"])
    if not token_data.get("email"):
        token_data["email"] = target_email

    cpa_payload = _build_cpa_payload(token_data)
    source_path = source_result.get("path", "")
    refresh_token_status = "oauth_present" if str(token_data.get("refresh_token") or "").strip() else "no_rt"

    if not cpa_payload.get("ok"):
        upload_result = {
            "ok": False,
            "error": cpa_payload.get("error", "invalid_cpa_payload"),
            "message": cpa_payload.get("message", ""),
        }
        export_result = {
            "ok": False,
            "email": token_data.get("email", target_email),
            "path": source_path,
            "mode": "at_json",
            "source_path": source_path,
            "source_mode": source_result.get("mode", ""),
            "refresh_token_status": refresh_token_status,
            "warnings": warnings,
        }
        _record_cpa_import(export_result.get("email", target_email), source_path, upload_result)
        return {
            "ok": False,
            "email": export_result.get("email", target_email),
            "path": source_path,
            "cpa": upload_result,
            "export": export_result,
            "refresh_token_status": refresh_token_status,
            "warnings": warnings,
        }

    path = _write_cpa_json(cpa_payload["data"], export_dir)
    export_result = {
        "ok": True,
        "email": cpa_payload["data"].get("email", target_email),
        "path": path,
        "mode": "at_json",
        "source_path": source_path,
        "source_mode": source_result.get("mode", ""),
        "refresh_token_status": refresh_token_status,
        "warnings": warnings,
    }
    filename = Path(path).name
    upload_result = upload_to_cpa(cpa_payload["data"], target_url, token, filename=filename)
    _record_cpa_import(export_result.get("email", target_email), path, upload_result)
    return {
        "ok": upload_result.get("ok", False),
        "email": export_result.get("email", target_email),
        "path": path,
        "cpa": upload_result,
        "export": export_result,
        "refresh_token_status": refresh_token_status,
        "warnings": warnings,
    }


def import_cpa_sessions(
    emails,
    export_dir="",
    workers=1,
    refresh=True,
    proxy=None,
    timeout=300,
    api_url="",
    api_token="",
):
    emails = [str(email or "").strip() for email in emails if str(email or "").strip()]
    ordered = [None] * len(emails)
    max_workers = max(1, min(int(workers or 1), 4, len(emails) or 1))

    def _run(index, item_email):
        return index, import_cpa_session(
            email=item_email,
            export_dir=export_dir,
            refresh=refresh,
            proxy=proxy,
            timeout=timeout,
            api_url=api_url,
            api_token=api_token,
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
    return {
        **import_result,
        "emails": emails,
        "skipped": skipped,
        "source": {"count": len(auth_files_result.get("files", []))},
    }


def fetch_cpa_auth_files(api_url="", api_token="", timeout=30):
    target_url = _normalize_cpa_auth_files_url(api_url)
    if not target_url:
        return {"ok": False, "error": "missing_cpa_api_url"}
    if not api_token:
        return {"ok": False, "error": "missing_cpa_api_token"}
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_token}",
        "X-Management-Key": api_token,
    }
    try:
        response = curl_requests.get(
            target_url,
            headers=headers,
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
                "error": _cpa_error_text(payload, response.status_code),
            }
        return {
            "ok": True,
            "status_code": response.status_code,
            "files": _parse_cpa_auth_files_payload(payload),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def probe_cpa_codex_quota(item, api_url="", api_token="", timeout=30):
    if not isinstance(item, dict):
        return {"ok": False, "status": "unknown", "error": "invalid_auth_file"}
    auth_index = _normalize_auth_index(item.get("auth_index") or item.get("authIndex") or item.get("auth-index"))
    if not auth_index:
        return {"ok": False, "status": "unknown", "error": "missing_auth_index"}

    resolved_api_url, resolved_api_token = _resolve_cpa_config(api_url=api_url, api_token=api_token)
    api_token = resolved_api_token
    target_url = _normalize_cpa_api_call_url(resolved_api_url)
    if not target_url:
        return {"ok": False, "status": "unknown", "error": "missing_cpa_api_url"}
    if not api_token:
        return {"ok": False, "status": "unknown", "error": "missing_cpa_api_token"}

    request_headers = dict(CODEX_QUOTA_HEADERS)
    account_id = _extract_chatgpt_account_id(item)
    if account_id:
        request_headers["Chatgpt-Account-Id"] = account_id

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_token}",
        "X-Management-Key": api_token,
    }
    body = {
        "authIndex": auth_index,
        "method": "GET",
        "url": CODEX_USAGE_URL,
        "header": request_headers,
    }
    try:
        response = curl_requests.post(
            target_url,
            headers=headers,
            json=body,
            timeout=timeout,
            impersonate="chrome110",
        )
        try:
            payload = response.json()
        except Exception:
            payload = {"body": response.text[:500]}
        if response.status_code < 200 or response.status_code >= 300:
            return {
                "ok": False,
                "status": "unknown",
                "status_code": response.status_code,
                "error": _cpa_error_text(payload, response.status_code),
            }
        quota_status = _extract_api_call_status(payload)
        error_text = _extract_api_call_error_text(payload)
        status = "token_invalid" if _is_token_invalid_quota_response(quota_status, error_text) else "active"
        return {
            "ok": True,
            "status": status,
            "status_code": quota_status,
            "error": error_text,
        }
    except Exception as exc:
        return {"ok": False, "status": "unknown", "error": str(exc)}


def classify_cpa_auth_file(item):
    status = str((item or {}).get("status") or "").strip().lower()
    text = " ".join([
        str((item or {}).get("status") or ""),
        str((item or {}).get("status_message") or ""),
        str((item or {}).get("message") or ""),
        str((item or {}).get("error") or ""),
        str(((item or {}).get("probe") or {}).get("status_code") if isinstance((item or {}).get("probe"), dict) else ""),
        str(((item or {}).get("probe") or {}).get("statusCode") if isinstance((item or {}).get("probe"), dict) else ""),
        str(((item or {}).get("probe") or {}).get("error") if isinstance((item or {}).get("probe"), dict) else ""),
        str(((item or {}).get("probe") or {}).get("raw") if isinstance((item or {}).get("probe"), dict) else ""),
    ]).lower()
    probe = (item or {}).get("probe") if isinstance((item or {}).get("probe"), dict) else {}
    try:
        probe_status = int(probe.get("status_code") or probe.get("statusCode") or 0)
    except Exception:
        probe_status = 0
    if (
        re.search(r"\b401\b|unauthorized|auth_unavailable|authentication token has been invalidated|token has been invalidated|refresh_token_expired|refresh token expired|refresh_token_reused|refresh_token_invalidated|invalid_grant", text)
        or re.search(r"\bbanned\b|\bsuspended\b|\bdeactivated\b|\bterminated\b|account closed|account_locked|fraud|abuse|违反|封禁", text)
        or probe_status == 401
        or status in {"disabled", "unavailable"}
        or (item or {}).get("disabled") is True
        or (item or {}).get("unavailable") is True
    ):
        return "token_invalid"
    if status in {"active", "ok"}:
        return "active"
    if re.search(r"timeout|temporarily|context canceled", text):
        return "transient"
    return status or "unknown"


def extract_cpa_auth_email(item):
    if not isinstance(item, dict):
        return ""
    for key in ("email", "account", "username"):
        email = _normalize_email(item.get(key))
        if email:
            return email
    name = str(item.get("name") or item.get("id") or "").strip()
    if name.lower().endswith(".json"):
        name = name[:-5]
    return _normalize_email(name)


def upload_to_cpa(token_data, api_url, api_token, filename=""):
    upload_url = _normalize_cpa_auth_files_url(api_url)
    if not upload_url:
        return {"ok": False, "error": "missing_cpa_api_url"}
    filename = filename or f"codex-{token_data.get('email', 'unknown')}-plus.json"
    file_content = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")

    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        mime = CurlMime()
        mime.addpart(name="file", data=file_content, filename=filename, content_type="application/json")
        response = curl_requests.post(
            upload_url,
            multipart=mime,
            headers=headers,
            timeout=30,
            impersonate="chrome110",
        )
        if response.status_code in (200, 201):
            return {"ok": True, "mode": "multipart", "status_code": response.status_code, "filename": filename}

        if response.status_code in (404, 405, 415):
            fallback_url = f"{upload_url}?name={urllib.parse.quote(filename)}"
            fallback = curl_requests.post(
                fallback_url,
                data=file_content,
                headers={**headers, "Content-Type": "application/json"},
                timeout=30,
                impersonate="chrome110",
            )
            if fallback.status_code in (200, 201):
                return {
                    "ok": True,
                    "mode": "raw_json",
                    "status_code": fallback.status_code,
                    "filename": filename,
                }
            response = fallback

        return {
            "ok": False,
            "status_code": response.status_code,
            "filename": filename,
            "error": response.text[:500],
        }
    except Exception as exc:
        return {"ok": False, "filename": filename, "error": str(exc)}


def _build_cpa_payload(token_data):
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    id_token = str(token_data.get("id_token") or "").strip()
    if not access_token:
        return {"ok": False, "error": "missing_access_token", "message": "CPA导入缺少 access_token。"}

    payload = {
        "type": "codex",
        "account_id": str(token_data.get("account_id") or token_data.get("chatgpt_account_id") or "").strip(),
        "chatgpt_account_id": str(token_data.get("chatgpt_account_id") or token_data.get("account_id") or "").strip(),
        "email": str(token_data.get("email") or "").strip(),
        "name": str(token_data.get("name") or token_data.get("email") or "ChatGPT Account").strip(),
        "plan_type": str(token_data.get("plan_type") or token_data.get("chatgpt_plan_type") or "").strip(),
        "chatgpt_plan_type": str(token_data.get("chatgpt_plan_type") or token_data.get("plan_type") or "").strip(),
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session_token": str(token_data.get("session_token") or "").strip(),
        "last_refresh": str(token_data.get("last_refresh") or "").strip(),
        "expired": str(token_data.get("expired") or "").strip(),
        "disabled": bool(token_data.get("disabled", False)),
    }
    optional_empty = {
        "account_id",
        "chatgpt_account_id",
        "email",
        "name",
        "plan_type",
        "chatgpt_plan_type",
        "id_token",
        "refresh_token",
        "session_token",
        "last_refresh",
        "expired",
    }
    return {
        "ok": True,
        "data": {
            key: value
            for key, value in payload.items()
            if value != "" or key not in optional_empty
        },
    }


def _normalize_cpa_auth_files_url(api_url):
    normalized = str(api_url or "").strip().rstrip("/")
    lower = normalized.lower()
    if not normalized:
        return ""
    if lower.endswith("/auth-files"):
        return normalized
    if lower.endswith("/v0/management") or lower.endswith("/management"):
        return f"{normalized}/auth-files"
    if lower.endswith("/v0"):
        return f"{normalized}/management/auth-files"
    return f"{normalized}/v0/management/auth-files"


def _normalize_cpa_api_call_url(api_url):
    normalized = str(api_url or "").strip().rstrip("/")
    lower = normalized.lower()
    if not normalized:
        return ""
    if lower.endswith("/api-call"):
        return normalized
    if lower.endswith("/auth-files"):
        return normalized[: -len("/auth-files")] + "/api-call"
    if lower.endswith("/v0/management") or lower.endswith("/management"):
        return f"{normalized}/api-call"
    if lower.endswith("/v0"):
        return f"{normalized}/management/api-call"
    return f"{normalized}/v0/management/api-call"


def _normalize_auth_index(value):
    text = str(value or "").strip()
    return text if text else ""


def _extract_chatgpt_account_id(item):
    candidates = []
    for container in (item, item.get("metadata"), item.get("attributes")):
        if isinstance(container, dict):
            candidates.append(container.get("id_token"))
    for candidate in candidates:
        account_id = _extract_chatgpt_account_id_from_token(candidate)
        if account_id:
            return account_id
    return ""


def _extract_chatgpt_account_id_from_token(value):
    if isinstance(value, dict):
        return str(value.get("chatgpt_account_id") or value.get("chatgptAccountId") or "").strip()
    token = str(value or "").strip()
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("chatgpt_account_id") or data.get("chatgptAccountId") or "").strip()


def _extract_api_call_status(payload):
    if not isinstance(payload, dict):
        return 0
    for key in ("status_code", "statusCode"):
        try:
            value = int(payload.get(key) or 0)
        except Exception:
            value = 0
        if value:
            return value
    return 0


def _extract_api_call_error_text(payload):
    if not isinstance(payload, dict):
        return str(payload or "")[:500]
    values = []
    body = payload.get("body")
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            values.append(error.get("message"))
            values.append(error.get("code"))
        else:
            values.append(error)
        values.append(body.get("message"))
    else:
        values.append(body)
    values.append(payload.get("bodyText"))
    values.append(payload.get("error"))
    values.append(payload.get("message"))
    text = " ".join(str(value or "") for value in values if str(value or "").strip()).strip()
    return text[:500]


def _is_token_invalid_quota_response(status_code, error_text):
    text = str(error_text or "").lower()
    return (
        int(status_code or 0) == 401
        or re.search(
            r"\b401\b|unauthorized|authentication token has been invalidated|token has been invalidated|invalid_grant|refresh_token",
            text,
        )
        is not None
    )


def _parse_cpa_auth_files_payload(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    candidates = [
        payload.get("data"),
        payload.get("items"),
        payload.get("files"),
        payload.get("auth_files"),
        payload.get("authFiles"),
        payload.get("results"),
        payload.get("rows"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            nested = _parse_cpa_auth_files_payload(candidate)
            if nested:
                return nested
    return []


def _normalize_email(value):
    text = str(value or "").strip().lower()
    return text if re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", text) else ""


def _normalize_domain_filter(value):
    text = str(value or "").strip().lower().lstrip("@")
    return text if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", text) else ""


def _cpa_error_text(payload, status_code):
    if isinstance(payload, dict):
        for key in ("error", "message", "detail", "reason", "raw"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value[:500]
    return f"CPA HTTP {status_code}"


def _resolve_cpa_config(api_url="", api_token=""):
    cpa = CFG.get("cpa") if isinstance(CFG.get("cpa"), dict) else {}
    cpa_mode = CFG.get("cpa_mode") if isinstance(CFG.get("cpa_mode"), dict) else {}
    resolved_url = (
        str(api_url or "").strip()
        or str(cpa.get("api_url") or "").strip()
        or str(cpa_mode.get("api_url") or "").strip()
    )
    resolved_token = (
        str(api_token or "").strip()
        or str(cpa.get("api_token") or cpa.get("api_key") or "").strip()
        or str(cpa_mode.get("api_token") or cpa_mode.get("api_key") or "").strip()
    )
    return resolved_url, resolved_token


def _load_cpa_source(email="", session_file="", export_dir=""):
    data, json_path = _load_seed_session(email=email, session_file=session_file)
    if isinstance(data, dict) and _has_access_token(data):
        return {
            "ok": True,
            "data": data,
            "path": json_path or session_file or "",
            "mode": "session_json",
        }

    existing = _existing_cpa_json_with_access_token(email, export_dir)
    if existing:
        try:
            return {
                "ok": True,
                "data": json.loads(Path(existing).read_text(encoding="utf-8-sig")),
                "path": existing,
                "mode": "existing_at_json",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"read_existing_at_json_failed: {exc}",
                "path": existing,
            }

    return {
        "ok": False,
        "error": "missing_at_json",
        "message": "CPA导入需要已有 access_token 的 JSON 文件；当前账号未找到可导入的 AT JSON。",
        "path": json_path or session_file or "",
    }


def _has_access_token(data):
    if not isinstance(data, dict):
        return False
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    candidates = [
        data.get("accessToken"),
        data.get("access_token"),
        (data.get("token") or {}).get("accessToken") if isinstance(data.get("token"), dict) else "",
        (data.get("token") or {}).get("access_token") if isinstance(data.get("token"), dict) else "",
        (data.get("credentials") or {}).get("accessToken") if isinstance(data.get("credentials"), dict) else "",
        (data.get("credentials") or {}).get("access_token") if isinstance(data.get("credentials"), dict) else "",
        auth_session.get("accessToken") if isinstance(auth_session, dict) else "",
        auth_session.get("access_token") if isinstance(auth_session, dict) else "",
        (auth_session.get("session") or {}).get("accessToken") if isinstance(auth_session.get("session"), dict) else "",
        (auth_session.get("session") or {}).get("access_token") if isinstance(auth_session.get("session"), dict) else "",
    ]
    return any(str(value or "").strip() for value in candidates)


def _write_cpa_json(token_data, export_dir=""):
    directory = Path(export_dir) if export_dir else output_dir(CFG) / "codex_exports"
    directory.mkdir(parents=True, exist_ok=True)
    email = str(token_data.get("email") or "unknown").strip()
    safe_email = "".join(ch if ch.isalnum() or ch in "_.@+-" else "_" for ch in email)
    path = directory / f"codex-{safe_email}-plus.json"
    path.write_text(json.dumps(token_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return str(path)


def _existing_cpa_json_with_access_token(email, export_dir=""):
    target_email = str(email or "").strip()
    if not target_email:
        return ""
    directory = Path(export_dir) if export_dir else output_dir(CFG) / "codex_exports"
    safe_email = "".join(ch if ch.isalnum() or ch in "_.@+-" else "_" for ch in target_email)
    candidates = [
        directory / f"codex-{safe_email}-plus.json",
        directory / f"codex-{safe_email}.json",
    ]
    for path in candidates:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if _has_access_token(data):
                return str(path)
        except Exception:
            continue
    return ""


def _record_cpa_import(email, path, upload_result):
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
    data["cpa_import"] = {
        "ok": bool(upload_result.get("ok")),
        "path": path,
        "filename": upload_result.get("filename", ""),
        "mode": upload_result.get("mode", ""),
        "status_code": upload_result.get("status_code", 0),
        "updated_at": int(time.time()),
    }
    if upload_result.get("error"):
        data["cpa_import"]["error"] = upload_result.get("error", "")
    upsert_account(data, json_path=record.get("json_path", ""))
