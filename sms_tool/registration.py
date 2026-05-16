import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

from curl_cffi import requests as curl_requests

from .config import CFG
from .sms_provider import (
    _phone_sms_cfg,
    _resolve_sms_provider,
    _sms_balance,
    _sms_get_number,
    _sms_pick_best_country,
    _sms_poll,
    _sms_set_status,
)
from .utils import _generate_password, _print_timings, _random_birthdate, _random_name, _tick, _tock, _tl

# ==========================================
# Sentinel token (cached, Playwright only when needed)
# ==========================================
SENTINEL_CACHE_FILE = Path(__file__).parent / "sentinel_cache.json"

def _get_cached_sentinel(force_fresh=False):
    if force_fresh: return None
    if SENTINEL_CACHE_FILE.exists():
        try:
            with open(SENTINEL_CACHE_FILE) as f: cache = json.load(f)
            age = time.time() - cache.get("ts", 0)
            if age < 600 and cache.get("sentinel_token"):
                print(f"[*] Using cached sentinel token (age: {age:.0f}s)")
                return cache
        except: pass
    return None

def _save_sentinel_cache(data):
    data["ts"] = time.time()
    with open(SENTINEL_CACHE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"[*] Sentinel token cached")

def _extract_sentinel():
    cached = _get_cached_sentinel()
    if cached: return cached
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[Error] pip install playwright && playwright install chromium")
        return None

    auth_base = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}, locale="en-US", timezone_id="America/New_York")
        page = ctx.new_page()

        device_id = str(uuid.uuid4())
        state_val = secrets.token_urlsafe(32)
        scope = "openid email profile offline_access model.request model.read organization.read organization.write"
        auth_url = (
            f"{auth_base}/api/accounts/authorize"
            f"?client_id={CFG['chatgpt']['chat_web_client_id']}"
            f"&scope={quote(scope)}"
            f"&response_type=code"
            f"&redirect_uri={quote('https://chatgpt.com/api/auth/callback/openai')}"
            f"&audience={quote('https://api.openai.com/v1')}"
            f"&device_id={device_id}"
            f"&prompt=login"
            f"&screen_hint=signup"
            f"&state={state_val}"
        )
        try: page.goto(auth_url, wait_until="domcontentloaded", timeout=120000)
        except: page.goto(auth_url, wait_until="commit", timeout=120000)

        for i in range(30):
            time.sleep(2)
            if page.evaluate("() => typeof window.SentinelSDK !== 'undefined'"):
                print(f"  SentinelSDK loaded after {i*2}s"); break
        else:
            print("  SentinelSDK not loaded!"); browser.close(); return None

        page.evaluate("() => SentinelSDK.init()"); time.sleep(2)
        did = page.evaluate("() => document.cookie.match(/oai-did=([^;]+)/)?.[1] || ''")

        sentinel_token = page.evaluate(f"""(did) => {{
            return SentinelSDK.token().then(raw => {{
                const parsed = JSON.parse(raw);
                parsed.id = did;
                parsed.flow = 'username_password_create';
                return JSON.stringify(parsed);
            }});
        }}""", did)

        sentinel_so = page.evaluate(f"""(did) => {{
            return SentinelSDK.token().then(raw => {{
                const parsed = JSON.parse(raw);
                return JSON.stringify({{
                    so: raw, c: parsed.c, id: did, flow: 'oauth_create_account'
                }});
            }});
        }}""", did)

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        browser.close()

    result = {
        "sentinel_token": sentinel_token,
        "sentinel_so_token": sentinel_so,
        "cookie_str": cookie_str,
        "oai_did": did,
    }
    _save_sentinel_cache(result)
    return result


# ==========================================
# Core Phone Registration Flow
# ==========================================
def run_phone(proxy=None, phone=None, password=None, activation_id=None,
              sentinel_data=None, sms_service=None, country=None,
              sms_provider_name=None, mailbox=None, email_as_username=False):
    """Register a ChatGPT account via phone number."""
    _tl().clear()

    provider = _resolve_sms_provider(sms_provider_name)
    if sms_service is None: sms_service = _phone_sms_cfg().get("service", "dr")
    auth_base = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com")
    chat_base = CFG["chatgpt"].get("chat_base_url", "https://chatgpt.com")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36"

    print(f"[*] ChatGPT Phone Registration Started")

    # Step 0: Get sentinel tokens
    if sentinel_data:
        print("[*] Using provided sentinel tokens")
    else:
        _tick("0-Extract sentinel token")
        sentinel_data = _extract_sentinel()
        _tock()
    if not sentinel_data or not sentinel_data.get("sentinel_token"):
        return {"success": False, "error": "sentinel_extract_failed"}

    # Step 1: Generate credentials
    password = password or _generate_password()
    first, last = _random_name()
    full_name = f"{first} {last}"
    birthdate = _random_birthdate()

    # Step 2: Get phone number + auth flow, retry if already registered
    new_activation = False
    if country:
        selected_country = country
    elif _phone_sms_cfg().get("country"):
        selected_country = _phone_sms_cfg()["country"]
    else:
        max_price = _phone_sms_cfg().get("max_price", 0.08)
        min_price = _phone_sms_cfg().get("min_price", 0.04)
        selected_country, _ = _sms_pick_best_country(provider, sms_service, max_price=max_price, min_price=min_price)

    for retry in range(3):
        if not phone and not activation_id:
            _tick(f"2-Get phone number (attempt {retry+1})")
            _sms_balance(provider)
            activation_id, phone = _sms_get_number(provider, service=sms_service, country=selected_country)
            _tock()
            new_activation = True
            if not activation_id or not phone:
                return {"success": False, "error": f"{provider.name}_get_number_failed"}
            if not phone.startswith("+"): phone = "+" + phone
            print(f"[*] Got phone: {phone} (activation: {activation_id}, country={selected_country})")
        elif phone and not activation_id:
            if not phone.startswith("+"): phone = "+" + phone

        username = mailbox.email if (email_as_username and mailbox and mailbox.email) else phone
        did = sentinel_data.get("oai_did", str(uuid.uuid4()))
        session_logging_id = str(uuid.uuid4()).replace("-", "")
        print(f"[*] Username: {username}  Phone: {phone}  Password: {password}  Name: {full_name}  Birth: {birthdate}")

        # Init curl_cffi session
        session = curl_requests.Session()
        base_headers = {"User-Agent": ua, "Accept": "application/json"}

        # Auth flow: prime + signin + authorize
        _tick(f"3-Auth flow (attempt {retry+1})")
        session.get(f"{auth_base}/create-account",
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml"}, impersonate="chrome", timeout=30)

        signin_url = (
            f"{chat_base}/api/auth/signin/openai"
            f"?prompt=login&ext-oai-did={did}"
            f"&auth_session_logging_id={session_logging_id}"
            f"&screen_hint=login_or_signup"
            f"&login_hint={quote(username, safe='')}"
        )
        session.post(signin_url, data=urlencode({"csrfToken": "true"}),
            headers={**base_headers, "Content-Type": "application/x-www-form-urlencoded",
                     "Origin": chat_base, "Referer": f"{chat_base}/"},
            impersonate="chrome", timeout=30)

        scope = "openid email profile offline_access model.request model.read organization.read organization.write"
        auth_session_url = (
            f"{auth_base}/api/accounts/authorize"
            f"?client_id={CFG['chatgpt']['chat_web_client_id']}"
            f"&scope={quote(scope)}&response_type=code"
            f"&redirect_uri={quote('https://chatgpt.com/api/auth/callback/openai')}"
            f"&audience={quote('https://api.openai.com/v1')}"
            f"&device_id={did}&prompt=login&screen_hint=login_or_signup"
            f"&login_hint={quote(username, safe='')}"
            f"&state={secrets.token_urlsafe(16)}"
        )
        r = session.get(auth_session_url,
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Origin": auth_base, "Referer": f"{chat_base}/"},
            impersonate="chrome", timeout=30)
        _tock()
        redirect_path = r.url.split("auth.openai.com")[-1]
        print(f"  Redirect: {redirect_path}")

        if "log-in" in redirect_path or "login" in redirect_path:
            print(f"  [!] Username already registered, trying another...")
            if new_activation: _sms_set_status(provider, activation_id, 8)
            activation_id = None; phone = None; new_activation = False
            time.sleep(2); continue
        break
    else:
        return {"success": False, "phone": phone, "error": "all_numbers_already_registered"}

    # Step 4: Register with username + password
    _tick("4-User register (username+password)")
    r = session.post(f"{auth_base}/api/accounts/user/register",
        json={"password": password, "username": username},
        headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/create-account/password",
                "openai-sentinel-token": sentinel_data["sentinel_token"]},
        impersonate="chrome", timeout=30)
    _tock()

    reg_data = {}
    try: reg_data = r.json()
    except: reg_data = {"_raw": r.text[:300]}
    print(f"  Status: {r.status_code}")
    print(f"  Response: {json.dumps(reg_data, ensure_ascii=False)[:300]}")

    if r.status_code != 200:
        err = reg_data.get("error", {}).get("message", str(reg_data))
        if new_activation: _sms_set_status(provider, activation_id, 8)
        return {"success": False, "phone": phone, "error": f"user_register: {err}"}

    # Step 5: Trigger SMS send
    _tick("5-Trigger SMS send")
    continue_url = reg_data.get("continue_url", "")
    if continue_url:
        r = session.get(continue_url,
            headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/create-account/password"},
            impersonate="chrome", timeout=30)
        print(f"  SMS send: {r.status_code}")
        try: print(f"  {json.dumps(r.json(), ensure_ascii=False)[:200]}")
        except: pass
    _tock()

    # Step 6: Get SMS OTP
    _tick("6-Get SMS OTP")
    code = None
    if activation_id:
        code = _sms_poll(provider, activation_id, timeout=120)
        if not code:
            print("  No code, calling resend...")
            r = session.post(f"{auth_base}/api/accounts/phone-otp/resend",
                headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/contact-verification"},
                impersonate="chrome", timeout=30)
            print(f"  Resend: {r.status_code}")
            try: print(f"  {json.dumps(r.json(), ensure_ascii=False)[:200]}")
            except: pass
            if r.status_code == 200:
                code = _sms_poll(provider, activation_id, timeout=180)
    else:
        print(f"\n[*] SMS OTP sent to {phone}")
        code = input("[*] Enter the 6-digit code: ").strip()
        if not re.match(r"^\d{6}$", code): code = None
    _tock()
    if not code:
        if new_activation: _sms_set_status(provider, activation_id, 8)
        return {"success": False, "phone": phone, "error": "sms_poll_timeout"}

    # Step 7: Validate phone OTP
    _tick("7-Validate phone OTP")
    r = session.post(f"{auth_base}/api/accounts/phone-otp/validate",
        json={"code": code},
        headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/contact-verification"},
        impersonate="chrome", timeout=30)
    _tock()

    otp_data = {}
    try: otp_data = r.json()
    except: otp_data = {"_raw": r.text[:300]}
    print(f"  Status: {r.status_code}")
    print(f"  Response: {json.dumps(otp_data, ensure_ascii=False)[:300]}")

    if r.status_code != 200:
        err = otp_data.get("error", {}).get("message", str(otp_data))
        if new_activation: _sms_set_status(provider, activation_id, 8)
        return {"success": False, "phone": phone, "error": f"phone_otp_validate: {err}"}

    # Step 8: Create account
    _tick("8-Create account")
    r = session.post(f"{auth_base}/api/accounts/create_account",
        json={"name": full_name, "birthdate": birthdate},
        headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/about-you",
                "openai-sentinel-token": sentinel_data["sentinel_token"],
                "openai-sentinel-so-token": sentinel_data["sentinel_so_token"]},
        impersonate="chrome", timeout=30)
    _tock()

    create_data = {}
    try: create_data = r.json()
    except: create_data = {"_raw": r.text[:300]}
    print(f"  Status: {r.status_code}")
    print(f"  Response: {json.dumps(create_data, ensure_ascii=False)[:300]}")

    result = {
        "success": r.status_code == 200,
        "email": mailbox.email if mailbox else "",
        "phone": phone,
        "password": password,
        "name": full_name,
        "birthdate": birthdate,
        "response": create_data,
        "activation_id": activation_id,
        "sms_provider": provider.name,
    }
    if mailbox:
        result["mailbox"] = {
            "email": mailbox.email,
            "password": mailbox.password,
            "refresh_token": mailbox.refresh_token,
            "access_token": mailbox.access_token,
            "source": mailbox.source,
        }
    _print_timings()
    return result


def run_batch(count=1, proxy=None, sms_service=None, country=None, sms_provider_name=None,
              mailboxes=None, email_as_username=False):
    if sms_service is None: sms_service = _phone_sms_cfg().get("service", "dr")
    results = []
    print(f"\n{'=' * 60}")
    print(f"  ChatGPT Phone Batch Registration - {count} accounts")
    print(f"{'=' * 60}\n")

    for i in range(count):
        print(f"\n{'#' * 40}")
        print(f"  Account {i + 1}/{count}")
        print(f"{'#' * 40}")
        if i > 0 and SENTINEL_CACHE_FILE.exists():
            SENTINEL_CACHE_FILE.unlink(); time.sleep(3)
        try:
            mailbox = mailboxes[i % len(mailboxes)] if mailboxes else None
            results.append(run_phone(proxy=proxy, sms_service=sms_service, country=country,
                                     sms_provider_name=sms_provider_name, mailbox=mailbox,
                                     email_as_username=email_as_username))
        except Exception as e:
            import traceback; traceback.print_exc()
            results.append({"success": False, "error": str(e)})
    return results


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
    session_token = (
        data.get("session_token")
        or response.get("session_token")
        or response.get("sessionToken")
        or _extract_nested(response, "session", "session_token")
    )
    access_token = (
        data.get("access_token")
        or response.get("access_token")
        or response.get("accessToken")
        or _extract_nested(response, "session", "access_token")
    )
    refresh_token = (
        data.get("refresh_token")
        or response.get("refresh_token")
        or response.get("refreshToken")
        or mailbox.get("refresh_token")
    )
    return {
        "email": data.get("email") or mailbox.get("email") or "",
        "phone": data.get("phone", ""),
        "password": data.get("password", ""),
        "session_token": session_token or "",
        "access_token": access_token or "",
        "refresh_token": refresh_token or "",
        "device_id": data.get("device_id") or response.get("device_id") or "",
        "cookie_header": data.get("cookie_header") or response.get("cookie_header") or "",
        "mailbox": {
            "email": mailbox.get("email", ""),
            "password": mailbox.get("password", ""),
            "refresh_token": mailbox.get("refresh_token", ""),
            "access_token": mailbox.get("access_token", ""),
            "source": mailbox.get("source", ""),
        } if mailbox else {},
        "sms": {
            "provider": data.get("sms_provider", ""),
            "activation_id": data.get("activation_id", ""),
        },
        "created_at": int(time.time()),
    }

