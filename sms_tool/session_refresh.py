import json
import re
import time
from pathlib import Path

from curl_cffi import requests as curl_requests

from .config import CFG
from .paths import output_dir
from .storage import get_account_record, list_paypal_accounts, upsert_account
from .http_utils import _minimal_chatgpt_cookie_header


def refresh_session(
    email="",
    session_file="",
    timeout=300,
    proxy=None,
    *,
    persist=True,
):
    """Refresh a ChatGPT session through the cookie-based protocol path.

    Browser-based re-login has been removed; recovery is protocol-only. Callers
    that need a full login when the cookie is dead should use the
    ``account_recovery`` chain (RT / cookie / protocol email-OTP / Codex OAuth).
    """
    data, json_path = _load_seed_session(email=email, session_file=session_file)
    target_email = (email or data.get("email") or "").strip().lower()
    timeout = max(30, int(timeout or 300))
    return _refresh_session_protocol(
        data,
        json_path,
        target_email,
        timeout,
        proxy=proxy,
        persist=persist,
    )


def _refresh_session_protocol(data, json_path, target_email, timeout, proxy=None, persist=True):
    cookie_header = _minimal_chatgpt_cookie_header(data.get("cookie_header") or "")
    cookie_header = _ensure_session_cookie(cookie_header, data)
    if not _has_session_cookie(cookie_header):
        return {"ok": False, "email": target_email, "mode": "protocol", "error": "missing_session_cookie"}

    auth_session = _fetch_protocol_auth_session(cookie_header, timeout=timeout, proxy=proxy)
    access_token = _session_token(auth_session, "accessToken", "access_token")
    oauth_refresh_token = _session_token(auth_session, "refreshToken", "refresh_token")
    if not access_token:
        return {"ok": False, "email": target_email, "mode": "protocol", "error": "auth_session_missing_access_token"}

    refreshed = _merge_refreshed_session(
        data=data,
        target_email=target_email,
        auth_session=auth_session,
        access_token=access_token,
        oauth_refresh_token=oauth_refresh_token,
        cookie_header=cookie_header,
    )
    return _finish_session_refresh(refreshed, json_path, "protocol", persist)


def _finish_session_refresh(refreshed, json_path, mode, persist):
    if not persist:
        return {
            "ok": True,
            "mode": mode,
            "email": refreshed.get("email", ""),
            "refresh_token_status": refreshed["refresh_token_status"],
            "persisted": False,
            "data": refreshed,
        }
    json_path = _save_refreshed(refreshed, json_path)
    return {
        "ok": True,
        "mode": mode,
        "email": refreshed.get("email", ""),
        "json_path": json_path,
        "refresh_token_status": refreshed["refresh_token_status"],
        "persisted": True,
    }


def _merge_refreshed_session(data, target_email, auth_session, access_token, oauth_refresh_token, cookie_header):
    refreshed = dict(data)
    if target_email:
        refreshed["email"] = target_email
    refreshed["success"] = True
    refreshed["access_token"] = access_token
    refreshed["auth_session"] = auth_session
    refreshed["cookie_header"] = cookie_header
    refreshed["oauth_refresh_token"] = oauth_refresh_token
    refreshed["refresh_token_status"] = "oauth_present" if oauth_refresh_token else "no_rt"
    refreshed["refresh_token_updated_at"] = int(time.time())
    refreshed["refreshed_at"] = int(time.time())
    return refreshed


def _save_refreshed(refreshed, json_path):
    if not json_path:
        json_path = _new_session_path(refreshed)
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(refreshed, ensure_ascii=False, indent=2), encoding="utf-8")
    upsert_account(refreshed, json_path=json_path)
    return json_path


def _load_seed_session(email="", session_file=""):
    if session_file:
        path = Path(session_file)
        data = _read_json(path)
        if not isinstance(data, dict):
            data = {}
        # Session snapshots intentionally omit mailbox credentials.  When a
        # caller supplies an explicit snapshot path, rehydrate the missing
        # mailbox columns from the canonical SQLite account record so recovery
        # can still use the configured mailbox provider.
        lookup_email = str(data.get("email") or email or "").strip().lower()
        if lookup_email:
            record = get_account_record(lookup_email)
            if record:
                data.setdefault("email", record.get("email", lookup_email))
                for key in (
                    "mailbox_provider",
                    "mailbox_source",
                    "mailbox_token",
                    "mailbox_refresh_token",
                ):
                    if not str(data.get(key) or "").strip() and str(record.get(key) or "").strip():
                        data[key] = record[key]
                if not str(data.get("password") or "").strip() and str(record.get("password") or "").strip():
                    data["password"] = record["password"]
        return data, str(path)
    if email:
        record = get_account_record(email)
        json_path = str(record.get("json_path") or "").strip()
        data = {}
        raw_json = str(record.get("raw_json") or "").strip()
        if raw_json:
            try:
                raw_data = json.loads(raw_json)
                if isinstance(raw_data, dict):
                    data.update(raw_data)
            except Exception:
                pass
        if json_path and Path(json_path).exists():
            file_data = _read_json(Path(json_path))
            if isinstance(file_data, dict):
                data = {**data, **file_data}
        if record:
            data.setdefault("email", record.get("email", ""))
            data.setdefault("access_token", record.get("access_token", ""))
            data.setdefault("oauth_refresh_token", record.get("oauth_refresh_token", ""))
            db_password = str(record.get("password") or "").strip()
            if not db_password:
                data["password"] = ""
            return data, json_path
        for row in list_paypal_accounts(email=email):
            json_path = str(row.get("json_path") or "").strip()
            if json_path and Path(json_path).exists():
                return _read_json(Path(json_path)), json_path
    return ({"email": email.strip().lower()} if email else {}, "")


def _fetch_protocol_auth_session(cookie_header, timeout=300, proxy=None):
    chat_base = CFG["chatgpt"].get("chat_base_url", "https://chatgpt.com").rstrip("/")
    deadline = time.time() + max(5, int(timeout or 30))
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": chat_base,
        "Referer": f"{chat_base}/",
        "Cookie": cookie_header,
    }
    last_status = ""
    while time.time() < deadline:
        try:
            response = session.get(
                f"{chat_base}/api/auth/session",
                headers=headers,
                impersonate="chrome124",
                timeout=30,
            )
            last_status = str(response.status_code)
            if response.status_code == 200:
                body = response.json()
                if _session_token(body, "accessToken", "access_token"):
                    print("[*] Protocol auth session refreshed.")
                    return body
        except Exception as e:
            last_status = str(e)
        print(f"[*] Waiting for protocol auth session... {last_status}")
        time.sleep(3)
    return {}


def _ensure_session_cookie(cookie_header, data):
    if _has_session_cookie(cookie_header):
        return cookie_header
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    session_token = (
        _session_token(auth_session, "sessionToken", "session_token")
        or str(data.get("session_token") or "").strip()
    )
    if not session_token:
        return cookie_header
    parts = [part.strip() for part in str(cookie_header or "").split(";") if part.strip()]
    parts.append(f"__Secure-next-auth.session-token={session_token}")
    return "; ".join(parts)


def _has_session_cookie(cookie_header):
    return any(
        item.strip().startswith("__Secure-next-auth.session-token=")
        for item in str(cookie_header or "").split(";")
    )


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _request_auth_session(ctx):
    chat_base = CFG["chatgpt"].get("chat_base_url", "https://chatgpt.com").rstrip("/")
    try:
        response = ctx.request.get(f"{chat_base}/api/auth/session", timeout=30000)
        if response.status == 200:
            body = response.json()
            return body if isinstance(body, dict) else {}
    except Exception:
        pass
    return {}


def _auth_session_email(data):
    if not isinstance(data, dict):
        return ""
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    session_user = session.get("user") if isinstance(session.get("user"), dict) else {}
    for value in (
        user.get("email"),
        account.get("email"),
        session_user.get("email"),
        data.get("email"),
    ):
        email = str(value or "").strip().lower()
        if email:
            return email
    return ""


def _poll_auth_session(ctx, timeout):
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        try:
            body = _request_auth_session(ctx)
            last_status = "200" if body else "unavailable"
            if _session_token(body, "accessToken", "access_token"):
                print("[*] Auth session refreshed.")
                return body
        except Exception as e:
            last_status = str(e)
        print(f"[*] Waiting for auth session... {last_status}")
        time.sleep(3)
    raise RuntimeError("timed out waiting for ChatGPT auth session")


def _session_token(data, *keys):
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    session = data.get("session")
    if isinstance(session, dict):
        for key in keys:
            value = session.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _new_session_path(data):
    directory = output_dir(CFG)
    email = (data.get("email") or "unknown").replace("+", "")
    safe_email = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", email)
    return str(directory / f"session_{safe_email}_{int(time.time())}.json")
