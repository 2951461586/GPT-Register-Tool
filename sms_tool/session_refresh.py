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
    headless=False,
    browser=False,
    proxy=None,
    *,
    persist=True,
    automated_login=False,
):
    """Refresh ChatGPT session. Protocol mode is the default; browser mode is opt-in."""
    data, json_path = _load_seed_session(email=email, session_file=session_file)
    target_email = (email or data.get("email") or "").strip().lower()
    timeout = max(30, int(timeout or 300))

    if browser:
        return _refresh_session_browser(
            data,
            json_path,
            target_email,
            timeout,
            headless,
            proxy=proxy,
            persist=persist,
            automated_login=automated_login,
        )
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


def _refresh_session_browser(
    data,
    json_path,
    target_email,
    timeout,
    headless,
    proxy=None,
    persist=True,
    automated_login=False,
):
    try:
        from cloakbrowser import launch
    except ImportError:
        return {"ok": False, "mode": "browser", "error": "cloakbrowser_not_installed: pip install cloakbrowser"}

    launch_kwargs = {"headless": bool(headless), "humanize": True}
    if proxy:
        launch_kwargs["proxy"] = proxy
    try:
        browser = launch(**launch_kwargs)
    except Exception as exc:
        return {"ok": False, "email": target_email, "mode": "browser", "error": _safe_session_error(f"browser_launch_failed:{exc}")}
    try:
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        _import_cookie_header(ctx, data.get("cookie_header", ""))
        page = ctx.new_page()
        page.goto(CFG["chatgpt"].get("chat_base_url", "https://chatgpt.com"), wait_until="domcontentloaded", timeout=120000)
        auth_session = _request_auth_session(ctx)
        if not _session_token(auth_session, "accessToken", "access_token"):
            if automated_login:
                login = _complete_browser_email_login(
                    page,
                    data,
                    target_email,
                    timeout=timeout,
                    proxy=proxy,
                )
                if not login.get("ok"):
                    return {
                        "ok": False,
                        "email": target_email,
                        "mode": "browser",
                        "error": login.get("error") or "browser_email_login_failed",
                    }
            else:
                print("[*] CloakBrowser opened. Complete any required login confirmation manually.")
        auth_session = _poll_auth_session(ctx, timeout, page=page)
        cookies = ctx.cookies()
    except Exception as e:
        return {"ok": False, "email": target_email, "mode": "browser", "error": _safe_session_error(e)}
    finally:
        try:
            browser.close()
        except Exception:
            pass

    access_token = _session_token(auth_session, "accessToken", "access_token")
    oauth_refresh_token = _session_token(auth_session, "refreshToken", "refresh_token")
    if not access_token:
        return {"ok": False, "email": target_email, "mode": "browser", "error": "auth_session_missing_access_token"}
    authenticated_email = _auth_session_email(auth_session)
    if target_email and not authenticated_email:
        return {"ok": False, "email": target_email, "mode": "browser", "error": "auth_session_missing_email"}
    if target_email and authenticated_email.lower() != target_email:
        return {"ok": False, "email": target_email, "mode": "browser", "error": "auth_session_email_mismatch"}

    refreshed = _merge_refreshed_session(
        data=data,
        target_email=target_email,
        auth_session=auth_session,
        access_token=access_token,
        oauth_refresh_token=oauth_refresh_token,
        cookie_header=_cookie_header(cookies),
    )
    return _finish_session_refresh(refreshed, json_path, "browser", persist)


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


def _complete_browser_email_login(page, data, target_email, timeout, proxy=None):
    if not target_email:
        return {"ok": False, "error": "browser_login_missing_email"}
    try:
        from .codex_oauth import LOGIN_EMAIL_OTP_SUBJECT_KEYWORD, _mailbox_from_data
        from .mailbox import MailboxTokenExpiredError, _poll_email_otp
    except Exception as exc:
        return {"ok": False, "error": f"browser_mailbox_unavailable:{exc}"}

    mailbox = _mailbox_from_data(data)
    if mailbox is None:
        return {"ok": False, "error": "browser_login_missing_mailbox"}

    chat_base = CFG["chatgpt"].get("chat_base_url", "https://chatgpt.com").rstrip("/")
    try:
        page.goto(f"{chat_base}/auth/login", wait_until="domcontentloaded", timeout=120000)
        if _page_has_auth_session(page):
            return {"ok": True}
        email_input = _wait_for_visible_locator(
            page,
            (
                'input[type="email"]',
                'input[name="email"]',
                'input[name="username"]',
                'input[autocomplete="email"]',
            ),
            timeout=min(30, timeout),
        )
        if email_input is None:
            if _page_has_auth_session(page):
                return {"ok": True}
            error = _browser_login_page_error(page)
            return {"ok": False, "error": error or "browser_login_email_form_not_found"}

        email_input.fill(target_email)
        issued_after = int(time.time()) - 10
        if not _click_visible_locator(
            page,
            (
                'button[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("Log in")',
            ),
        ):
            email_input.press("Enter")

        otp_selectors = (
            'input[autocomplete="one-time-code"]',
            'input[name="code"]',
            'input[inputmode="numeric"]',
            'input[data-testid="otp-input"]',
        )
        otp_input = _wait_for_visible_locator(page, otp_selectors, timeout=min(15, timeout))
        if otp_input is None:
            _click_visible_locator(
                page,
                (
                    'button:has-text("Continue with code")',
                    'button:has-text("Email me a code")',
                    'button:has-text("Send code")',
                    'button:has-text("Use a code")',
                    'button:has-text("Log in with a code")',
                    'a:has-text("Continue with code")',
                    'a:has-text("Use a code")',
                ),
            )
            otp_input = _wait_for_visible_locator(page, otp_selectors, timeout=min(20, timeout))
        if otp_input is None:
            if _page_has_auth_session(page):
                return {"ok": True}
            error = _browser_login_page_error(page)
            return {"ok": False, "error": error or "browser_login_otp_form_not_found"}

        try:
            code = _poll_email_otp(
                mailbox,
                subject_keyword=LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
                timeout=max(30, min(int(timeout or 180), 300)),
                issued_after_unix=issued_after,
                proxy=proxy,
            )
        except MailboxTokenExpiredError:
            return {"ok": False, "error": "mailbox_token_expired"}
        if not code:
            return {"ok": False, "error": "browser_email_otp_poll_timeout"}

        _fill_browser_otp(page, str(code), otp_input)
        if not _click_visible_locator(
            page,
            (
                'button[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("Verify")',
            ),
        ):
            otp_input.press("Enter")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": _safe_session_error(f"browser_email_login_failed:{exc}")}


def _wait_for_visible_locator(page, selectors, timeout=20):
    deadline = time.time() + max(1, int(timeout or 1))
    while time.time() < deadline:
        locator = _first_visible_locator(page, selectors)
        if locator is not None:
            return locator
        time.sleep(0.25)
    return None


def _first_visible_locator(page, selectors):
    for selector in selectors:
        try:
            matches = page.locator(selector)
            for index in range(min(matches.count(), 8)):
                candidate = matches.nth(index)
                if candidate.is_visible():
                    return candidate
        except Exception:
            continue
    return None


def _click_visible_locator(page, selectors):
    locator = _first_visible_locator(page, selectors)
    if locator is None:
        return False
    locator.click()
    return True


def _fill_browser_otp(page, code, fallback_locator):
    digits = [char for char in str(code or "") if char.isdigit()]
    try:
        inputs = page.locator('input[inputmode="numeric"]')
        visible = [inputs.nth(index) for index in range(min(inputs.count(), 8)) if inputs.nth(index).is_visible()]
        if len(visible) > 1 and len(visible) >= len(digits):
            for locator, digit in zip(visible, digits):
                locator.fill(digit)
            return
    except Exception:
        pass
    fallback_locator.fill("".join(digits) or str(code or ""))


def _browser_login_page_error(page):
    current_url = str(getattr(page, "url", "") or "").lower()
    body_text = ""
    try:
        body_text = str(page.locator("body").inner_text(timeout=1000) or "").lower()
    except Exception:
        pass
    combined = f"{current_url}\n{body_text}"
    if "add-phone" in combined or "phone-verification" in combined or "verify your phone" in combined:
        return "browser_login_phone_verification_required"
    if "deactivated" in combined or "account has been deleted" in combined or "account was deleted" in combined:
        return "account_deactivated"
    if "captcha" in combined or "challenge" in current_url or "checking your browser" in body_text:
        return "browser_login_challenge_required"
    return ""


def _page_has_auth_session(page):
    try:
        body = _request_auth_session(page.context)
        return bool(_session_token(body, "accessToken", "access_token"))
    except Exception:
        return False


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


def _safe_session_error(value):
    text = str(value or "")
    text = re.sub(r"((?:https?|socks5h?)://)[^@\s/]+@", r"\1[REDACTED]@", text, flags=re.I)
    text = re.sub(r"\brt_[A-Za-z0-9._~-]+", "rt_[REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED_JWT]", text)
    return text[:1000]


def _poll_auth_session(ctx, timeout, page=None):
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        if page is not None:
            page_error = _browser_login_page_error(page)
            if page_error:
                raise RuntimeError(page_error)
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
