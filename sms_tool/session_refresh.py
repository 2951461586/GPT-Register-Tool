import json
import re
import time
from pathlib import Path

from curl_cffi import requests as curl_requests

from .config import CFG
from .paths import output_dir
from .storage import get_account_record, list_paypal_accounts, upsert_account


def refresh_session(email="", session_file="", timeout=300, headless=False, browser=False, proxy=None):
    """Refresh ChatGPT session. Protocol mode is the default; browser mode is opt-in."""
    data, json_path = _load_seed_session(email=email, session_file=session_file)
    target_email = (email or data.get("email") or "").strip().lower()
    timeout = max(30, int(timeout or 300))

    if browser:
        return _refresh_session_browser(data, json_path, target_email, timeout, headless)
    return _refresh_session_protocol(data, json_path, target_email, timeout, proxy=proxy)


def _refresh_session_protocol(data, json_path, target_email, timeout, proxy=None):
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
    json_path = _save_refreshed(refreshed, json_path)
    return {
        "ok": True,
        "mode": "protocol",
        "email": refreshed.get("email", ""),
        "json_path": json_path,
        "refresh_token_status": refreshed["refresh_token_status"],
    }


def _refresh_session_browser(data, json_path, target_email, timeout, headless):
    try:
        from cloakbrowser import launch
    except ImportError:
        return {"ok": False, "mode": "browser", "error": "cloakbrowser_not_installed: pip install cloakbrowser"}

    browser = launch(headless=bool(headless), humanize=True)
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
    )
    _import_cookie_header(ctx, data.get("cookie_header", ""))
    try:
        page = ctx.new_page()
        page.goto(CFG["chatgpt"].get("chat_base_url", "https://chatgpt.com"), wait_until="domcontentloaded", timeout=120000)
        print("[*] CloakBrowser opened. Complete any required login/payment confirmation manually.")
        auth_session = _poll_auth_session(ctx, timeout)
        cookies = ctx.cookies()
    except Exception as e:
        return {"ok": False, "email": target_email, "mode": "browser", "error": str(e)}
    finally:
        browser.close()

    access_token = _session_token(auth_session, "accessToken", "access_token")
    oauth_refresh_token = _session_token(auth_session, "refreshToken", "refresh_token")
    if not access_token:
        return {"ok": False, "email": target_email, "mode": "browser", "error": "auth_session_missing_access_token"}

    refreshed = _merge_refreshed_session(
        data=data,
        target_email=target_email,
        auth_session=auth_session,
        access_token=access_token,
        oauth_refresh_token=oauth_refresh_token,
        cookie_header=_cookie_header(cookies),
    )
    json_path = _save_refreshed(refreshed, json_path)
    return {
        "ok": True,
        "mode": "browser",
        "email": refreshed.get("email", ""),
        "json_path": json_path,
        "refresh_token_status": refreshed["refresh_token_status"],
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
        return _read_json(path), str(path)
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
                impersonate="chrome",
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


def _minimal_chatgpt_cookie_header(cookie_header):
    keep = {
        "__Host-next-auth.csrf-token",
        "__Secure-next-auth.callback-url",
        "__Secure-next-auth.session-token",
    }
    output = []
    for item in str(cookie_header or "").split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name in keep and value:
            output.append(f"{name}={value}")
    return "; ".join(output)


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


def _poll_auth_session(ctx, timeout):
    chat_base = CFG["chatgpt"].get("chat_base_url", "https://chatgpt.com").rstrip("/")
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        try:
            response = ctx.request.get(f"{chat_base}/api/auth/session", timeout=30000)
            last_status = str(response.status)
            if response.status == 200:
                body = response.json()
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


def _import_cookie_header(ctx, cookie_header):
    for item in str(cookie_header or "").split(";"):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        cookie = {
            "name": name,
            "value": value,
            "url": "https://chatgpt.com",
            "path": "/",
            "httpOnly": name.startswith("__Secure-") or name.startswith("__Host-"),
            "secure": True,
            "sameSite": "Lax",
        }
        try:
            ctx.add_cookies([cookie])
        except Exception as e:
            print(f"[*] Skipping stale cookie {name}: {e}")


def _cookie_header(cookies):
    return "; ".join(
        f"{cookie.get('name')}={cookie.get('value')}"
        for cookie in cookies
        if cookie.get("name") and cookie.get("value") and _chatgpt_cookie(cookie)
    )


def _chatgpt_cookie(cookie):
    domain = str(cookie.get("domain") or "")
    return "chatgpt.com" in domain


def _new_session_path(data):
    directory = output_dir(CFG)
    email = (data.get("email") or "unknown").replace("+", "")
    safe_email = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", email)
    return str(directory / f"session_{safe_email}_{int(time.time())}.json")
