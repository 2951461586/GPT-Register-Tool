import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, quote, urlencode, urlparse

from curl_cffi import requests as curl_requests

from .codex_sentinel import import_cookie_header, load_cached_sentinel, with_sentinel
from .config import CFG
from .http_client import request_with_retry
from .sentinel_tokens import (
    _cookie_jar_header,
    _extract_sentinel,
    _extract_sentinel_http,
    _import_sentinel_cookies,
    _sentinel_device_id,
    _set_oai_did_cookie,
)
from .auth_flow import (
    _absolute_url,
    _continue_signup_username,
    _invalid_state_auth_response,
    _is_chatgpt_auth_login_landing,
    _is_email_verification_step,
    _is_existing_login_redirect,
    _is_signup_password_step,
    _json_or_raw,
    _openai_signin_url,
    _passwordless_signin_attempts,
    _prepare_signup_auth_state,
    _prime_email_verification_page,
    _response_next_url,
    _signup_signin_attempts,
    _with_query_param,
)
from .account_creation import (
    _auth_session_access_token,
    _cookie_header,
    _create_account_continue_url,
    _create_account_sentinel_token,
    _email_otp_send_url,
    _fetch_auth_session,
    _follow_continue_url,
    _generate_payment_link,
    _generate_paypal_link,
    _is_user_already_exists,
    _is_wrong_email_otp_code,
    _minimal_chatgpt_cookie_header,
    _validate_email_otp,
)
from .auth_state import fetch_client_auth_session_dump as _fetch_client_auth_session_dump
from . import account_creation as _account_creation
from . import otp_strategy as _otp_strategy
from .mailbox import _ensure_mailbox_account, _poll_email_otp, _snapshot_mailbox_message
from .paths import runtime_file
from .utils import _generate_password, _print_timings, _random_birthdate, _random_name, _tick, _timing_summary, _tock, _tl

REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD = "verification code"
LOGIN_EMAIL_OTP_SUBJECT_KEYWORD = "login code"
REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS = f"{REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD}|{LOGIN_EMAIL_OTP_SUBJECT_KEYWORD}"

# ==========================================
# Sentinel token (cached, browser only when needed)
# ==========================================
SENTINEL_CACHE_FILE = runtime_file(CFG, "sentinel_cache.json")

def _mailbox_snapshot(mailbox):
    if not mailbox:
        return {}
    return {
        "email": getattr(mailbox, "email", ""),
        "password": getattr(mailbox, "password", ""),
        "refresh_token": getattr(mailbox, "refresh_token", ""),
        "access_token": getattr(mailbox, "access_token", ""),
        "source": getattr(mailbox, "source", ""),
        "provider": getattr(mailbox, "provider", ""),
        "order_no": getattr(mailbox, "order_no", ""),
        "token": getattr(mailbox, "token", ""),
        "purchase_id": getattr(mailbox, "purchase_id", ""),
        "project_name": getattr(mailbox, "project_name", ""),
        "price": getattr(mailbox, "price", ""),
        "purchase_total_cost": getattr(mailbox, "purchase_total_cost", ""),
        "balance_after": getattr(mailbox, "balance_after", ""),
    }


def _failure_result(error, email="", mailbox=None, password=""):
    result = {"success": False, "error": error, "timing": _timing_summary()}
    if email:
        result["email"] = email
    if password:
        result["password"] = password
    mailbox_data = _mailbox_snapshot(mailbox)
    if mailbox_data:
        result["mailbox"] = mailbox_data
    return result


def _stored_registration_password(email):
    try:
        from .storage import get_account_record
        row = get_account_record(email)
    except Exception:
        return ""
    if not row:
        return ""
    error = str(row.get("error") or "").lower()
    if "password_verify_failed" in error:
        return ""
    password = str(row.get("password") or "").strip()
    if password:
        return password
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except Exception:
        raw = {}
    return str(raw.get("password") or "").strip()



def _safe_tock():
    timings = _tl()
    if timings and timings[-1][1] > 1_000_000:
        _tock()

# Sentinel token extraction moved to sms_tool.sentinel_tokens.

def _resolve_proxy_scheme(proxy):
    """Detect working proxy scheme. Many providers labeled socks5h:// are actually HTTP CONNECT proxies."""
    if not proxy or not proxy.startswith(("socks5h://", "socks5://")):
        return proxy
    # Quick connectivity test: try socks5h first, fall back to http
    http_proxy = proxy.replace("socks5h://", "http://").replace("socks5://", "http://")
    for scheme, test_proxy in [(proxy, proxy), ("http://", http_proxy)]:
        try:
            s = curl_requests.Session()
            s.proxies = {"http": test_proxy, "https": test_proxy}
            s.get("http://ip-api.com/json?fields=query", timeout=15, impersonate="chrome")
            if scheme != proxy:
                print(f"[*] Proxy scheme auto-corrected: socks5h:// → http://")
            return test_proxy
        except Exception:
            continue
    # Both failed, return original and let caller handle
    print("[!] Warning: proxy connectivity test failed with both socks5h:// and http:// schemes")
    return proxy


# Sentinel orchestration/browser fallback moved to sms_tool.sentinel_tokens.


def _send_registration_email_otp(session, auth_base, base_headers, current_url="", mode="send"):
    # Keep the historical registration-module seam stable for tests and callers
    # that monkeypatch CFG/request_with_retry here, while the implementation now
    # lives in otp_strategy.py.
    _otp_strategy.CFG = CFG
    _otp_strategy.request_with_retry = request_with_retry
    return _otp_strategy.send_registration_email_otp(
        session,
        auth_base,
        base_headers,
        current_url=current_url,
        mode=mode,
    )


# Account creation/session helpers moved to sms_tool.account_creation.


def _response_next_url(response, base_url):
    body = _json_or_raw(response, limit=1000)
    if isinstance(body, dict):
        value = body.get("continue_url") or body.get("url")
        if value:
            return _absolute_url(base_url, value)
    location = getattr(response, "headers", {}).get("location") or getattr(response, "headers", {}).get("Location")
    if location:
        return _absolute_url(base_url, location)
    return str(getattr(response, "url", "") or "")


def _send_existing_login_otp(session, auth_base, base_headers, current_url, did, sentinel_token="", sentinel_so_token=""):
    headers = {
        **base_headers,
        "Origin": auth_base,
        "Referer": current_url or f"{auth_base}/email-verification",
        "Content-Type": "application/json",
        "oai-device-id": did,
    }
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    if sentinel_so_token:
        headers["openai-sentinel-so-token"] = sentinel_so_token
    for endpoint in (
        "/api/accounts/passwordless/send-otp",
        "/api/accounts/email-otp/send",
    ):
        response = request_with_retry(
            session,
            "post",
            _absolute_url(auth_base, endpoint),
            label=f"Existing account OTP send {endpoint}",
            json={},
            headers=headers,
            impersonate="chrome",
        )
        print(f"  Existing account OTP send: {endpoint} {response.status_code}")
        if response.status_code in (200, 202, 204, 409):
            return True, response
        if response.status_code not in (400, 404, 405):
            return False, response
    return False, None


def _login_existing_account_with_email_otp(
    session,
    username,
    mailbox,
    did,
    session_logging_id,
    auth_base,
    chat_base,
    base_headers,
    csrf_token,
    proxy=None,
    sentinel_token="",
    sentinel_so_token="",
):
    print("  Existing account login: starting email OTP flow")
    signin_url = (
        f"{chat_base}/api/auth/signin/openai"
        f"?prompt=login&ext-oai-did={did}"
        f"&auth_session_logging_id={session_logging_id}"
        f"&screen_hint=login"
        f"&login_hint={quote(username, safe='')}"
    )
    signin_payload = {
        "csrfToken": csrf_token,
        "callbackUrl": f"{chat_base}/",
        "json": "true",
    }
    signin_resp = request_with_retry(
        session,
        "post",
        signin_url,
        label="Existing account signin",
        data=urlencode(signin_payload),
        headers={**base_headers, "Content-Type": "application/x-www-form-urlencoded", "Origin": chat_base, "Referer": f"{chat_base}/"},
        impersonate="chrome",
    )
    signin_body = _json_or_raw(signin_resp, limit=1000)
    auth_session_url = signin_body.get("url") or signin_resp.headers.get("location") or signin_resp.url
    auth_session_url = _with_query_param(auth_session_url, "device_id", did)
    authorize_resp = request_with_retry(
        session,
        "get",
        auth_session_url,
        label="Existing account authorize",
        headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Origin": auth_base, "Referer": f"{chat_base}/"},
        impersonate="chrome",
    )
    current_url = str(authorize_resp.url or "")
    print(f"  Existing account authorize: {authorize_resp.status_code} {current_url}")

    current_lower = current_url.lower()
    if "chatgpt.com" in current_lower and ("/api/auth/callback/openai" in current_lower or current_lower.rstrip("/") == chat_base.lower().rstrip("/")):
        return {"ok": True}
    if "email-verification" not in current_lower and "email-otp" not in current_lower:
        continue_resp = request_with_retry(
            session,
            "post",
            f"{auth_base}/api/accounts/authorize/continue",
            label="Existing account continue",
            json={"username": {"value": username, "kind": "email"}},
            headers={
                **base_headers,
                "Origin": auth_base,
                "Referer": current_url or f"{auth_base}/log-in",
                "Content-Type": "application/json",
                "oai-device-id": did,
                "openai-sentinel-token": sentinel_token,
                "openai-sentinel-so-token": sentinel_so_token,
            },
            impersonate="chrome",
        )
        print(f"  Existing account continue: {continue_resp.status_code}")
        if continue_resp.status_code != 200:
            return {"ok": False, "error": f"existing_login_continue_failed:{continue_resp.status_code}"}

        current_url = _response_next_url(continue_resp, auth_base)
        if current_url:
            follow_resp = _follow_continue_url(
                session,
                current_url,
                base_headers,
                referer=current_url,
                label="Existing account continue follow",
            )
            current_url = str(getattr(follow_resp, "url", "") or current_url)

    otp_send_started = int(time.time())
    ok, otp_send_response = _send_existing_login_otp(
        session,
        auth_base,
        base_headers,
        current_url,
        did,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
    )
    if not ok:
        status = getattr(otp_send_response, "status_code", 0)
        return {"ok": False, "error": f"existing_login_otp_send_failed:{status}"}

    email_cfg = CFG.get("email_registration", {})
    code = _poll_email_otp(
        mailbox,
        subject_keyword=LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
        timeout=int(email_cfg.get("otp_timeout", 300)),
        issued_after_unix=otp_send_started,
        proxy=proxy,
    )
    if not code:
        return {"ok": False, "error": "existing_login_otp_poll_timeout"}

    otp_ok, otp_data = _validate_email_otp(session, auth_base, base_headers, code,
        sentinel_data={"sentinel_token": sentinel_token, "sentinel_so_token": sentinel_so_token})
    if not otp_ok:
        return {"ok": False, "error": f"existing_login_otp_validate:{json.dumps(otp_data, ensure_ascii=False)[:200]}"}
    try:
        _follow_continue_url(
            session,
            otp_data.get("continue_url", ""),
            base_headers,
            referer=f"{auth_base}/email-verification",
            label="Existing account OTP continue",
        )
    except Exception as e:
        print(f"  Existing account OTP continue transport warning: {e}")
    return {"ok": True}


# OTP validation/session helpers moved to sms_tool.account_creation.
def _validate_email_otp(*args, **kwargs):
    # Keep the historical registration-module seam stable for tests and callers
    # that monkeypatch CFG/request_with_retry here.
    _account_creation.CFG = CFG
    _account_creation.request_with_retry = request_with_retry
    return _account_creation._validate_email_otp(*args, **kwargs)


def _extract_query_param(url, key):
    try:
        values = parse_qs(urlparse(url).query).get(key)
    except Exception:
        values = None
    return values[0] if values else ""


def _with_query_param(url, key, value):
    if not value or f"{key}=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{key}={quote(str(value), safe='')}"


def _openai_signin_url(chat_base, did, session_logging_id, login_hint, *, screen_hint="", prompt=""):
    params = {
        "ext-oai-did": did,
        "auth_session_logging_id": session_logging_id,
        "login_hint": login_hint,
    }
    if screen_hint:
        params["screen_hint"] = screen_hint
    if prompt:
        params["prompt"] = prompt
    return f"{chat_base}/api/auth/signin/openai?{urlencode(params)}"


def _signup_signin_attempts():
    """Ordered NextAuth signup entry variants.

    The legacy email-registration URL used both ``prompt=login`` and
    ``screen_hint=signup``. Current auth behavior can treat that as a login
    flow and bounce back to ``chatgpt.com/auth/login`` without creating an
    auth.openai.com signup transaction; any later ``authorize/continue`` then
    fails with ``invalid_state``. Prefer a pure signup hint first and keep the
    older shapes only as fallbacks for stale deployments.
    """
    return (
        {"name": "signup_screen_hint", "screen_hint": "signup", "prompt": ""},
        {"name": "signup_prompt_signup", "screen_hint": "signup", "prompt": "signup"},
        {"name": "signup_legacy_prompt_login", "screen_hint": "signup", "prompt": "login"},
    )


def _passwordless_signin_attempts():
    """Ordered NextAuth entry variants observed in the browser HAR.

    The July 2026 browser flow reports original_screen_hint=login_or_signup and
    email_verification_mode=passwordless_signup after username continue.  Keep a
    signup fallback for regions or cached deployments that still route directly
    to create-account.
    """
    return (
        {"name": "login_or_signup", "screen_hint": "login_or_signup", "prompt": ""},
        {"name": "login_or_signup_prompt_signup", "screen_hint": "login_or_signup", "prompt": "signup"},
        {"name": "signup_screen_hint", "screen_hint": "signup", "prompt": ""},
    )


def _invalid_state_auth_response(data):
    if not isinstance(data, dict):
        return False
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    code = str(error.get("code") or "").strip().lower()
    message = str(error.get("message") or "").strip().lower()
    return code == "invalid_state" or "session is no longer valid" in message


def _normalize_registration_mode(value=None):
    raw = str(value or "").strip().lower().replace("-", "_")
    if not raw:
        cfg = CFG.get("email_registration") if isinstance(CFG.get("email_registration"), dict) else {}
        raw = str(cfg.get("registration_mode") or cfg.get("signup_mode") or "passwordless").strip().lower().replace("-", "_")
    if raw in {"password", "password_signup", "user_register", "legacy"}:
        return "password"
    if raw in {"passwordless", "passwordless_signup", "login_or_signup", "har"}:
        return "passwordless"
    return "passwordless"


def _registration_otp_issued_after(mailbox, issued_after_unix):
    issued_after_unix = int(issued_after_unix or 0)
    if getattr(mailbox, "provider", "") != "cfworker":
        return issued_after_unix
    try:
        cfg = CFG.get("email_registration", {}) if isinstance(CFG.get("email_registration"), dict) else {}
        grace = int(cfg.get("cfworker_otp_issued_after_grace_seconds", 10))
    except Exception:
        grace = 10
    return max(0, issued_after_unix - max(0, grace))


# Payment generation helpers moved to sms_tool.account_creation.
def run_email(
    proxy=None,
    password=None,
    sentinel_data=None,
    mailbox=None,
    paypal_link=True,
    phone_pool=None,
    codex_oauth=True,
    payment_method="paypal",
    paypal_generation_type=None,
    registration_mode=None,
):
    """Register a ChatGPT account via mailbox OTP, then create a PayPal payment link."""
    _tl().clear()

    proxy = _resolve_proxy_scheme(proxy)

    mailbox = _ensure_mailbox_account(mailbox)
    if not mailbox or not mailbox.email:
        return _failure_result("mailbox_required", mailbox=mailbox)

    auth_base = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com")
    chat_base = CFG["chatgpt"].get("chat_base_url", "https://chatgpt.com")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36"

    print(f"[*] ChatGPT Email Registration Started")

    # Step 0: Get sentinel tokens
    if sentinel_data:
        print("[*] Using provided sentinel tokens")
    else:
        _tick("0-Extract sentinel token")
        sentinel_data = _extract_sentinel(proxy=proxy, force_fresh=True)
        _tock()
    if not sentinel_data or not sentinel_data.get("sentinel_token"):
        return _failure_result("sentinel_extract_failed", email=getattr(mailbox, "email", ""), mailbox=mailbox)

    # Step 1: Generate credentials
    explicit_password = bool(str(password or "").strip())
    stored_password = "" if explicit_password else _stored_registration_password(mailbox.email)
    password_from_storage = bool(stored_password)
    password = password or stored_password or _generate_password()
    password_unknown = False
    first, last = _random_name()
    full_name = f"{first} {last}"
    birthdate = _random_birthdate()
    username = mailbox.email
    registration_mode = _normalize_registration_mode(registration_mode)
    registration_mode_used = registration_mode

    # Keep device_id aligned with the fresh sentinel/auth cookies.  A mismatch
    # makes auth.openai.com drop the signup state and user/register returns
    # invalid_state.
    did = _sentinel_device_id(sentinel_data) or str(uuid.uuid4())
    session_logging_id = str(uuid.uuid4()).replace("-", "")

    # Use original sentinel tokens — do NOT patch the embedded id, as it breaks the HMAC signature
    _sentinel_token = sentinel_data["sentinel_token"]
    _sentinel_so_token = sentinel_data["sentinel_so_token"]
    print(f"[*] Username: {username}  Password: {password}  Name: {full_name}  Birth: {birthdate}")

    # Init curl_cffi session
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    if registration_mode == "passwordless":
        # The HAR passwordless flow starts from ChatGPT's NextAuth entry and
        # does not pre-load auth.openai.com/create-account cookies.  Importing
        # those legacy cookies can make /api/accounts/authorize choose a stale
        # login transaction and redirect back to chatgpt.com/auth/login.
        _set_oai_did_cookie(session, did)
    else:
        _import_sentinel_cookies(session, sentinel_data, did)
    base_headers = {"User-Agent": ua, "Accept": "application/json"}
    auth_flow_started = int(time.time())

    try:
        # Auth flow: prime + signin + authorize
        _tick("2-Auth flow")
        auth_flow_started = int(time.time())
        if registration_mode == "passwordless":
            request_with_retry(session, "get", f"{chat_base}/", label="ChatGPT prime",
                headers={**base_headers, "Accept": "text/html,application/xhtml+xml"}, impersonate="chrome")
        else:
            request_with_retry(session, "get", f"{auth_base}/create-account", label="Auth prime",
                headers={**base_headers, "Accept": "text/html,application/xhtml+xml"}, impersonate="chrome")

        csrf_resp = request_with_retry(session, "get", f"{chat_base}/api/auth/csrf", label="Auth csrf",
            headers={**base_headers, "Accept": "application/json", "Referer": f"{chat_base}/"},
            impersonate="chrome")
        csrf_token = (_json_or_raw(csrf_resp).get("csrfToken") or "").strip()

        signup_state = _prepare_signup_auth_state(
            session,
            username,
            did,
            session_logging_id,
            auth_base,
            chat_base,
            base_headers,
            csrf_token,
            sentinel_token=_sentinel_token,
            sentinel_so_token=_sentinel_so_token,
            attempts=_passwordless_signin_attempts() if registration_mode == "passwordless" else _signup_signin_attempts(),
        )
        _tock()
        auth_dump_after_signup = _fetch_client_auth_session_dump(session, auth_base, base_headers, "after_signup_state")
        if signup_state.get("existing_login_redirect"):
            return _failure_result("email_already_registered_or_login_redirect", email=username, mailbox=mailbox, password=password)
        if not signup_state.get("ok"):
            return _failure_result(f"signup_auth_state: {json.dumps(signup_state, ensure_ascii=False)[:300]}", email=username, mailbox=mailbox, password=password)
        if _is_chatgpt_auth_login_landing(signup_state.get("url", "")):
            return _failure_result("signup_auth_state: redirected_to_chatgpt_login", email=username, mailbox=mailbox, password=password)

        if registration_mode == "passwordless":
            reg_data = {
                "mode": "passwordless_signup",
                "auth_state": {
                    "attempt": signup_state.get("attempt", ""),
                    "url": signup_state.get("url", ""),
                    "status": signup_state.get("status", 0),
                },
            }
            r = None
            password_unknown = True
            print("  Registration mode: passwordless_signup (HAR login_or_signup)")
        else:
            # Step 4: Register with username + password
            _tick("3-User register (email+password)")
            r = request_with_retry(session, "post", f"{auth_base}/api/accounts/user/register", label="User register",
                json={"password": password, "username": username},
                headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/create-account/password",
                        "openai-sentinel-token": _sentinel_token, "oai-device-id": did},
                impersonate="chrome")
            _tock()
    except Exception as e:
        _safe_tock()
        print(f"  Transport error: {e}")
        return _failure_result(f"transport_error: {e}", email=username, mailbox=mailbox, password=password)

    if registration_mode != "passwordless":
        reg_data = {}
        try: reg_data = r.json()
        except: reg_data = {"_raw": r.text[:300]}
        print(f"  Status: {r.status_code}")
        print(f"  Response: {json.dumps(reg_data, ensure_ascii=False)[:300]}")

    resume_email_verification = False
    if registration_mode != "passwordless" and r.status_code != 200:
        err_code = reg_data.get("error", {}).get("code", "")
        err_msg = reg_data.get("error", {}).get("message", str(reg_data))
        state_url = str((signup_state or {}).get("url") or "")
        if err_code == "invalid_auth_step" and "email-verification" in state_url:
            print(f"  Account already in email-verification flow, resuming OTP step...")
            resume_email_verification = True
        else:
            return _failure_result(f"user_register: {err_msg}", email=username, mailbox=mailbox, password=password)

    _snapshot_mailbox_message(mailbox, proxy=proxy)

    # Step 4: Trigger email OTP send
    _tick("4-Trigger email OTP")
    continue_url = _email_otp_send_url(reg_data, auth_base, resume_email_verification)
    otp_send_started = int(time.time())
    try:
        if registration_mode == "passwordless":
            _prime_email_verification_page(
                session,
                auth_base,
                base_headers,
                (signup_state or {}).get("url", ""),
            )
            otp_send_response = _send_registration_email_otp(
                session,
                auth_base,
                base_headers,
                current_url=(signup_state or {}).get("url", ""),
                mode="passwordless",
            )
        else:
            otp_send_response = _follow_continue_url(session, continue_url, base_headers, referer=f"{auth_base}/create-account/password", label="Email OTP send")
        _tock()
    except Exception as e:
        _safe_tock()
        print(f"  Transport error: {e}")
        return _failure_result(f"email_otp_send_transport: {e}", email=username, mailbox=mailbox, password=password)
    _fetch_client_auth_session_dump(session, auth_base, base_headers, "after_otp_send")
    if otp_send_response is None:
        return _failure_result("email_otp_send_missing_continue_url", email=username, mailbox=mailbox, password=password)
    if getattr(otp_send_response, "status_code", 0) not in (200, 202, 204):
        return _failure_result(f"email_otp_send_failed:{otp_send_response.status_code}", email=username, mailbox=mailbox, password=password)
    otp_issued_after = otp_send_started
    try:
        if registration_mode == "passwordless" and (_json_or_raw(otp_send_response).get("assumed_pre_sent")):
            otp_issued_after = max(0, auth_flow_started - 5)
    except Exception:
        pass
    otp_issued_after = _registration_otp_issued_after(mailbox, otp_issued_after)

    # Step 5: Get email OTP
    _tick("5-Get email OTP")
    email_cfg = CFG.get("email_registration", {})
    code = _poll_email_otp(
        mailbox,
        subject_keyword=REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS,
        timeout=int(email_cfg.get("otp_timeout", 300)),
        issued_after_unix=otp_issued_after,
        proxy=proxy,
    )
    _tock()
    if not code:
        return _failure_result("email_otp_poll_timeout", email=username, mailbox=mailbox, password=password)

    # Step 6: Validate email OTP
    _tick("6-Validate email OTP")
    try:
        otp_ok, otp_data = _validate_email_otp(
            session,
            auth_base,
            base_headers,
            code,
            sentinel_data=sentinel_data,
            use_sentinel=(registration_mode != "passwordless"),
        )
        if not otp_ok and _is_wrong_email_otp_code(otp_data):
            print("  Email OTP was rejected; retrying latest mailbox code once...")
            retry_code = _poll_email_otp(
                mailbox,
                subject_keyword=REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS,
                timeout=min(60, int(email_cfg.get("otp_timeout", 300))),
                issued_after_unix=otp_issued_after,
                proxy=proxy,
            )
            if retry_code and retry_code != code:
                code = retry_code
                otp_ok, otp_data = _validate_email_otp(
                    session,
                    auth_base,
                    base_headers,
                    code,
                    sentinel_data=sentinel_data,
                    use_sentinel=(registration_mode != "passwordless"),
                )
        _tock()
    except Exception as e:
        _safe_tock()
        print(f"  Transport error: {e}")
        return _failure_result(f"email_otp_validate_transport: {e}", email=username, mailbox=mailbox, password=password)
    if not otp_ok:
        _fetch_client_auth_session_dump(session, auth_base, base_headers, "after_otp_validate_failed")
        return _failure_result(f"email_otp_validate: {json.dumps(otp_data, ensure_ascii=False)[:300]}", email=username, mailbox=mailbox, password=password)
    try:
        _follow_continue_url(session, otp_data.get("continue_url", ""), base_headers, referer=f"{auth_base}/verify-email", label="Email OTP continue")
    except Exception as e:
        print(f"  Email OTP continue transport warning: {e}")

    # Step 7: Create account
    _tick("7-Create account")
    try:
        create_sentinel_token = _create_account_sentinel_token(sentinel_data, proxy=proxy)
        r = request_with_retry(session, "post", f"{auth_base}/api/accounts/create_account", label="Create account",
            json={"name": full_name, "birthdate": birthdate},
            headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/about-you",
                    "openai-sentinel-token": create_sentinel_token,
                    "openai-sentinel-so-token": _sentinel_so_token},
            impersonate="chrome")
        _tock()
    except Exception as e:
        _safe_tock()
        print(f"  Transport error: {e}")
        return _failure_result(f"create_account_transport: {e}", email=username, mailbox=mailbox, password=password)

    create_data = {}
    try: create_data = r.json()
    except: create_data = {"_raw": r.text[:300]}
    print(f"  Status: {r.status_code}")
    print(f"  Response: {json.dumps(create_data, ensure_ascii=False)[:300]}")
    create_ok = r.status_code == 200
    existing_account = _is_user_already_exists(create_data)
    password_unknown = bool(resume_email_verification and not (explicit_password or password_from_storage))
    if not create_ok and existing_account:
        print("  Account already exists, password may differ from generated one; clearing stored password.")
        create_ok = True
        password_unknown = True
    try:
        _follow_continue_url(session, _create_account_continue_url(create_data), base_headers, referer=f"{auth_base}/about-you", label="Create account continue")
    except Exception as e:
        print(f"  Create account continue transport warning: {e}")

    # Step 8: Fetch ChatGPT auth session access token
    _tick("8-Fetch auth session")
    try:
        auth_session = _fetch_auth_session(session, chat_base, base_headers)
        _tock()
    except Exception as e:
        _safe_tock()
        print(f"  Transport error: {e}")
        return _failure_result(f"auth_session_transport: {e}", email=username, mailbox=mailbox, password=password)
    auth_body = auth_session.get("body") or {}
    access_token = _auth_session_access_token(auth_body)
    if existing_account and not access_token:
        print("  Existing account has no ChatGPT session yet; retrying with passwordless email login...")
        try:
            existing_login = _login_existing_account_with_email_otp(
                session=session,
                username=username,
                mailbox=mailbox,
                did=did,
                session_logging_id=session_logging_id,
                auth_base=auth_base,
                chat_base=chat_base,
                base_headers=base_headers,
                csrf_token=csrf_token,
                proxy=proxy,
                sentinel_token=_sentinel_token,
                sentinel_so_token=_sentinel_so_token,
            )
        except Exception as e:
            existing_login = {"ok": False, "error": f"existing_login_transport:{e}"}
        if existing_login.get("ok"):
            _tick("8b-Fetch existing account auth session")
            try:
                auth_session = _fetch_auth_session(session, chat_base, base_headers)
                _tock()
            except Exception as e:
                _safe_tock()
                print(f"  Transport error: {e}")
                return _failure_result(f"existing_auth_session_transport: {e}", email=username, mailbox=mailbox, password=password)
            auth_body = auth_session.get("body") or {}
            access_token = _auth_session_access_token(auth_body)
        else:
            print(f"  Existing account login failed: {existing_login.get('error') or 'unknown'}")

    # Step 8c: Codex OAuth PKCE. /api/auth/session can be AT-only; UI one-click
    # registration intentionally skips this and leaves RT acquisition to "one-click SMS".
    oauth_result = {}
    oauth_tokens = {}
    phone_result = {}
    oauth_refresh_token = ""
    id_token = ""
    if create_ok and codex_oauth:
        _tick("8c-Codex OAuth refresh token")
        try:
            from .codex_oauth import collect_codex_oauth_tokens
            oauth_seed = {
                "email": username,
                "password": "" if password_unknown else password,
                "device_id": did,
                "cookie_header": _cookie_header(session),
                "auth_session": auth_body,
                "mailbox": {
                    "email": mailbox.email,
                    "password": mailbox.password,
                    "refresh_token": mailbox.refresh_token,
                    "access_token": mailbox.access_token,
                    "source": mailbox.source,
                    "provider": getattr(mailbox, "provider", ""),
                    "token": getattr(mailbox, "token", ""),
                } if mailbox else {},
            }
            oauth_result = collect_codex_oauth_tokens(
                data=oauth_seed,
                session=session,
                proxy=proxy,
                timeout=int((CFG.get("codex_oauth") or {}).get("registration_timeout", 180)),
                force_email_otp_login=bool(resume_email_verification or password_unknown),
                phone_pool=phone_pool,
            )
            _tock()
        except Exception as e:
            _safe_tock()
            oauth_result = {"ok": False, "error": f"codex_oauth_transport:{e}"}

        if oauth_result.get("ok"):
            oauth_tokens = oauth_result.get("tokens") or {}
            access_token = oauth_tokens.get("access_token") or access_token
            id_token = oauth_tokens.get("id_token", "")
            oauth_refresh_token = oauth_tokens.get("refresh_token", "")
            phone_result = oauth_result.get("phone_attempt") or {}
            if phone_result.get("ok"):
                print(f"  Phone verified: {phone_result.get('phone', '')} "
                      f"(reuse {phone_result.get('reuse_count', 0)}/{phone_result.get('max_reuse_count', 0)})")
            if oauth_refresh_token:
                print(f"  OAuth refresh token captured: {oauth_refresh_token[:20]}...")
            else:
                print("  Codex OAuth exchange returned no refresh token")
        else:
            phone_result = oauth_result.get("phone_attempt") or {}
            print(f"  Codex OAuth refresh token failed: {oauth_result.get('error', 'unknown')}")
    elif create_ok:
        print("  Codex OAuth refresh token skipped (AT-only registration mode)")

    require_refresh_token = _registration_requires_refresh_token() if codex_oauth else False
    require_phone_verification = _registration_requires_phone_verification(phone_pool) if codex_oauth else False
    phone_verified = bool(phone_result.get("ok"))
    success = (
        create_ok
        and bool(access_token)
        and (not require_refresh_token or bool(oauth_refresh_token))
        and (not require_phone_verification or phone_verified)
    )
    error = ""
    if not create_ok:
        create_error = create_data.get("error") if isinstance(create_data.get("error"), dict) else {}
        create_code = str(create_error.get("code") or "").strip()
        create_message = str(create_error.get("message") or "").strip()
        error = "create_account_failed"
        if create_code:
            error += f":{create_code}"
        if create_message:
            error += f": {create_message}"
    elif not access_token:
        error = "missing_auth_session_access_token"
    elif create_ok and require_refresh_token and not oauth_refresh_token:
        error = (
            (phone_result or {}).get("error")
            or oauth_result.get("error")
            or "missing_oauth_refresh_token"
        )
    elif create_ok and require_phone_verification and not phone_verified:
        error = (
            (phone_result or {}).get("error")
            or oauth_result.get("error")
            or "phone_verification_required"
        )

    paypal = {}
    if success and access_token and paypal_link:
        payment_method = str(payment_method or "paypal").strip().lower()
        if payment_method in {"upi", "upiqr", "upi_qr", "upi-qr"}:
            payment_method = "upi"
            payment_label = "UPI"
        elif payment_method in {"gopay", "go-pay", "go_pay"}:
            payment_method = "gopay"
            payment_label = "GoPay"
        else:
            payment_method = "paypal"
            payment_label = "PayPal"
        _tick(f"9-Generate {payment_label} link")
        paypal = _generate_payment_link(
            access_token,
            proxy=proxy,
            payment_method=payment_method,
            paypal_generation_type=paypal_generation_type,
        )
        paypal.setdefault("payment_method", payment_method)
        paypal.setdefault("method", payment_method)
        print(f"  {payment_label} link: {'ok' if paypal.get('ok') else paypal.get('error', 'failed')}")
        _tock()

    result = {
        "success": success,
        "error": error,
        "email": username,
        "phone": phone_result.get("phone", "") if phone_result.get("ok") else "",
        "password": "" if password_unknown else password,
        "name": full_name,
        "birthdate": birthdate,
        "response": {
            "register": reg_data,
            "email_otp": otp_data,
            "create_account": create_data,
            "auth_session": auth_body,
            "phone_verification": phone_result,
            "codex_oauth": _oauth_result_summary(oauth_result),
        },
        "auth_session": auth_body,
        "access_token": access_token or "",
        "id_token": id_token,
        "oauth_refresh_token": oauth_refresh_token,
        "refresh_token_status": "oauth_present" if oauth_refresh_token else "no_rt",
        "cookie_header": auth_session.get("cookie_header", ""),
        "paypal": paypal,
        "payment_method": (paypal.get("payment_method") or payment_method or "paypal") if paypal else (payment_method or "paypal"),
        "registration_mode": registration_mode_used,
        "device_id": did,
        "timing": _timing_summary(),
    }
    if mailbox:
        result["mailbox"] = {
            "email": mailbox.email,
            "password": mailbox.password,
            "refresh_token": mailbox.refresh_token,
            "access_token": mailbox.access_token,
            "source": mailbox.source,
            "provider": getattr(mailbox, "provider", ""),
            "order_no": getattr(mailbox, "order_no", ""),
            "token": getattr(mailbox, "token", ""),
            "purchase_id": getattr(mailbox, "purchase_id", ""),
            "project_name": getattr(mailbox, "project_name", ""),
            "price": getattr(mailbox, "price", ""),
            "purchase_total_cost": getattr(mailbox, "purchase_total_cost", ""),
            "balance_after": getattr(mailbox, "balance_after", ""),
        }
    _print_timings()
    return result


def run_phone(*args, **kwargs):
    """Compatibility wrapper; SMS/phone registration has been removed from the active flow."""
    return run_email(
        proxy=kwargs.get("proxy"),
        password=kwargs.get("password"),
        sentinel_data=kwargs.get("sentinel_data"),
        mailbox=kwargs.get("mailbox"),
        paypal_link=kwargs.get("paypal_link", True),
        phone_pool=kwargs.get("phone_pool"),
        codex_oauth=kwargs.get("codex_oauth", True),
        payment_method=kwargs.get("payment_method", "paypal"),
        paypal_generation_type=kwargs.get("paypal_generation_type"),
        registration_mode=kwargs.get("registration_mode"),
    )


def run_phone_register(
    proxy=None,
    password=None,
    sentinel_data=None,
    paypal_link=True,
    codex_oauth=True,
    payment_method="paypal",
    paypal_generation_type=None,
    smsbower_country=None,
    smsbower_api_key=None,
    bind_email=None,
):
    """Register a ChatGPT account via phone number (SMS OTP), then optionally bind email."""
    _tl().clear()

    auth_base = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com")
    chat_base = CFG["chatgpt"].get("chat_base_url", "https://chatgpt.com")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36"

    # Load SMSBower config before buying a number so the proxy can be matched
    # to the phone country and verified first.
    phone_reuse_cfg = CFG.get("phone_reuse") if isinstance(CFG.get("phone_reuse"), dict) else {}
    smsbower_cfg = phone_reuse_cfg.get("smsbower") if isinstance(phone_reuse_cfg.get("smsbower"), dict) else {}
    country = smsbower_country or smsbower_cfg.get("country", "38")
    api_key = smsbower_api_key or smsbower_cfg.get("api_key", "")

    try:
        from .phone_proxy import select_phone_proxy
        proxy_result = select_phone_proxy(proxy, country=country, provider="smsbower", country_cfg=smsbower_cfg)
    except Exception as exc:
        proxy_result = {"ok": False, "error": f"phone_proxy_select_failed:{exc}"}
    if not proxy_result.get("ok"):
        detail = proxy_result.get("error") or "phone_proxy_unavailable"
        return _failure_result(f"phone_proxy_unavailable: {detail}")
    proxy = proxy_result.get("proxy") or ""

    print(f"[*] ChatGPT Phone Registration Started (country={country})")
    if proxy:
        print(f"[*] Phone registration proxy ready: region={proxy_result.get('region', '')} ip={proxy_result.get('ip', '')}")

    # Step 0: Acquire phone number from SMSBower
    _tick("0-Acquire phone number")
    from .smsbower import SmsBowerClient, normalize_phone
    sms_client = SmsBowerClient(api_key=api_key)
    try:
        activation = sms_client.get_number(service="dr", country=country)
    except Exception as e:
        _safe_tock()
        return _failure_result(f"smsbower_get_number_failed: {e}")
    phone = normalize_phone(activation.phone)
    print(f"[*] Phone: {phone}  Activation ID: {activation.activation_id}")
    _tock()

    # Step 1: Get sentinel tokens
    if sentinel_data:
        print("[*] Using provided sentinel tokens")
    else:
        _tick("1-Extract sentinel token")
        sentinel_data = _extract_sentinel(proxy=proxy, force_fresh=True)
        _tock()
    if not sentinel_data or not sentinel_data.get("sentinel_token"):
        sms_client.cancel(activation.activation_id)
        return _failure_result("sentinel_extract_failed", email=phone)

    # Step 2: Generate credentials
    explicit_password = bool(str(password or "").strip())
    password = password or _generate_password()
    first, last = _random_name()
    full_name = f"{first} {last}"
    birthdate = _random_birthdate()
    did = _sentinel_device_id(sentinel_data) or str(uuid.uuid4())
    session_logging_id = str(uuid.uuid4()).replace("-", "")

    _sentinel_token = sentinel_data["sentinel_token"]
    _sentinel_so_token = sentinel_data["sentinel_so_token"]
    print(f"[*] Phone: {phone}  Password: {password}  Name: {full_name}  Birth: {birthdate}")

    # Init session
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    _import_sentinel_cookies(session, sentinel_data, did)
    base_headers = {"User-Agent": ua, "Accept": "application/json"}

    try:
        # Auth flow: prime + signin + authorize
        _tick("2-Auth flow")
        request_with_retry(session, "get", f"{auth_base}/create-account", label="Auth prime",
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml"}, impersonate="chrome")

        csrf_resp = request_with_retry(session, "get", f"{chat_base}/api/auth/csrf", label="Auth csrf",
            headers={**base_headers, "Accept": "application/json", "Referer": f"{chat_base}/"},
            impersonate="chrome")
        csrf_token = (_json_or_raw(csrf_resp).get("csrfToken") or "").strip()

        # Key difference: prompt=login (not screen_hint=signup)
        signin_url = (
            f"{chat_base}/api/auth/signin/openai"
            f"?prompt=login&ext-oai-did={did}"
            f"&auth_session_logging_id={session_logging_id}"
            f"&login_hint={quote(phone, safe='')}"
        )
        signin_payload = {
            "csrfToken": csrf_token,
            "callbackUrl": f"{chat_base}/",
            "json": "true",
        }
        signin_resp = request_with_retry(session, "post", signin_url, label="Auth signin", data=urlencode(signin_payload),
            headers={**base_headers, "Content-Type": "application/x-www-form-urlencoded",
                     "Origin": chat_base, "Referer": f"{chat_base}/"},
            impersonate="chrome")
        signin_body = _json_or_raw(signin_resp, limit=1000)
        auth_session_url = signin_body.get("url") or signin_resp.headers.get("location") or signin_resp.url
        auth_session_url = _with_query_param(auth_session_url, "device_id", did)
        r = request_with_retry(session, "get", auth_session_url, label="Auth authorize",
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Origin": auth_base, "Referer": f"{chat_base}/"},
            impersonate="chrome")
        _tock()
        redirect_path = r.url.split("auth.openai.com")[-1]
        print(f"  Redirect: {redirect_path}")

        if _is_existing_login_redirect(r.url):
            sms_client.cancel(activation.activation_id)
            return _failure_result("phone_already_registered_or_login_redirect", email=phone)

        # Step 3: Register with phone + password
        _tick("3-User register (phone+password)")
        r = request_with_retry(session, "post", f"{auth_base}/api/accounts/user/register", label="User register",
            json={"password": password, "username": phone},
            headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/create-account/password",
                    "openai-sentinel-token": _sentinel_token},
            impersonate="chrome")
        _tock()

        reg_data = {}
        try: reg_data = r.json()
        except: reg_data = {"_raw": r.text[:300]}
        print(f"  Status: {r.status_code}")
        print(f"  Response: {json.dumps(reg_data, ensure_ascii=False)[:300]}")

        if r.status_code != 200:
            err_code = reg_data.get("error", {}).get("code", "")
            err_msg = reg_data.get("error", {}).get("message", str(reg_data))
            sms_client.cancel(activation.activation_id)
            return _failure_result(f"user_register: {err_msg}", email=phone)

        # Step 4: Wait for SMS code from SMSBower
        _tick("4-Wait SMS code")
        print(f"[*] Waiting for SMS code on {phone}...")
        code_result = sms_client.wait_for_code(activation.activation_id, timeout=180, interval=5)
        _tock()

        if not code_result or not code_result.get("code"):
            sms_client.cancel(activation.activation_id)
            return _failure_result("sms_code_timeout", email=phone)

        sms_code = code_result["code"]
        print(f"[*] SMS code received: {sms_code}")

        # Step 5: Validate phone OTP
        _tick("5-Validate phone OTP")
        validate_resp = request_with_retry(session, "post", f"{auth_base}/api/accounts/phone-otp/validate",
            label="Phone OTP validate",
            json={"code": sms_code},
            headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/phone-verification",
                    "openai-sentinel-token": _sentinel_token, "oai-device-id": did},
            impersonate="chrome")
        _tock()

        validate_data = {}
        try: validate_data = validate_resp.json()
        except: validate_data = {"_raw": validate_resp.text[:300]}
        print(f"  Status: {validate_resp.status_code}")
        print(f"  Response: {json.dumps(validate_data, ensure_ascii=False)[:300]}")

        if validate_resp.status_code != 200:
            err_msg = validate_data.get("error", {}).get("message", str(validate_data))
            sms_client.cancel(activation.activation_id)
            return _failure_result(f"phone_otp_validate: {err_msg}", email=phone)

        # Mark SMSBower activation as complete
        try:
            sms_client.complete(activation.activation_id)
        except Exception:
            pass

        continue_url = validate_data.get("continue_url") or validate_resp.headers.get("Location") or ""

        # Step 6: Create account
        _tick("6-Create account")
        create_body = {"name": full_name, "birthdate": birthdate}
        if continue_url:
            create_body["continue_url"] = continue_url
        create_resp = request_with_retry(session, "post", f"{auth_base}/api/accounts/create_account",
            label="Create account",
            json=create_body,
            headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/create-account/name",
                    "openai-sentinel-token": _sentinel_token, "openai-sentinel-so-token": _sentinel_so_token},
            impersonate="chrome")
        _tock()

        create_data = {}
        try: create_data = create_resp.json()
        except: create_data = {"_raw": create_resp.text[:300]}
        print(f"  Status: {create_resp.status_code}")
        print(f"  Response: {json.dumps(create_data, ensure_ascii=False)[:300]}")

        if create_resp.status_code != 200:
            err_msg = create_data.get("error", {}).get("message", str(create_data))
            return _failure_result(f"create_account: {err_msg}", email=phone)

    except Exception as e:
        _safe_tock()
        try: sms_client.cancel(activation.activation_id)
        except: pass
        return _failure_result(f"transport_error: {e}", email=phone)

    # Step 7: Fetch auth session for access_token
    _tick("7-Auth session")
    access_token = ""
    id_token = ""
    try:
        for attempt in range(6):
            session_resp = request_with_retry(session, "get", f"{chat_base}/api/auth/session",
                label=f"Auth session (attempt {attempt+1})",
                headers={**base_headers, "Referer": f"{chat_base}/"}, impersonate="chrome")
            session_data = _json_or_raw(session_resp, limit=2000)
            access_token = session_data.get("accessToken") or session_data.get("access_token") or ""
            id_token = session_data.get("idToken") or session_data.get("id_token") or ""
            if access_token:
                break
            time.sleep(1)
    except Exception as e:
        print(f"  Auth session error: {e}")
    _tock()

    if not access_token:
        return _failure_result("auth_session_no_token", email=phone, password=password)

    print(f"[*] Access token obtained: {access_token[:20]}...")

    # Step 8: (Optional) Codex OAuth
    refresh_token = ""
    if codex_oauth:
        _tick("8-Codex OAuth")
        try:
            from .codex_oauth import collect_codex_oauth_tokens
            oauth_result = collect_codex_oauth_tokens(
                access_token, proxy=proxy, device_id=did,
                phone_pool=None,  # phone already verified
                sentinel_data=sentinel_data,
            )
            refresh_token = (oauth_result.get("tokens") or {}).get("refresh_token") or ""
            if refresh_token:
                print(f"[*] Refresh token obtained: {refresh_token[:20]}...")
        except Exception as e:
            print(f"  Codex OAuth error: {e}")
        _tock()

    # Step 9: (Optional) Generate payment link
    paypal_result = {}
    if paypal_link and access_token:
        _tick("9-Payment link")
        try:
            from .gen_pp_link import generate_paypal_link
            paypal_result = generate_paypal_link(
                access_token, CFG, proxy=proxy, payment_method=payment_method,
            ) or {}
        except Exception as e:
            paypal_result = {"ok": False, "error": str(e)}
        _tock()

    _print_timings()

    return {
        "success": True,
        "email": phone,
        "phone": phone,
        "password": password,
        "name": full_name,
        "birthdate": birthdate,
        "access_token": access_token,
        "id_token": id_token,
        "refresh_token": refresh_token,
        "paypal": paypal_result,
        "activation_id": activation.activation_id,
        "source": "phone_register",
        "timing": _timing_summary(),
    }


def _registration_requires_refresh_token():
    cfg = CFG.get("codex_oauth") if isinstance(CFG.get("codex_oauth"), dict) else {}
    return bool(cfg.get("require_registration_refresh_token", True))


def _registration_requires_phone_verification(phone_pool=None):
    cfg = CFG.get("codex_oauth") if isinstance(CFG.get("codex_oauth"), dict) else {}
    default = bool(phone_pool)
    return bool(cfg.get("require_registration_phone_verification", default))


def _oauth_result_summary(result):
    if not isinstance(result, dict):
        return {}
    summary = {key: value for key, value in result.items() if key != "tokens"}
    tokens = result.get("tokens") if isinstance(result.get("tokens"), dict) else {}
    if tokens:
        summary["has_access_token"] = bool(tokens.get("access_token"))
        summary["has_refresh_token"] = bool(tokens.get("refresh_token"))
        summary["has_id_token"] = bool(tokens.get("id_token"))
    return summary


# Batch runner moved to sms_tool.batch_runner.
def run_batch(
    count=1,
    proxy=None,
    mailboxes=None,
    paypal_link=True,
    workers=4,
    phone_pool=None,
    codex_oauth=True,
    payment_method="paypal",
    paypal_generation_type=None,
    registration_mode=None,
):
    from .batch_runner import run_batch_impl
    return run_batch_impl(
        count=count, proxy=proxy, mailboxes=mailboxes, paypal_link=paypal_link,
        workers=workers, phone_pool=phone_pool, codex_oauth=codex_oauth,
        payment_method=payment_method, paypal_generation_type=paypal_generation_type,
        registration_mode=registration_mode, run_email_func=run_email,
    )


def _extract_nested(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def _build_session_file(data):
    mailbox = data.get("mailbox") or {}
    response = data.get("response") or {}
    auth_session = data.get("auth_session") or response.get("auth_session") or {}
    paypal = data.get("paypal") or {}
    session_token = (
        data.get("session_token")
        or response.get("session_token")
        or response.get("sessionToken")
        or _extract_nested(response, "session", "session_token")
        or auth_session.get("sessionToken")
        or auth_session.get("session_token")
    )
    access_token = (
        data.get("access_token")
        or response.get("access_token")
        or response.get("accessToken")
        or _extract_nested(response, "session", "access_token")
        or auth_session.get("accessToken")
        or auth_session.get("access_token")
    )
    id_token = (
        data.get("id_token")
        or data.get("idToken")
        or auth_session.get("idToken")
        or auth_session.get("id_token")
        or _extract_nested(auth_session, "session", "id_token")
        or _extract_nested(auth_session, "session", "idToken")
    )
    refresh_token = (
        data.get("refresh_token")
        or response.get("refresh_token")
        or response.get("refreshToken")
        or mailbox.get("refresh_token")
    )
    oauth_refresh_token = (
        data.get("oauth_refresh_token")
        or auth_session.get("refreshToken")
        or auth_session.get("refresh_token")
        or _extract_nested(auth_session, "session", "refresh_token")
        or _extract_nested(auth_session, "session", "refreshToken")
    )
    paypal_status = (
        data.get("paypal_status")
        or paypal.get("paypal_status")
        or paypal.get("status")
        or ("pm_created" if paypal.get("ok") and str(paypal.get("pm_id") or "").startswith("pm_") else "")
        or ("link_ready" if paypal.get("url") else "")
    )
    payment_method = (
        data.get("payment_method")
        or paypal.get("payment_method")
        or paypal.get("method")
        or ("paypal" if (paypal.get("url") or paypal.get("pm_id")) else "")
    )
    refresh_token_status = data.get("refresh_token_status") or ("oauth_present" if oauth_refresh_token else ("legacy_present" if refresh_token else "no_rt"))
    purchase = {
        "source": mailbox.get("source", ""),
        "provider": mailbox.get("provider", ""),
        "email": mailbox.get("email", ""),
        "purchase_id": mailbox.get("purchase_id", ""),
        "project_name": mailbox.get("project_name", ""),
        "price": mailbox.get("price", ""),
        "total_cost": mailbox.get("purchase_total_cost", ""),
        "balance_after": mailbox.get("balance_after", ""),
    }
    purchase = {key: value for key, value in purchase.items() if value}
    return {
        "email": data.get("email") or mailbox.get("email") or "",
        "phone": data.get("phone", ""),
        "password": data.get("password", ""),
        "session_token": session_token or "",
        "access_token": access_token or "",
        "id_token": id_token or "",
        "refresh_token": refresh_token or "",
        "device_id": data.get("device_id") or response.get("device_id") or "",
        "cookie_header": data.get("cookie_header") or response.get("cookie_header") or "",
        "auth_session": auth_session,
        "paypal": paypal,
        "payment_method": payment_method,
        "paypal_status": paypal_status,
        "registration_mode": data.get("registration_mode", ""),
        "oauth_refresh_token": oauth_refresh_token or "",
        "refresh_token_status": refresh_token_status,
        "timing": data.get("timing") or {},
        "pipeline_timing": data.get("pipeline_timing") or {},
        "purchase": data.get("purchase") or purchase,
        "mailbox": {
            "email": mailbox.get("email", ""),
            "password": mailbox.get("password", ""),
            "refresh_token": mailbox.get("refresh_token", ""),
            "access_token": mailbox.get("access_token", ""),
            "source": mailbox.get("source", ""),
            "provider": mailbox.get("provider", ""),
            "order_no": mailbox.get("order_no", ""),
            "token": mailbox.get("token", ""),
            "purchase_id": mailbox.get("purchase_id", ""),
            "project_name": mailbox.get("project_name", ""),
            "price": mailbox.get("price", ""),
            "purchase_total_cost": mailbox.get("purchase_total_cost", ""),
            "balance_after": mailbox.get("balance_after", ""),
        } if mailbox else {},
        "created_at": int(time.time()),
    }

