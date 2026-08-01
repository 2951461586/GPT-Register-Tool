import json
import time

from .codex_sentinel import load_cached_sentinel, with_sentinel
from .auth_headers import auth_impersonate, openai_auth_headers
from .config import CFG
from .http_client import request_with_retry
from .sentinel_tokens import _extract_sentinel_http
from .auth_flow import _absolute_url, _json_or_raw

def _create_account_sentinel_token(sentinel_data, proxy=None):
    token = str((sentinel_data or {}).get("sentinel_oauth_token") or "").strip()
    if token:
        return token
    # Older browser-based sentinel extraction only captured a username-password
    # token.  HAR evidence for passwordless signup shows create_account now uses
    # oauth_create_account, so try one direct protocol refresh before falling
    # back to the legacy token.
    #
    # Refresh with the same device ID. A valid token from a different Sentinel
    # transaction would invalidate the auth state at create_account.
    did = str((sentinel_data or {}).get("oai_did") or "").strip()
    if not did:
        try:
            did = str(json.loads(str((sentinel_data or {}).get("sentinel_token") or "{}")).get("id") or "").strip()
        except Exception:
            did = ""
    try:
        refreshed = _extract_sentinel_http(proxy=proxy, persist=False, device_id=did or None)
        if refreshed and refreshed.get("sentinel_oauth_token"):
            return refreshed["sentinel_oauth_token"]
    except Exception as exc:
        print(f"  OAuth create sentinel refresh warning: {exc}")
    return str((sentinel_data or {}).get("sentinel_token") or "").strip()


def _follow_continue_url(session, url, base_headers, referer="", label="continue"):
    if not url:
        return None
    full_url = _absolute_url(CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com"), url)
    headers = {**base_headers, "Accept": "text/html,application/xhtml+xml"}
    if referer:
        headers["Referer"] = referer
    r = request_with_retry(session, "get", full_url, label=label,
        headers=headers, impersonate=auth_impersonate())
    print(f"  {label}: {r.status_code} {r.url}")
    return r


def _email_otp_send_url(reg_data, auth_base, resume_email_verification=False):
    continue_url = ""
    if isinstance(reg_data, dict):
        continue_url = str(reg_data.get("continue_url") or "").strip()
    if continue_url:
        return continue_url
    if resume_email_verification:
        return _absolute_url(auth_base, "/api/accounts/email-otp/send")
    return ""


def _create_account_continue_url(create_data):
    if not isinstance(create_data, dict):
        return ""
    continue_url = str(create_data.get("continue_url") or "").strip()
    if continue_url:
        return continue_url
    error = create_data.get("error") if isinstance(create_data.get("error"), dict) else {}
    return str(error.get("redirect_uri") or error.get("redirect_url") or "").strip()


def _is_user_already_exists(create_data):
    if not isinstance(create_data, dict):
        return False
    error = create_data.get("error") if isinstance(create_data.get("error"), dict) else {}
    return str(error.get("code") or "").strip() == "user_already_exists"

def _validate_email_otp(session, auth_base, base_headers, code, sentinel_data=None, use_sentinel=True):
    # Primary endpoint: same as codex_oauth (proven working)
    primary_endpoint = "/api/accounts/email-otp/validate"
    fallback_endpoints = [
        "/api/accounts/email-verification/validate",
        "/api/accounts/email-verification/verify",
        "/api/accounts/verify-email",
    ]
    did = str((base_headers or {}).get("oai-device-id") or (base_headers or {}).get("Oai-Device-Id") or "").strip()
    validate_headers = {
        **(base_headers or {}),
        **openai_auth_headers(
            did,
            referer=f"{auth_base}/email-verification",
            origin=auth_base,
            extra={"content-type": "application/json"},
        ),
    }
    if use_sentinel:
        sentinel = sentinel_data or load_cached_sentinel()
        validate_headers = with_sentinel(validate_headers, sentinel)
    # Try primary endpoint first with {"code": payload (matches codex_oauth)
    url = _absolute_url(auth_base, primary_endpoint)
    r = request_with_retry(session, "post", url, label=f"Email OTP validate {primary_endpoint}",
        json={"code": code}, headers=validate_headers, impersonate=auth_impersonate())
    body = _json_or_raw(r)
    if r.status_code == 200:
        print(f"  Email OTP validate: {primary_endpoint} {r.status_code}")
        return True, body
    last_error = {"endpoint": primary_endpoint, "status": r.status_code, "body": body}
    print(f"  Email OTP validate: {primary_endpoint} {r.status_code} {json.dumps(body, ensure_ascii=False, default=str)[:200]}")
    # If primary returns 404/405, try fallback endpoints
    if r.status_code in (404, 405):
        for endpoint in fallback_endpoints:
            url = _absolute_url(auth_base, endpoint)
            for payload in ({"code": code}, {"otp": code}):
                r = request_with_retry(session, "post", url, label=f"Email OTP validate {endpoint}",
                    json=payload, headers=validate_headers, impersonate=auth_impersonate())
                body = _json_or_raw(r)
                if r.status_code == 200:
                    print(f"  Email OTP validate: {endpoint} {r.status_code}")
                    return True, body
                if r.status_code not in (404, 405):
                    last_error = {"endpoint": endpoint, "status": r.status_code, "body": body}
                    print(f"  Email OTP validate failed: {endpoint} {r.status_code} {json.dumps(body, ensure_ascii=False, default=str)[:200]}")
                    break
                last_error = {"endpoint": endpoint, "status": r.status_code, "body": body}
    return False, last_error


def _is_wrong_email_otp_code(data):
    try:
        error = (data or {}).get("body", {}).get("error", {})
        code = str(error.get("code") or "").strip().lower()
        message = str(error.get("message") or "").strip().lower()
        return code == "wrong_email_otp_code" or "wrong code" in message
    except Exception:
        return False


def _cookie_header(session):
    cookies = getattr(session, "cookies", None)
    if not cookies:
        return ""
    if hasattr(cookies, "get_dict"):
        items = cookies.get_dict().items()
    else:
        items = [(cookie.name, cookie.value) for cookie in cookies]
    return _minimal_chatgpt_cookie_header("; ".join(f"{name}={value}" for name, value in items))


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


def _extract_nested(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current or ""


def _auth_session_access_token(body):
    return (
        body.get("accessToken")
        or body.get("access_token")
        or _extract_nested(body, "session", "access_token")
        or _extract_nested(body, "session", "accessToken")
    )


def _fetch_auth_session(session, chat_base, base_headers, attempts=6, delay=2.0):
    last = {"status_code": 0, "body": {}, "cookie_header": _cookie_header(session)}
    for attempt in range(1, max(1, int(attempts or 1)) + 1):
        r = request_with_retry(session, "get", f"{chat_base}/api/auth/session", label="Auth session",
            headers={**base_headers, "Accept": "application/json", "Origin": chat_base, "Referer": f"{chat_base}/"},
            impersonate=auth_impersonate())
        body = _json_or_raw(r, limit=1000)
        last = {
            "status_code": r.status_code,
            "body": body,
            "cookie_header": _cookie_header(session),
        }
        print(f"  Auth session: {r.status_code}" + (f" attempt={attempt}" if attempt > 1 else ""))
        if r.status_code == 200 and _auth_session_access_token(body):
            return last
        if attempt < attempts:
            time.sleep(delay)
    return last
# ==========================================
# Core Email Registration Flow
# ==========================================
