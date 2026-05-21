import base64
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests as curl_requests

from .config import CFG
from .codex_oauth import refresh_codex_oauth_session
from .paths import output_dir
from .session_refresh import _load_seed_session, _session_token, refresh_session
from .storage import upsert_account


def export_codex_session(
    email="",
    session_file="",
    export_dir="",
    refresh=True,
    proxy=None,
    timeout=300,
    require_refresh_token=False,
    force_email_otp_login=False,
):
    data, json_path = _load_seed_session(email=email, session_file=session_file)
    target_email = (email or data.get("email") or "").strip().lower()
    if not target_email:
        return {"ok": False, "error": "missing_email"}

    refresh_result = {"ok": False, "mode": "none", "error": "refresh_disabled"}
    if refresh:
        refresh_result = _refresh_seed(
            data,
            json_path,
            target_email,
            proxy=proxy,
            timeout=timeout,
            require_refresh_token=require_refresh_token,
            force_email_otp_login=force_email_otp_login,
        )
        if refresh_result.get("ok"):
            data, json_path = _load_seed_session(email=target_email, session_file=json_path)

    codex_json, warnings = build_codex_json(data)
    if not codex_json.get("access_token"):
        return {
            "ok": False,
            "email": target_email,
            "error": "missing_access_token",
            "refresh": refresh_result,
        }

    if not codex_json.get("email"):
        codex_json["email"] = target_email

    if require_refresh_token and not str(codex_json.get("refresh_token") or "").strip().startswith("rt_"):
        return {
            "ok": False,
            "email": target_email,
            "error": "missing_refresh_token_for_cpa",
            "message": "CPA导入必须先拿到 OpenAI refresh_token(rt_开头)，当前账号已跳过无RT导出。",
            "refresh": refresh_result,
            "refresh_token_status": "no_rt",
            "warnings": warnings,
        }

    path = _codex_export_path(codex_json["email"], export_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(codex_json, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    data["codex_session"] = codex_json
    data["refresh_token_status"] = "oauth_present" if codex_json.get("refresh_token") else "no_rt"
    data["codex_export"] = {
        "path": str(path),
        "updated_at": int(time.time()),
        "refresh_mode": refresh_result.get("mode", "none"),
        "warnings": warnings,
    }
    if json_path:
        try:
            Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            data.setdefault("codex_export", {})["source_write_error"] = str(exc)
    upsert_account(data, json_path=json_path)

    return {
        "ok": True,
        "email": codex_json["email"],
        "path": str(path),
        "refresh": refresh_result,
        "refresh_token_status": data.get("refresh_token_status", ""),
        "warnings": warnings,
    }


def export_codex_sessions(
    emails,
    export_dir="",
    workers=1,
    refresh=True,
    proxy=None,
    timeout=300,
    require_refresh_token=False,
    force_email_otp_login=False,
):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ordered = [None] * len(emails)
    max_workers = max(1, min(int(workers or 1), 4, len(emails) or 1))

    def _run(index, item_email):
        return index, export_codex_session(
            email=item_email,
            export_dir=export_dir,
            refresh=refresh,
            proxy=proxy,
            timeout=timeout,
            require_refresh_token=require_refresh_token,
            force_email_otp_login=force_email_otp_login,
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


def build_codex_json(data):
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    access_token = _first_non_empty(
        data.get("accessToken"),
        data.get("access_token"),
        _nested(data, "token", "accessToken"),
        _nested(data, "token", "access_token"),
        _nested(data, "credentials", "accessToken"),
        _nested(data, "credentials", "access_token"),
        _session_token(auth_session, "accessToken", "access_token"),
    )
    session_token = _first_non_empty(
        data.get("sessionToken"),
        data.get("session_token"),
        _nested(data, "token", "sessionToken"),
        _nested(data, "token", "session_token"),
        _nested(data, "credentials", "session_token"),
        _session_token(auth_session, "sessionToken", "session_token"),
    )
    input_id_token = _first_non_empty(
        data.get("idToken"),
        data.get("id_token"),
        _nested(data, "token", "idToken"),
        _nested(data, "token", "id_token"),
        _nested(data, "credentials", "id_token"),
        _session_token(auth_session, "idToken", "id_token"),
    )
    refresh_token = _openai_refresh_token(data, auth_session)
    access_claims = _jwt_claims(access_token)
    id_claims = _jwt_claims(input_id_token)
    auth_claims = access_claims.get("https://api.openai.com/auth") if isinstance(access_claims.get("https://api.openai.com/auth"), dict) else {}
    id_auth_claims = id_claims.get("https://api.openai.com/auth") if isinstance(id_claims.get("https://api.openai.com/auth"), dict) else {}
    profile_claims = access_claims.get("https://api.openai.com/profile") if isinstance(access_claims.get("https://api.openai.com/profile"), dict) else {}
    user = auth_session.get("user") if isinstance(auth_session.get("user"), dict) else {}

    email = _first_non_empty(
        _nested(data, "user", "email"),
        data.get("email"),
        _nested(data, "credentials", "email"),
        _nested(data, "providerSpecificData", "email"),
        profile_claims.get("email"),
        id_claims.get("email"),
        access_claims.get("email"),
        user.get("email"),
    )
    account_id = _first_non_empty(
        _nested(data, "account", "id"),
        data.get("account_id"),
        data.get("chatgptAccountId"),
        _nested(data, "providerSpecificData", "chatgptAccountId"),
        _nested(data, "providerSpecificData", "chatgpt_account_id"),
        _nested(data, "credentials", "chatgpt_account_id"),
        auth_claims.get("chatgpt_account_id"),
        id_auth_claims.get("chatgpt_account_id"),
        data.get("id") if data.get("provider") == "codex" else "",
    )
    user_id = _first_non_empty(
        _nested(data, "user", "id"),
        data.get("user_id"),
        data.get("chatgptUserId"),
        _nested(data, "providerSpecificData", "chatgptUserId"),
        _nested(data, "providerSpecificData", "chatgpt_user_id"),
        auth_claims.get("chatgpt_user_id"),
        auth_claims.get("user_id"),
        id_auth_claims.get("chatgpt_user_id"),
        id_auth_claims.get("user_id"),
    )
    plan_type = _first_non_empty(
        _nested(data, "account", "planType"),
        _nested(data, "account", "plan_type"),
        data.get("planType"),
        data.get("plan_type"),
        _nested(data, "providerSpecificData", "chatgptPlanType"),
        _nested(data, "providerSpecificData", "chatgpt_plan_type"),
        _nested(data, "credentials", "plan_type"),
        auth_claims.get("chatgpt_plan_type"),
        id_auth_claims.get("chatgpt_plan_type"),
    )
    expires_at = _first_non_empty(
        _timestamp_from_unix(access_claims.get("exp")),
        _normalize_timestamp(data.get("expires")),
        _normalize_timestamp(data.get("expiresAt")),
        _normalize_timestamp(data.get("expired")),
        _normalize_timestamp(data.get("expires_at")),
        _timestamp_from_unix(id_claims.get("exp")),
    )
    synthetic_id_token = ""
    if not input_id_token:
        synthetic_id_token = _build_synthetic_codex_id_token(
            email=email,
            account_id=account_id,
            plan_type=plan_type,
            user_id=user_id,
            expires_at=expires_at,
        )
    id_token = _first_non_empty(input_id_token, synthetic_id_token)
    warnings = []
    if synthetic_id_token:
        warnings.append("Missing real id_token; generated synthetic CPA-compatible id_token.")
    if not refresh_token:
        warnings.append("Missing refresh_token; exported CPA JSON cannot refresh automatically after access token expiry.")

    output = {
        "type": "codex",
        "account_id": account_id,
        "chatgpt_account_id": account_id,
        "email": email,
        "name": _first_non_empty(data.get("name"), email, "ChatGPT Account"),
        "plan_type": plan_type,
        "chatgpt_plan_type": plan_type,
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session_token": session_token,
        "last_refresh": _iso_utc(int(time.time())),
        "expired": expires_at,
        "disabled": bool(data.get("disabled", False)),
    }
    if synthetic_id_token:
        output["id_token_synthetic"] = True
    optional_empty_fields = {"name", "plan_type", "chatgpt_plan_type", "id_token", "session_token", "expired"}
    filtered = {}
    for key, value in output.items():
        if value is None:
            continue
        if value == "" and key in optional_empty_fields:
            continue
        filtered[key] = value
    return filtered, warnings


def _refresh_seed(data, json_path, target_email, proxy=None, timeout=300, require_refresh_token=False, force_email_otp_login=False):
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    refresh_token = _openai_refresh_token(data, auth_session)
    if refresh_token:
        refreshed = _refresh_with_openai_oauth(data, refresh_token, proxy=proxy)
        if refreshed.get("ok"):
            data.update(refreshed["data"])
            data["refresh_token_status"] = "oauth_present"
            data["refresh_token_updated_at"] = int(time.time())
            if json_path:
                Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            upsert_account(data, json_path=json_path)
            return {"ok": True, "mode": "oauth_refresh"}
        return {"ok": False, "mode": "oauth_refresh", "error": refreshed.get("error", "oauth_refresh_failed")}

    oauth_result = refresh_codex_oauth_session(
        data,
        json_path=json_path,
        proxy=proxy,
        timeout=timeout,
        force_email_otp_login=force_email_otp_login,
    )
    if oauth_result.get("ok"):
        return oauth_result

    print(f"[*] Codex OAuth PKCE did not return RT: {oauth_result.get('error', 'unknown')}")
    if require_refresh_token:
        return oauth_result
    result = refresh_session(email=target_email, session_file=json_path, timeout=timeout, browser=False, proxy=proxy)
    result.setdefault("mode", "protocol_auth_session")
    result["oauth_attempt"] = oauth_result
    return result


def _refresh_with_openai_oauth(data, refresh_token, proxy=None):
    auth_base = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com").rstrip("/")
    client_id = (
        str((CFG.get("chatgpt") or {}).get("codex_oauth_client_id") or "").strip()
        or _jwt_claims(str(data.get("access_token") or "")).get("client_id")
        or "app_EMoamEEZ73f0CkXaXp7hrann"
    )
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        response = session.post(
            f"{auth_base}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
            },
            impersonate="chrome",
            timeout=30,
        )
        body = response.json() if response.text else {}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if response.status_code >= 400:
        return {"ok": False, "error": f"HTTP {response.status_code}: {json.dumps(body, ensure_ascii=False)[:300]}"}
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        return {"ok": False, "error": "oauth_response_missing_access_token"}
    refreshed = {
        "success": True,
        "access_token": access_token,
        "id_token": str(body.get("id_token") or data.get("id_token") or "").strip(),
        "oauth_refresh_token": str(body.get("refresh_token") or refresh_token).strip(),
        "refreshed_at": int(time.time()),
    }
    return {"ok": True, "data": refreshed}


def _openai_refresh_token(data, auth_session):
    candidates = [
        data.get("oauth_refresh_token"),
        auth_session.get("refreshToken") if isinstance(auth_session, dict) else "",
        auth_session.get("refresh_token") if isinstance(auth_session, dict) else "",
        _session_token(auth_session, "refreshToken", "refresh_token"),
        data.get("refresh_token"),
    ]
    for value in candidates:
        token = str(value or "").strip()
        if token.startswith("rt_"):
            return token
    return ""


def _codex_export_path(email, export_dir):
    directory = Path(export_dir) if export_dir else output_dir(CFG) / "codex_exports"
    safe_email = re.sub(r"[^a-zA-Z0-9_.@+-]+", "_", (email or "unknown").strip())
    return directory / f"codex-{safe_email}-plus.json"


def _jwt_claims(token):
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _first_non_empty(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list, tuple, set)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _nested(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if current is not None else ""


def _build_synthetic_codex_id_token(email, account_id, plan_type, user_id, expires_at):
    if not account_id:
        return ""
    now = int(time.time())
    exp = _epoch_seconds_from_value(expires_at) or now + 90 * 24 * 60 * 60
    auth_info = {"chatgpt_account_id": account_id}
    if plan_type:
        auth_info["chatgpt_plan_type"] = plan_type
    if user_id:
        auth_info["chatgpt_user_id"] = user_id
        auth_info["user_id"] = user_id
    payload = {
        "iat": now,
        "exp": exp,
        "https://api.openai.com/auth": auth_info,
    }
    if email:
        payload["email"] = email
    return f"{_base64url_json({'alg': 'none', 'typ': 'JWT', 'cpa_synthetic': True})}.{_base64url_json(payload)}."


def _base64url_json(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _timestamp_from_unix(value):
    seconds = _as_int(value)
    return _iso_utc(seconds) if seconds else ""


def _normalize_timestamp(value):
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:
            seconds = seconds / 1000
        return _iso_utc(seconds)
    text = str(value).strip()
    if not text:
        return ""
    try:
        if text.isdigit():
            return _timestamp_from_unix(int(text))
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _format_utc(parsed.astimezone(timezone.utc))
    except Exception:
        return ""


def _epoch_seconds_from_value(value):
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        raw = float(value)
        return int(raw / 1000 if raw > 1e11 else raw)
    text = str(value).strip()
    if not text:
        return 0
    try:
        if text.isdigit():
            raw = int(text)
            return int(raw / 1000 if raw > 1e11 else raw)
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return 0


def _iso_utc(epoch_seconds):
    return _format_utc(datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc))


def _format_utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _as_int(value):
    try:
        return int(value)
    except Exception:
        return 0
