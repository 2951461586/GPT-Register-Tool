import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, quote, urlencode, urlparse

from curl_cffi import requests as curl_requests

from .config import CFG
from .http_client import request_with_retry
from .mailbox import _ensure_mailbox_account, _poll_email_otp, _snapshot_mailbox_message
from .paths import runtime_file
from .utils import _generate_password, _print_timings, _random_birthdate, _random_name, _tick, _timing_summary, _tock, _tl

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



def _safe_tock():
    timings = _tl()
    if timings and timings[-1][1] > 1_000_000:
        _tock()

def _get_cached_sentinel(force_fresh=False):
    if force_fresh: return None
    if SENTINEL_CACHE_FILE.exists():
        try:
            with open(SENTINEL_CACHE_FILE) as f: cache = json.load(f)
            age = time.time() - cache.get("ts", 0)
            ttl = int((CFG.get("timeouts") or {}).get("token_cache_ttl", 600) or 600)
            if age < ttl and cache.get("sentinel_token"):
                print(f"[*] Using cached sentinel token (age: {age:.0f}s)")
                return cache
        except: pass
    return None

def _save_sentinel_cache(data):
    data["ts"] = time.time()
    with open(SENTINEL_CACHE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"[*] Sentinel token cached")

def _extract_sentinel(proxy=None):
    cached = _get_cached_sentinel()
    if cached: return cached
    browser_proxy = proxy.replace("socks5h://", "socks5://") if proxy else None
    return _extract_sentinel_cloakbrowser(browser_proxy)


def _extract_sentinel_cloakbrowser(browser_proxy):
    """Extract sentinel tokens using CloakBrowser."""
    try:
        from cloakbrowser import launch
    except ImportError:
        print("[Error] pip install cloakbrowser")
        return None

    browser = launch(headless=True, humanize=True, proxy=browser_proxy)
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800}, locale="en-US", timezone_id="America/New_York")
    page = ctx.new_page()

    # Use create-account page (lighter, fewer redirects)
    auth_base = CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com")
    page_url = f"{auth_base}/create-account"

    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=120000)
    except Exception as e:
        err_msg = str(e)
        if "ERR_PROXY" in err_msg or "ERR_TUNNEL" in err_msg or "ERR_CONNECTION" in err_msg:
            print(f"  [Error] Proxy connection failed: {browser_proxy}")
            print(f"  [Error] Please check if your proxy (Clash/V2Ray etc.) is running on the correct port.")
            browser.close(); return None
        try: page.goto(page_url, wait_until="commit", timeout=120000)
        except Exception as e2:
            print(f"  [Error] Page navigation failed: {e2}"); browser.close(); return None

    if "error" in page.url:
        print(f"  [Error] Auth page returned error: {page.url[:200]}")
        browser.close(); return None

    # Wait for Cloudflare challenge to resolve (title changes from "Just a moment..." or empty)
    cf_deadline = time.time() + 180
    cf_waited = 0
    while time.time() < cf_deadline:
        try:
            title = page.title()
        except Exception:
            time.sleep(1); continue
        if title and "just a moment" not in title.lower():
            if cf_waited > 5:
                print(f"  Cloudflare challenge resolved after {cf_waited}s")
            break
        if cf_waited > 0 and cf_waited % 30 == 0:
            print(f"  Waiting for Cloudflare challenge... ({cf_waited}s)")
        cf_waited += 1
        time.sleep(1)
    else:
        print("  [Error] Cloudflare challenge did not resolve in 180s")
        browser.close(); return None

    # Now wait for SentinelSDK to load (CF challenge can take 10s to 2+ minutes)
    # Use page.evaluate() instead of wait_for_function to avoid CSP unsafe-eval violations
    sdk_deadline = time.time() + 180
    sdk_loaded = False
    while time.time() < sdk_deadline:
        try:
            if page.evaluate("() => typeof window.SentinelSDK !== 'undefined'"):
                sdk_loaded = True; break
        except Exception:
            pass
        time.sleep(1)
    if not sdk_loaded:
        print("  SentinelSDK not loaded after 180s! Check proxy connectivity to auth.openai.com")
        browser.close(); return None
    print("  SentinelSDK loaded")

    result = _collect_sentinel_tokens(page, ctx)
    browser.close()
    return result


def _collect_sentinel_tokens(page, ctx):
    """Call SentinelSDK.init() and extract tokens from the loaded page."""
    page.evaluate("() => SentinelSDK.init()"); time.sleep(0.5)
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

    result = {
        "sentinel_token": sentinel_token,
        "sentinel_so_token": sentinel_so,
        "cookie_str": cookie_str,
        "oai_did": did,
    }
    _save_sentinel_cache(result)
    return result

    page.evaluate("() => SentinelSDK.init()"); time.sleep(0.5)
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


def _json_or_raw(response, limit=500):
    try:
        return response.json()
    except Exception:
        return {"_raw": response.text[:limit]}


def _absolute_url(base_url, url):
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return base_url.rstrip("/") + "/" + url.lstrip("/")


def _is_existing_login_redirect(url):
    parsed = urlparse(url or "")
    path = (parsed.path or url or "").lower()
    return path in {"/log-in", "/login"} or path.endswith("/log-in") or path.endswith("/login")


def _follow_continue_url(session, url, base_headers, referer="", label="continue"):
    if not url:
        return None
    full_url = _absolute_url(CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com"), url)
    headers = {**base_headers, "Accept": "text/html,application/xhtml+xml"}
    if referer:
        headers["Referer"] = referer
    r = request_with_retry(session, "get", full_url, label=label,
        headers=headers, impersonate="chrome")
    print(f"  {label}: {r.status_code} {r.url}")
    return r


def _validate_email_otp(session, auth_base, base_headers, code):
    endpoints = CFG.get("email_registration", {}).get("otp_validate_endpoints") or [
        "/api/accounts/email-otp/validate",
        "/api/accounts/email-verification/validate",
        "/api/accounts/email-verification/verify",
        "/api/accounts/verify-email",
    ]
    payloads = (
        {"code": code},
        {"otp": code},
        {"verification_code": code},
    )
    last_error = {}
    for endpoint in endpoints:
        url = _absolute_url(auth_base, endpoint)
        for payload in payloads:
            r = request_with_retry(session, "post", url, label=f"Email OTP validate {endpoint}",
                json=payload,
                headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/verify-email"},
                impersonate="chrome")
            body = _json_or_raw(r)
            if r.status_code == 200:
                print(f"  Email OTP validate: {endpoint} {r.status_code}")
                return True, body
            if r.status_code not in (404, 405):
                last_error = {"endpoint": endpoint, "status": r.status_code, "body": body}
                print(f"  Email OTP validate failed: {endpoint} {r.status_code} {json.dumps(body, ensure_ascii=False)[:200]}")
                break
            last_error = {"endpoint": endpoint, "status": r.status_code, "body": body}
    return False, last_error


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
            impersonate="chrome")
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


def _generate_paypal_link(access_token, proxy=None):
    try:
        from .gen_pp_link import generate_pp_link
    except Exception as e:
        return {"ok": False, "error": f"load_gen_pp_link_failed: {e}"}
    try:
        return generate_pp_link(access_token, proxy=proxy)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ==========================================
# Core Email Registration Flow
# ==========================================
def run_email(proxy=None, password=None, sentinel_data=None, mailbox=None, paypal_link=True):
    """Register a ChatGPT account via mailbox OTP, then create a PayPal payment link."""
    _tl().clear()

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
        sentinel_data = _extract_sentinel(proxy=proxy)
        _tock()
    if not sentinel_data or not sentinel_data.get("sentinel_token"):
        return _failure_result("sentinel_extract_failed", email=getattr(mailbox, "email", ""), mailbox=mailbox)

    # Step 1: Generate credentials
    password = password or _generate_password()
    first, last = _random_name()
    full_name = f"{first} {last}"
    birthdate = _random_birthdate()
    username = mailbox.email

    # Each registration needs its own device_id to avoid auth session conflicts in batch mode
    did = str(uuid.uuid4())
    session_logging_id = str(uuid.uuid4()).replace("-", "")

    # Use original sentinel tokens — do NOT patch the embedded id, as it breaks the HMAC signature
    _sentinel_token = sentinel_data["sentinel_token"]
    _sentinel_so_token = sentinel_data["sentinel_so_token"]
    print(f"[*] Username: {username}  Password: {password}  Name: {full_name}  Birth: {birthdate}")

    # Init curl_cffi session
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
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

        signin_url = (
            f"{chat_base}/api/auth/signin/openai"
            f"?prompt=login&ext-oai-did={did}"
            f"&auth_session_logging_id={session_logging_id}"
            f"&screen_hint=signup"
            f"&login_hint={quote(username, safe='')}"
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
            return _failure_result("email_already_registered_or_login_redirect", email=username, mailbox=mailbox, password=password)

        # Step 4: Register with username + password
        _tick("3-User register (email+password)")
        r = request_with_retry(session, "post", f"{auth_base}/api/accounts/user/register", label="User register",
            json={"password": password, "username": username},
            headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/create-account/password",
                    "openai-sentinel-token": _sentinel_token},
            impersonate="chrome")
        _tock()
    except Exception as e:
        _safe_tock()
        print(f"  Transport error: {e}")
        return _failure_result(f"transport_error: {e}", email=username, mailbox=mailbox, password=password)

    reg_data = {}
    try: reg_data = r.json()
    except: reg_data = {"_raw": r.text[:300]}
    print(f"  Status: {r.status_code}")
    print(f"  Response: {json.dumps(reg_data, ensure_ascii=False)[:300]}")

    if r.status_code != 200:
        err_code = reg_data.get("error", {}).get("code", "")
        err_msg = reg_data.get("error", {}).get("message", str(reg_data))
        if err_code == "invalid_auth_step" and "email-verification" in redirect_path:
            print(f"  Account already in email-verification flow, resuming OTP step...")
        else:
            return _failure_result(f"user_register: {err_msg}", email=username, mailbox=mailbox, password=password)

    _snapshot_mailbox_message(mailbox, proxy=proxy)

    # Step 4: Trigger email OTP send
    _tick("4-Trigger email OTP")
    continue_url = reg_data.get("continue_url", "")
    otp_send_started = int(time.time())
    try:
        _follow_continue_url(session, continue_url, base_headers, referer=f"{auth_base}/create-account/password", label="Email OTP send")
        _tock()
    except Exception as e:
        _safe_tock()
        print(f"  Transport error: {e}")
        return _failure_result(f"email_otp_send_transport: {e}", email=username, mailbox=mailbox, password=password)

    # Step 5: Get email OTP
    _tick("5-Get email OTP")
    email_cfg = CFG.get("email_registration", {})
    code = _poll_email_otp(
        mailbox,
        subject_keyword=email_cfg.get("otp_subject_keyword", ""),
        timeout=int(email_cfg.get("otp_timeout", 300)),
        issued_after_unix=otp_send_started if getattr(mailbox, "provider", "") == "cfworker" else int(time.time()) - 30,
        proxy=proxy,
    )
    _tock()
    if not code:
        return _failure_result("email_otp_poll_timeout", email=username, mailbox=mailbox, password=password)

    # Step 6: Validate email OTP
    _tick("6-Validate email OTP")
    try:
        otp_ok, otp_data = _validate_email_otp(session, auth_base, base_headers, code)
        _tock()
    except Exception as e:
        _safe_tock()
        print(f"  Transport error: {e}")
        return _failure_result(f"email_otp_validate_transport: {e}", email=username, mailbox=mailbox, password=password)
    if not otp_ok:
        return _failure_result(f"email_otp_validate: {json.dumps(otp_data, ensure_ascii=False)[:300]}", email=username, mailbox=mailbox, password=password)
    try:
        _follow_continue_url(session, otp_data.get("continue_url", ""), base_headers, referer=f"{auth_base}/verify-email", label="Email OTP continue")
    except Exception as e:
        print(f"  Email OTP continue transport warning: {e}")

    # Step 7: Create account
    _tick("7-Create account")
    try:
        r = request_with_retry(session, "post", f"{auth_base}/api/accounts/create_account", label="Create account",
            json={"name": full_name, "birthdate": birthdate},
            headers={**base_headers, "Origin": auth_base, "Referer": f"{auth_base}/about-you",
                    "openai-sentinel-token": _sentinel_token,
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
    try:
        _follow_continue_url(session, create_data.get("continue_url", ""), base_headers, referer=f"{auth_base}/about-you", label="Create account continue")
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

    paypal = {}
    if r.status_code == 200 and access_token and paypal_link:
        _tick("9-Generate PayPal link")
        paypal = _generate_paypal_link(access_token, proxy=proxy)
        print(f"  PayPal link: {'ok' if paypal.get('ok') else paypal.get('error', 'failed')}")
        _tock()

    result = {
        "success": r.status_code == 200 and bool(access_token),
        "email": username,
        "phone": "",
        "password": password,
        "name": full_name,
        "birthdate": birthdate,
        "response": {
            "register": reg_data,
            "email_otp": otp_data,
            "create_account": create_data,
            "auth_session": auth_body,
        },
        "auth_session": auth_body,
        "access_token": access_token or "",
        "cookie_header": auth_session.get("cookie_header", ""),
        "paypal": paypal,
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
    )


def _unique_mailboxes(mailboxes):
    if not mailboxes:
        return []
    unique = []
    seen = set()
    for mailbox in mailboxes:
        email = str(getattr(mailbox, "email", "") or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        unique.append(mailbox)
    return unique


def run_batch(count=1, proxy=None, mailboxes=None, paypal_link=True, workers=4):
    mailboxes = _unique_mailboxes(mailboxes)
    if mailboxes and int(count or 1) > len(mailboxes):
        print(f"[!] Requested {count} account(s), but only {len(mailboxes)} unique mailbox(es) are available; capping batch size.")
        count = len(mailboxes)
    results = []
    print(f"\n{'=' * 60}")
    print(f"  ChatGPT Email Batch Registration - {count} accounts")
    print(f"{'=' * 60}\n")

    def _run_one(i):
        print(f"\n{'#' * 40}")
        print(f"  Account {i + 1}/{count}")
        print(f"{'#' * 40}")
        try:
            mailbox = mailboxes[i] if mailboxes else None
            return i, run_email(proxy=proxy, mailbox=mailbox, paypal_link=paypal_link)
        except Exception as e:
            import traceback; traceback.print_exc()
            return i, {"success": False, "error": str(e)}

    workers = max(1, min(int(workers or 1), 4, int(count or 1)))
    if workers <= 1:
        for i in range(count):
            _, result = _run_one(i)
            results.append(result)
        return results

    ordered = [None] * count
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_run_one, i) for i in range(count)]
        for future in as_completed(futures):
            i, result = future.result()
            ordered[i] = result
    results.extend(result for result in ordered if result is not None)
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
    paypal_status = data.get("paypal_status") or paypal.get("status") or ("link_ready" if paypal.get("url") else "")
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
        "refresh_token": refresh_token or "",
        "device_id": data.get("device_id") or response.get("device_id") or "",
        "cookie_header": data.get("cookie_header") or response.get("cookie_header") or "",
        "auth_session": auth_session,
        "paypal": paypal,
        "paypal_status": paypal_status,
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

