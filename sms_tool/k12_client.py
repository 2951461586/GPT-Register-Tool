import time
import uuid

from curl_cffi import requests as curl_requests

from .config import CFG
from .http_client import is_transient_transport_error, request_with_retry
from .k12_identity import _extract_access_token, _extract_account_id_from_data, _extract_user_id_from_data


def normalize_k12_route(route):
    value = str(route or "").strip().lower()
    if value in {"leave", "exit", "quit", "remove", "delete", "workspace_leave"}:
        return "leave"
    if value in {"accept", "join", "invite_accept"}:
        return "accept"
    return "request"

def _refresh_access_token_from_cookie(account, proxy=None, timeout=30):
    cookie = str(account.get("cookie_header") or "").strip()
    if not cookie:
        return ""
    chat_base = (CFG.get("chatgpt") or {}).get("chat_base_url", "https://chatgpt.com").rstrip("/")
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    headers = {
        "accept": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
    }
    try:
        response = request_with_retry(
            session,
            "get",
            f"{chat_base}/api/auth/session",
            label="session refresh",
            headers=headers,
            timeout=timeout,
            impersonate="chrome124",
        )
    except Exception as exc:
        if is_transient_transport_error(exc):
            raise RuntimeError(f"session refresh transport: {exc}")
        raise
    try:
        body = response.json()
    except Exception:
        body = {}
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"session refresh HTTP {response.status_code}: {response.text[:200]}")
    token = _extract_access_token(body)
    if not token:
        raise RuntimeError(
            "session refresh returned empty accessToken "
            f"(HTTP {response.status_code}, content-type={response.headers.get('content-type', '')}, "
            f"body={response.text[:160]!r})"
        )
    account["access_token"] = token
    return token



def _fetch_auth_session_from_cookie(account, proxy=None, timeout=30):
    cookie = str(account.get("cookie_header") or "").strip()
    if not cookie:
        return {}
    chat_base = (CFG.get("chatgpt") or {}).get("chat_base_url", "https://chatgpt.com").rstrip("/")
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    headers = {
        "accept": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
    }
    try:
        response = request_with_retry(
            session,
            "get",
            f"{chat_base}/api/auth/session",
            label="auth session",
            headers=headers,
            timeout=timeout,
            impersonate="chrome124",
        )
    except Exception as exc:
        if is_transient_transport_error(exc):
            raise RuntimeError(f"session fetch transport: {exc}")
        raise
    try:
        body = response.json()
    except Exception:
        body = {}
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"session fetch HTTP {response.status_code}: {response.text[:200]}")
    if not isinstance(body, dict):
        return {}
    token = _extract_access_token(body)
    if token:
        account["access_token"] = token
    user_id = _extract_user_id_from_data(body)
    if user_id:
        account["user_id"] = user_id
    account_id = _extract_account_id_from_data(body)
    if account_id:
        account["account_id"] = account_id
    raw = account.get("raw") if isinstance(account.get("raw"), dict) else {}
    raw["auth_session"] = body
    account["raw"] = raw
    return body



def _delete_workspace_user(account, workspace_id="", proxy=None, timeout=30, max_retries=0, retry_backoff=5, fetch_auth_session_func=None):
    raw = account.get("raw") if isinstance(account.get("raw"), dict) else {}
    account_id = str(workspace_id or account.get("account_id") or _extract_account_id_from_data(raw) or "").strip()
    user_id = str(account.get("user_id") or _extract_user_id_from_data(raw) or "").strip()
    if not user_id or not account_id:
        try:
            (fetch_auth_session_func or _fetch_auth_session_from_cookie)(account, proxy=proxy, timeout=timeout)
            raw = account.get("raw") if isinstance(account.get("raw"), dict) else {}
            user_id = str(account.get("user_id") or _extract_user_id_from_data(raw) or "").strip()
            if not account_id:
                account_id = str(account.get("account_id") or _extract_account_id_from_data(raw) or "").strip()
        except Exception as exc:
            return {
                "ok": False,
                "email": account.get("email", ""),
                "workspace_id": account_id or workspace_id,
                "route": "leave",
                "status": 0,
                "error": f"missing_user_id:{exc}",
            }
    if not account_id:
        return {
            "ok": False,
            "email": account.get("email", ""),
            "workspace_id": workspace_id,
            "route": "leave",
            "status": 0,
            "error": "missing_account_id",
        }
    if not user_id:
        return {
            "ok": False,
            "email": account.get("email", ""),
            "workspace_id": account_id,
            "route": "leave",
            "status": 0,
            "error": "missing_user_id",
        }

    chat_base = (CFG.get("chatgpt") or {}).get("chat_base_url", "https://chatgpt.com").rstrip("/")
    url = f"{chat_base}/backend-api/accounts/{account_id}/users/{user_id}"
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    headers = {
        "accept": "application/json",
        "authorization": "Bearer " + str(account.get("access_token") or "").strip(),
        "content-type": "application/json",
        "oai-device-id": str(account.get("device_id") or "").strip() or str(uuid.uuid4()),
        "oai-language": "en-US",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
    }
    cookie = str(account.get("cookie_header") or "").strip()
    if cookie:
        headers["cookie"] = cookie

    started = time.time()
    attempts = max(0, int(max_retries or 0)) + 1
    last_result = {}
    refresh_errors = []
    for attempt in range(attempts):
        headers["authorization"] = "Bearer " + str(account.get("access_token") or "").strip()
        try:
            response = session.delete(
                url,
                headers=headers,
                timeout=timeout,
                impersonate="chrome124",
            )
            text = response.text or ""
            ok = 200 <= int(response.status_code) < 300
            last_result = {
                "ok": ok,
                "email": account.get("email", ""),
                "workspace_id": account_id,
                "account_id": account_id,
                "user_id": user_id,
                "route": "leave",
                "status": response.status_code,
                "body": text[:500],
                "attempt": attempt + 1,
                "seconds": round(time.time() - started, 2),
            }
            if ok:
                return last_result
            if response.status_code in (401, 403) and attempt < attempts - 1:
                try:
                    (fetch_auth_session_func or _fetch_auth_session_from_cookie)(account, proxy=proxy, timeout=timeout)
                    headers["authorization"] = "Bearer " + str(account.get("access_token") or "").strip()
                    last_result["token_refreshed"] = True
                except Exception as refresh_exc:
                    refresh_errors.append(str(refresh_exc))
                    last_result["refresh_error"] = str(refresh_exc)
        except Exception as exc:
            last_result = {
                "ok": False,
                "email": account.get("email", ""),
                "workspace_id": account_id,
                "account_id": account_id,
                "user_id": user_id,
                "route": "leave",
                "status": 0,
                "error": str(exc),
                "attempt": attempt + 1,
                "seconds": round(time.time() - started, 2),
            }
        if refresh_errors:
            last_result["refresh_errors"] = refresh_errors[-3:]
        if attempt < attempts - 1:
            time.sleep(max(0.0, float(retry_backoff or 0)))
    return last_result



def _post_workspace_invite(account, workspace_id, route="request", proxy=None, timeout=30, max_retries=0, retry_backoff=5, refresh_access_token_func=None):
    route = normalize_k12_route(route)
    chat_base = (CFG.get("chatgpt") or {}).get("chat_base_url", "https://chatgpt.com").rstrip("/")
    url = f"{chat_base}/backend-api/accounts/{workspace_id}/invites/{route}"
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    headers = {
        "accept": "*/*",
        "authorization": "Bearer " + str(account.get("access_token") or "").strip(),
        "content-type": "application/json",
        "oai-device-id": str(account.get("device_id") or "").strip() or str(uuid.uuid4()),
        "oai-language": "en-US",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
    }
    cookie = str(account.get("cookie_header") or "").strip()
    if cookie:
        headers["cookie"] = cookie

    started = time.time()
    attempts = max(0, int(max_retries or 0)) + 1
    last_result = {}
    refresh_errors = []
    for attempt in range(attempts):
        headers["authorization"] = "Bearer " + str(account.get("access_token") or "").strip()
        try:
            response = session.post(
                url,
                headers=headers,
                data="",
                timeout=timeout,
                impersonate="chrome124",
            )
            text = response.text or ""
            ok = 200 <= int(response.status_code) < 300
            last_result = {
                "ok": ok,
                "email": account.get("email", ""),
                "workspace_id": workspace_id,
                "route": route,
                "status": response.status_code,
                "body": text[:500],
                "attempt": attempt + 1,
                "seconds": round(time.time() - started, 2),
            }
            if ok:
                return last_result
            if response.status_code in (401, 403) and attempt < attempts - 1:
                try:
                    (refresh_access_token_func or _refresh_access_token_from_cookie)(account, proxy=proxy, timeout=timeout)
                    last_result["token_refreshed"] = True
                except Exception as refresh_exc:
                    refresh_errors.append(str(refresh_exc))
                    last_result["refresh_error"] = str(refresh_exc)
        except Exception as exc:
            last_result = {
                "ok": False,
                "email": account.get("email", ""),
                "workspace_id": workspace_id,
                "route": route,
                "status": 0,
                "error": str(exc),
                "attempt": attempt + 1,
                "seconds": round(time.time() - started, 2),
            }
        if refresh_errors:
            last_result["refresh_errors"] = refresh_errors[-3:]
        if attempt < attempts - 1:
            time.sleep(max(0.0, float(retry_backoff or 0)))
    return last_result


