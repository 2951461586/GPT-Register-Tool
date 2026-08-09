import json
import time
import sys
import uuid
from collections.abc import Mapping
from urllib.parse import quote, urlencode

from curl_cffi import requests as curl_requests

from .codex_sentinel import import_cookie_header, load_cached_sentinel, with_sentinel
from .auth_headers import (
    auth_fingerprint_capabilities,
    auth_impersonate,
    chatgpt_headers,
    curl_cffi_capabilities,
    current_auth_fingerprint,
    openai_auth_headers,
    nextauth_headers,
    set_fingerprint_geo,
    select_auth_fingerprint,
)
from .error_classification import classify_error
from .config import CFG, current_config_data, resolve_runtime_config, runtime_config_scope, validate_config
from .http_client import request_with_retry
from .phone_proxy import normalize_proxy_url, refresh_proxy_sid
from .sentinel_tokens import (
    _cookie_jar_header,
    _extract_sentinel,
    _extract_sentinel_http,
    assert_sentinel_device_id,
    _import_sentinel_cookies,
    _sentinel_device_id,
    _sentinel_frame_version,
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
    _is_user_already_exists,
    _is_wrong_email_otp_code,
    _minimal_chatgpt_cookie_header,
    _validate_email_otp,
)
from .http_utils import _follow_continue_url
from .auth_state import fetch_client_auth_session_dump as _fetch_client_auth_session_dump
from .otp_strategy import send_registration_email_otp as _send_registration_email_otp
from .mailbox import _ensure_mailbox_account, _poll_email_otp, _snapshot_mailbox_message
from .paths import runtime_file
from .registration_progress import registration_stage, track_registration
from .registration_state import (
    RegistrationStage,
    RegistrationState,
    RegistrationStateMachine,
    prepare_registration_context,
)
from .sanitizer import sanitize as _sanitize, sanitize_text as _sanitize_text
from . import account_liveness
from .utils import _generate_password, _print_timings, _random_birthdate, _random_name, _tick, _timing_summary, _tock, _tl, think_stage

REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD = "verification code"
LOGIN_EMAIL_OTP_SUBJECT_KEYWORD = "login code"
REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS = f"{REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD}|{LOGIN_EMAIL_OTP_SUBJECT_KEYWORD}"


def probe_account_liveness(*args, **kwargs):
    """Delegate dynamically so runtime instrumentation and tests can replace the canonical probe."""
    return account_liveness.probe_account_liveness(*args, **kwargs)

# ==========================================
# Sentinel token (cached, browser only when needed)
# ==========================================
def _mailbox_snapshot(mailbox):
    if not mailbox:
        return {}
    return {
        "email": getattr(mailbox, "email", ""),
        "password": getattr(mailbox, "password", ""),
        "login_password": getattr(mailbox, "login_password", ""),
        "refresh_token": getattr(mailbox, "refresh_token", ""),
        "access_token": getattr(mailbox, "access_token", ""),
        "source": getattr(mailbox, "source", ""),
        "provider": getattr(mailbox, "provider", ""),
        "order_no": getattr(mailbox, "order_no", ""),
        "token": getattr(mailbox, "token", ""),
        "client_secret": getattr(mailbox, "client_secret", ""),
        "auth_mode": getattr(mailbox, "auth_mode", ""),
        "sender_name": getattr(mailbox, "sender_name", ""),
        "purchase_id": getattr(mailbox, "purchase_id", ""),
        "project_name": getattr(mailbox, "project_name", ""),
        "price": getattr(mailbox, "price", ""),
        "purchase_total_cost": getattr(mailbox, "purchase_total_cost", ""),
        "balance_after": getattr(mailbox, "balance_after", ""),
    }


def _failure_result(error, email="", mailbox=None, password=""):
    result = {"success": False, "error": _sanitize_text(error), "failure_class": classify_error(_sanitize_text(error)), "timing": _timing_summary()}
    if email:
        result["email"] = email
    if password:
        result["password"] = "[REDACTED]"
    mailbox_data = _mailbox_snapshot(mailbox)
    if mailbox_data:
        result["mailbox"] = mailbox_data
    return _sanitize(result)


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




def _registration_outcome(create_ok, create_data, access_token, at_probe):
    probe = at_probe if isinstance(at_probe, dict) else {}
    try:
        status_code = int(probe.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    create_error = _create_account_error(create_ok, create_data or {})
    success = bool(str(access_token or "").strip()) and status_code == 200
    if success:
        return True, "", create_error
    if not str(access_token or "").strip():
        return False, create_error or "missing_auth_session_access_token", ""
    if status_code:
        return False, f"access_token_probe_http_{status_code}", create_error
    probe_error = str(probe.get("error") or probe.get("status") or "unknown").strip()
    return False, f"access_token_probe_failed:{probe_error}", create_error



# Sentinel token extraction moved to sms_tool.sentinel_tokens.

def _resolve_proxy_scheme(proxy):
    """Detect working proxy scheme. Many providers labeled socks5h:// are actually HTTP CONNECT proxies."""
    proxy = normalize_proxy_url(proxy)
    if not proxy or not proxy.startswith(("socks5h://", "socks5://")):
        return proxy
    # Quick connectivity test: try socks5h first, fall back to http
    http_proxy = proxy.replace("socks5h://", "http://").replace("socks5://", "http://")
    for scheme, test_proxy in [(proxy, proxy), ("http://", http_proxy)]:
        try:
            s = curl_requests.Session()
            s.proxies = {"http": test_proxy, "https": test_proxy}
            s.get("http://ip-api.com/json?fields=query", timeout=15, impersonate=auth_impersonate())
            if scheme != proxy:
                print(f"[*] Proxy scheme auto-corrected: socks5h:// → http://")
            return test_proxy
        except Exception:
            continue
    # Both failed, return original and let caller handle
    print("[!] Warning: proxy connectivity test failed with both socks5h:// and http:// schemes")
    return proxy


def registration_network_preflight(proxy=None, *, proxy_attempts: int = 2):
    """Validate the three auth edge nodes before claiming a mailbox."""
    capabilities = curl_cffi_capabilities()
    profile_capabilities = auth_fingerprint_capabilities()
    if not capabilities["version_ok"] and profile_capabilities["missing"]:
        raise RuntimeError(
            "auth_fingerprint_unavailable:curl_cffi_requires_0.15.x_or_0.16.x"
        )
    if profile_capabilities["missing"]:
        raise RuntimeError(
            "auth_fingerprint_unavailable:" + ",".join(profile_capabilities["missing"])
        )
    chat_base = str((CFG.get("chatgpt") or {}).get("chat_base_url") or "https://chatgpt.com").rstrip("/")
    auth_base = str((CFG.get("chatgpt") or {}).get("auth_base_url") or "https://auth.openai.com").rstrip("/")
    sentinel_url = "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=" + _sentinel_frame_version()
    checks = (
        ("chatgpt-login", f"{chat_base}/login", f"{chat_base}/"),
        ("auth-login", f"{auth_base}/log-in", f"{chat_base}/login"),
        ("sentinel-frame", sentinel_url, f"{auth_base}/log-in"),
    )
    candidate = normalize_proxy_url(proxy) or None
    last_error = None
    for attempt in range(max(1, min(int(proxy_attempts or 1), 3))):
        session = curl_requests.Session()
        try:
            session.trust_env = False
        except Exception:
            pass
        session.proxies = {"http": candidate, "https": candidate} if candidate else {"http": "", "https": ""}
        try:
            for label, url, referer in checks:
                headers = openai_auth_headers(
                    referer=referer,
                    origin=url.split("/", 3)[0] + "//" + url.split("/", 3)[2],
                    accept="text/html,application/xhtml+xml",
                    include_trace=True,
                    extra={
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "same-site",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                response = session.get(url, headers=headers, timeout=15, impersonate=auth_impersonate())
                if int(getattr(response, "status_code", 0) or 0) >= 400:
                    raise RuntimeError(f"registration_preflight_failed:{label}:http_{response.status_code}")
            result = {"ok": True, "profile": current_auth_fingerprint()["impersonate"]}
            original = normalize_proxy_url(proxy) or ""
            if candidate and candidate != original:
                result["proxy"] = candidate
            return result
        except Exception as exc:
            last_error = exc
            if attempt + 1 >= max(1, min(int(proxy_attempts or 1), 3)) or not candidate:
                break
            candidate = refresh_proxy_sid(candidate)
        finally:
            try:
                session.close()
            except Exception:
                pass
    raise RuntimeError(str(last_error or "registration_preflight_failed"))


# Sentinel orchestration/browser fallback moved to sms_tool.sentinel_tokens.


# Account creation/session helpers moved to sms_tool.account_creation.


def _auth_request_headers(base_headers, did="", referer="", origin="", sentinel_token="", sentinel_so_token="", extra=None):
    return {
        **(base_headers or {}),
        **openai_auth_headers(
            did,
            referer=referer,
            origin=origin,
            sentinel_token=sentinel_token,
            sentinel_so_token=sentinel_so_token,
            extra=extra or {},
        ),
    }


def _send_existing_login_otp(session, auth_base, base_headers, current_url, did, sentinel_token="", sentinel_so_token=""):
    headers = _auth_request_headers(
        base_headers,
        did=did,
        referer=current_url or f"{auth_base}/email-verification",
        origin=auth_base,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
        extra={"Content-Type": "application/json"},
    )
    last_response = None
    for endpoint in (
        "/api/accounts/passwordless/send-otp",
        "/api/accounts/email-otp/send",
        "/api/accounts/email-otp/resend",
    ):
        response = request_with_retry(
            session,
            "post",
            _absolute_url(auth_base, endpoint),
            label=f"Existing account OTP send {endpoint}",
            json={},
            headers=headers,
            impersonate=auth_impersonate(),
        )
        last_response = response
        body_preview = ""
        try:
            body_preview = json.dumps(response.json(), ensure_ascii=False)[:200]
        except Exception:
            body_preview = (response.text or "")[:200]
        print(f"  Existing account OTP send: {endpoint} {response.status_code} {body_preview}")
        if response.status_code in (200, 202, 204):
            return True, response
        # 409 may mean "OTP already sent recently" — treat as success but
        # only when the response body confirms a pending OTP. Otherwise keep
        # trying alternate endpoints.
        if response.status_code == 409:
            body_lower = body_preview.lower()
            if "already" in body_lower or "pending" in body_lower or "rate" in body_lower or "too_many" in body_lower:
                return True, response
            # Ambiguous 409: try next endpoint
            continue
        if response.status_code not in (400, 404, 405):
            return False, response
    # All endpoints exhausted; return last response so caller can decide
    if last_response is not None:
        return False, last_response
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
        impersonate=auth_impersonate(),
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
        impersonate=auth_impersonate(),
    )
    current_url = str(authorize_resp.url or "")
    print(f"  Existing account authorize: {authorize_resp.status_code} {current_url}")

    current_lower = current_url.lower()
    if "chatgpt.com" in current_lower and ("/api/auth/callback/openai" in current_lower or current_lower.rstrip("/") == chat_base.lower().rstrip("/")):
        return {"ok": True}

    # Always call authorize/continue to ensure the auth session transitions
    # from signup state to login state.  Previously this was skipped when the
    # authorize redirect landed on /email-verification, which left the session
    # in a signup state and caused OTP send to return 409.
    continue_resp = request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/authorize/continue",
        label="Existing account continue",
        json={"username": {"value": username, "kind": "email"}},
        headers=_auth_request_headers(
            base_headers,
            did=did,
            referer=current_url or f"{auth_base}/log-in",
            origin=auth_base,
            sentinel_token=sentinel_token,
            sentinel_so_token=sentinel_so_token,
            extra={"Content-Type": "application/json"},
        ),
        impersonate=auth_impersonate(),
    )
    print(f"  Existing account continue: {continue_resp.status_code}")
    if continue_resp.status_code == 200:
        next_url = _response_next_url(continue_resp, auth_base)
        if next_url:
            try:
                follow_resp = _follow_continue_url(
                    session,
                    next_url,
                    base_headers,
                    referer=next_url,
                    label="Existing account continue follow",
                )
                current_url = str(getattr(follow_resp, "url", "") or next_url)
            except Exception as e:
                print(f"  Existing account continue follow transport warning: {e}")
    elif continue_resp.status_code not in (409, 400):
        return {"ok": False, "error": f"existing_login_continue_failed:{continue_resp.status_code}"}

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

    email_cfg = current_config_data().get("email_registration", {})
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


def _normalize_registration_mode(value=None):
    raw = str(value or "").strip().lower().replace("-", "_")
    if not raw:
        value = current_config_data().get("email_registration")
        cfg = value if isinstance(value, Mapping) else {}
        raw = str(cfg.get("registration_mode") or cfg.get("signup_mode") or "passwordless").strip().lower().replace("-", "_")
    if raw in {"password", "password_signup", "user_register", "legacy"}:
        return "password"
    if raw in {"passwordless", "passwordless_signup", "login_or_signup", "har"}:
        return "passwordless"
    return "passwordless"


def _poll_registration_email_otp(
    mailbox,
    *,
    subject_keyword,
    timeout,
    issued_after_unix,
    proxy=None,
    excluded_otps=None,
    resend_callback=None,
    resend_after_seconds=None,
    poll_otp_fn=None,
):
    poll_otp_fn = poll_otp_fn or _poll_email_otp
    total_timeout = max(0, int(timeout or 0))
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    if provider != "remail" or resend_callback is None:
        return poll_otp_fn(
            mailbox,
            subject_keyword=subject_keyword,
            timeout=total_timeout,
            issued_after_unix=issued_after_unix,
            proxy=proxy,
            excluded_otps=excluded_otps,
        )
    if resend_after_seconds is None:
        value = current_config_data().get("email_registration")
        email_cfg = value if isinstance(value, Mapping) else {}
        resend_after_seconds = email_cfg.get("remail_otp_resend_after_seconds", 30)
    try:
        first_window = max(0, int(resend_after_seconds or 0))
    except (TypeError, ValueError):
        first_window = 30
    if first_window <= 0 or first_window >= total_timeout:
        return _poll_email_otp(
            mailbox,
            subject_keyword=subject_keyword,
            timeout=total_timeout,
            issued_after_unix=issued_after_unix,
            proxy=proxy,
            excluded_otps=excluded_otps,
        )
    code = poll_otp_fn(
        mailbox,
        subject_keyword=subject_keyword,
        timeout=first_window,
        issued_after_unix=issued_after_unix,
        proxy=proxy,
        excluded_otps=excluded_otps,
    )
    if code:
        return code
    registration_stage("email_otp_resend")
    try:
        response = resend_callback()
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in (200, 202, 204, 409):
            print(f"  ReMail OTP resend was not accepted: {status}")
    except Exception as exc:
        print(f"  ReMail OTP resend warning: {exc}")
    registration_stage("email_otp_wait")
    return poll_otp_fn(
        mailbox,
        subject_keyword=subject_keyword,
        timeout=total_timeout - first_window,
        issued_after_unix=issued_after_unix,
        proxy=proxy,
        excluded_otps=excluded_otps,
    )


@track_registration
def run_email(
    proxy=None,
    password=None,
    sentinel_data=None,
    mailbox=None,
    phone_pool=None,
    codex_oauth=True,
    registration_mode=None,
    browser_headless: bool | None = None,
    runtime_config=None,
):
    """Run the staged email-registration workflow."""
    from .registration_handlers import RegistrationEmailWorkflow

    flow = RegistrationStateMachine(registration_stage)
    config = resolve_runtime_config(runtime_config, workflow="registration")
    return RegistrationEmailWorkflow(
        flow,
        proxy=proxy,
        password=password,
        sentinel_data=sentinel_data,
        mailbox=mailbox,
        phone_pool=phone_pool,
        codex_oauth=codex_oauth,
        registration_mode=registration_mode,
        browser_headless=browser_headless,
        config=config.data,
        operations=sys.modules[__name__],
    ).run()


def run_phone(*args, **kwargs):
    """Compatibility wrapper; SMS/phone registration has been removed from the active flow."""
    return run_email(
        proxy=kwargs.get("proxy"),
        password=kwargs.get("password"),
        sentinel_data=kwargs.get("sentinel_data"),
        mailbox=kwargs.get("mailbox"),
        phone_pool=kwargs.get("phone_pool"),
        codex_oauth=kwargs.get("codex_oauth", True),
        registration_mode=kwargs.get("registration_mode"),
        browser_headless=kwargs.get("browser_headless"),
        runtime_config=kwargs.get("runtime_config"),
    )


def run_phone_register(
    proxy=None,
    password=None,
    sentinel_data=None,
    codex_oauth=True,
    smsbower_country=None,
    smsbower_api_key=None,
    bind_email=None,
):
    """Register a ChatGPT account via phone number (SMS OTP), then optionally bind email."""
    _tl().clear()
    select_auth_fingerprint(rotate=True)

    config = current_config_data()
    auth_base = config["chatgpt"].get("auth_base_url", "https://auth.openai.com")
    chat_base = config["chatgpt"].get("chat_base_url", "https://chatgpt.com")

    # Load SMSBower config before buying a number so the proxy can be matched
    # to the phone country and verified first.
    phone_value = config.get("phone_reuse")
    phone_reuse_cfg = phone_value if isinstance(phone_value, Mapping) else {}
    smsbower_value = phone_reuse_cfg.get("smsbower")
    smsbower_cfg = smsbower_value if isinstance(smsbower_value, Mapping) else {}
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
        try:
            sentinel_data = _extract_sentinel(proxy=proxy, force_fresh=True, persist=False)
            _tock()
        except Exception as exc:
            _safe_tock()
            sms_client.cancel(activation.activation_id)
            return _failure_result(f"sentinel_extract_failed: {exc}", email=phone)
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
    # 密码是敏感凭据，绝不打印明文到 stdout（日志会被采集/分享）。
    # 跟 run_email 的 passwordless 分支保持一致的脱敏标记。
    _phone_display = f"{phone[:3]}***{phone[-3:]}" if phone and len(phone) >= 6 else "(none)"
    print(f"[*] Phone: {_phone_display}  Password: [generated]  Name: {full_name}  Birth: {birthdate}")

    # Init session
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    _import_sentinel_cookies(session, sentinel_data, did)
    base_headers = openai_auth_headers(did, accept="application/json", include_trace=True)

    try:
        # Auth flow: prime + signin + authorize
        _tick("2-Auth flow")
        request_with_retry(session, "get", f"{auth_base}/create-account", label="Auth prime",
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml"}, impersonate=auth_impersonate())

        csrf_resp = request_with_retry(session, "get", f"{chat_base}/api/auth/csrf", label="Auth csrf",
            headers={**base_headers, "Accept": "application/json", "Referer": f"{chat_base}/"},
            impersonate=auth_impersonate())
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
            impersonate=auth_impersonate())
        signin_body = _json_or_raw(signin_resp, limit=1000)
        auth_session_url = signin_body.get("url") or signin_resp.headers.get("location") or signin_resp.url
        auth_session_url = _with_query_param(auth_session_url, "device_id", did)
        r = request_with_retry(session, "get", auth_session_url, label="Auth authorize",
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Origin": auth_base, "Referer": f"{chat_base}/"},
            impersonate=auth_impersonate())
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
            headers=_auth_request_headers(
                base_headers,
                did=did,
                referer=f"{auth_base}/create-account/password",
                origin=auth_base,
                sentinel_token=_sentinel_token,
            ),
            impersonate=auth_impersonate())
        _tock()

        reg_data = {}
        try: reg_data = r.json()
        except (ValueError, TypeError): reg_data = {"_raw": r.text[:300]}
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
            headers=_auth_request_headers(
                base_headers,
                did=did,
                referer=f"{auth_base}/phone-verification",
                origin=auth_base,
                sentinel_token=_sentinel_token,
            ),
            impersonate=auth_impersonate())
        _tock()

        validate_data = {}
        try: validate_data = validate_resp.json()
        except (ValueError, TypeError): validate_data = {"_raw": validate_resp.text[:300]}
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
            headers=_auth_request_headers(
                base_headers,
                did=did,
                referer=f"{auth_base}/create-account/name",
                origin=auth_base,
                sentinel_token=_sentinel_token,
                sentinel_so_token=_sentinel_so_token,
            ),
            impersonate=auth_impersonate())
        _tock()

        create_data = {}
        try: create_data = create_resp.json()
        except (ValueError, TypeError): create_data = {"_raw": create_resp.text[:300]}
        print(f"  Status: {create_resp.status_code}")
        print(f"  Response: {json.dumps(create_data, ensure_ascii=False)[:300]}")

        if create_resp.status_code != 200:
            err_msg = create_data.get("error", {}).get("message", str(create_data))
            return _failure_result(f"create_account: {err_msg}", email=phone)

    except Exception as e:
        _safe_tock()
        try: sms_client.cancel(activation.activation_id)
        except Exception: pass
        return _failure_result(f"transport_error: {e}", email=phone)

    # Step 7: Fetch auth session for access_token
    _tick("7-Auth session")
    access_token = ""
    id_token = ""
    try:
        for attempt in range(6):
            session_resp = request_with_retry(session, "get", f"{chat_base}/api/auth/session",
                label=f"Auth session (attempt {attempt+1})",
                headers={**base_headers, "Referer": f"{chat_base}/"}, impersonate=auth_impersonate())
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

    print("[*] Access token obtained")

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
                print("[*] Refresh token obtained")
        except Exception as e:
            print(f"  Codex OAuth error: {e}")
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
        "activation_id": activation.activation_id,
        "source": "phone_register",
        "timing": _timing_summary(),
    }


# ===== 结果判定 / 代理解析 / 输出生成 — 委托独立模块 =====
from .registration_outcome import (
    _create_account_error,
    _probe_registration_access_token as _probe_registration_access_token_impl,
    _registration_requires_phone_verification,
    _registration_requires_refresh_token,
)
from .session_builder import build_session_file


def _probe_registration_access_token(access_token, auth_session, proxy=None, cfg=None):
    return _probe_registration_access_token_impl(
        access_token,
        auth_session,
        proxy=proxy,
        cfg=cfg or current_config_data(),
        probe_fn=probe_account_liveness,
        stage_fn=registration_stage,
        sleep_fn=time.sleep,
    )


def run_batch(
    count=1,
    proxy=None,
    proxy_pool=None,
    mailboxes=None,
    workers=4,
    phone_pool=None,
    codex_oauth=True,
    registration_mode=None,
    max_attempts=2,
    retry_delay_seconds=1.0,
    browser_headless=None,
):
    """Compatibility entry point for callers importing ``registration.run_batch``."""
    from .batch_runner import run_batch_impl

    return run_batch_impl(
        count=count,
        proxy=proxy,
        proxy_pool=proxy_pool,
        mailboxes=mailboxes,
        workers=workers,
        phone_pool=phone_pool,
        codex_oauth=codex_oauth,
        registration_mode=registration_mode,
        max_attempts=max_attempts,
        retry_delay_seconds=retry_delay_seconds,
        browser_headless=browser_headless,
        run_email_func=run_email,
    )

# 保持向后兼容（cli.py 等通过 `_build_session_file` 引用）
_build_session_file = build_session_file


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
