import json
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from curl_cffi import CurlMime
from curl_cffi import requests as curl_requests

from .codex_export import export_codex_session
from .config import CFG
from .paths import output_dir
from .storage import get_account_record, upsert_account


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

    existing = _existing_cpa_json_with_rt(target_email, export_dir)
    export_result = {"ok": True, "email": target_email, "path": existing, "mode": "existing_rt_json"} if existing else None
    if export_result is None:
        export_result = export_codex_session(
            email=email,
            session_file=session_file,
            export_dir=export_dir,
            refresh=refresh,
            proxy=proxy,
            timeout=timeout,
            require_refresh_token=True,
            force_email_otp_login=True,
        )
        if not export_result.get("ok"):
            return {
                "ok": False,
                "email": target_email or export_result.get("email", ""),
                "error": "export_failed",
                "export": export_result,
            }

    path = export_result.get("path", "")
    try:
        token_data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "email": export_result.get("email", target_email),
            "error": f"read_export_failed: {exc}",
            "export": export_result,
        }

    cpa_payload = _build_cpa_payload(token_data)
    if not cpa_payload.get("ok"):
        upload_result = {
            "ok": False,
            "error": cpa_payload.get("error", "invalid_cpa_payload"),
            "message": cpa_payload.get("message", ""),
        }
        _record_cpa_import(export_result.get("email", target_email), path, upload_result)
        return {
            "ok": False,
            "email": export_result.get("email", target_email),
            "path": path,
            "cpa": upload_result,
            "export": export_result,
            "refresh_token_status": export_result.get("refresh_token_status", ""),
            "warnings": export_result.get("warnings", []),
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
        "refresh_token_status": export_result.get("refresh_token_status", ""),
        "warnings": export_result.get("warnings", []),
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
    if not refresh_token.startswith("rt_"):
        return {
            "ok": False,
            "error": "missing_refresh_token_for_cpa",
            "message": "CPA导入必须带 OpenAI refresh_token(rt_开头)，无RT账号导入后不可用，已跳过上传。",
        }
    if not id_token or token_data.get("id_token_synthetic"):
        return {
            "ok": False,
            "error": "missing_real_id_token_for_cpa",
            "message": "CPA导入需要真实 id_token；当前账号没有真实 id_token，已跳过上传。",
        }
    return {
        "ok": True,
        "data": {
            "access_token": access_token,
            "account_id": str(token_data.get("account_id") or token_data.get("chatgpt_account_id") or "").strip(),
            "disabled": bool(token_data.get("disabled", False)),
            "email": str(token_data.get("email") or "").strip(),
            "expired": str(token_data.get("expired") or "").strip(),
            "id_token": id_token,
            "last_refresh": str(token_data.get("last_refresh") or "").strip(),
            "refresh_token": refresh_token,
            "type": "codex",
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


def _existing_cpa_json_with_rt(email, export_dir=""):
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
            if str(data.get("refresh_token") or "").strip().startswith("rt_") and data.get("id_token"):
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
