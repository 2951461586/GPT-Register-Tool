"""Phone-number (SMS OTP) ChatGPT registration orchestration."""

import json
import time
import uuid
from collections.abc import Mapping
from urllib.parse import quote, urlencode

from curl_cffi import requests as curl_requests

from .auth_flow import _auth_request_headers, _is_existing_login_redirect, _json_or_raw, _with_query_param
from .auth_headers import auth_impersonate, openai_auth_headers, select_auth_fingerprint
from .config import current_config_data
from .http_client import request_with_retry
from .registration_outcome import _failure_result
from .sentinel_tokens import _extract_sentinel, _import_sentinel_cookies, _sentinel_device_id
from .utils import (
    _generate_password,
    _print_timings,
    _random_birthdate,
    _random_name,
    _safe_tock,
    _tick,
    _timing_summary,
    _tl,
    _tock,
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
