import json
import time

from .codex_sentinel import load_cached_sentinel, with_sentinel
from .auth_headers import auth_impersonate, openai_auth_headers
from .config import CFG
from .http_client import request_with_retry
from .http_utils import _absolute_url, _cookie_header, _json_or_raw, _minimal_chatgpt_cookie_header

def _create_account_sentinel_token(sentinel_data, proxy=None):
    token = str((sentinel_data or {}).get("sentinel_oauth_token") or "").strip()
    if token:
        return token
    raise RuntimeError("sentinel_extract_failed: oauth_create_account SDK token is required")




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
