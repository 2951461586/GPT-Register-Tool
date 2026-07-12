import json
from urllib.parse import quote, urlencode, urlparse

from .config import CFG
from .auth_headers import openai_auth_headers
from .http_client import request_with_retry

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
    if not path:
        return False
    # Normalize: strip trailing slashes
    path = path.rstrip("/")
    return path in {"/log-in", "/login"} or path.startswith("/log-in/") or path.startswith("/login/")


def _is_chatgpt_auth_login_landing(url):
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    return host.endswith("chatgpt.com") and path in {"/auth/login", "/auth/log-in"}


def _is_signup_password_step(url):
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    if not host.endswith("auth.openai.com"):
        return False
    return path.endswith("/create-account/password") or path.endswith("/create-account")


def _is_email_verification_step(url):
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").rstrip("/").lower()
    if not host.endswith("auth.openai.com"):
        return False
    return path.endswith("/email-verification") or "email-otp" in path


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


def _with_query_param(url, key, value):
    if not value or f"{key}=" in (url or ""):
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
    return (
        {"name": "signup_screen_hint", "screen_hint": "signup", "prompt": ""},
        {"name": "signup_prompt_signup", "screen_hint": "signup", "prompt": "signup"},
        {"name": "signup_legacy_prompt_login", "screen_hint": "signup", "prompt": "login"},
    )


def _passwordless_signin_attempts():
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


def _follow_continue_url(session, url, base_headers, referer="", label="continue"):
    if not url:
        return None
    full_url = _absolute_url(CFG["chatgpt"].get("auth_base_url", "https://auth.openai.com"), url)
    headers = {**base_headers, "Accept": "text/html,application/xhtml+xml"}
    if referer:
        headers["Referer"] = referer
    response = request_with_retry(
        session,
        "get",
        full_url,
        label=label,
        headers=headers,
        impersonate="chrome",
    )
    print(f"  {label}: {response.status_code} {response.url}")
    return response


def _continue_signup_username(session, username, did, auth_base, base_headers, current_url, sentinel_token="", sentinel_so_token=""):
    """Ensure auth.openai.com has an active signup state before user/register.

    Recent auth flows may bounce the initial NextAuth authorize request back to
    chatgpt.com/auth/login.  Posting user/register from that landing page always
    returns invalid_state, so advance the auth session with the username first.
    """
    if _is_signup_password_step(current_url) or _is_email_verification_step(current_url):
        return {"ok": True, "url": current_url, "skipped": True}

    referer = current_url if str(current_url or "").startswith(auth_base) else f"{auth_base}/create-account"
    headers = {
        **base_headers,
        **openai_auth_headers(
            did,
            referer=referer,
            origin=auth_base,
            sentinel_token=sentinel_token,
            sentinel_so_token=sentinel_so_token,
            extra={"Content-Type": "application/json"},
        ),
    }

    response = request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/authorize/continue",
        label="Signup username continue",
        json={"username": {"value": username, "kind": "email"}},
        headers=headers,
        impersonate="chrome",
    )
    body = _json_or_raw(response, limit=1000)
    next_url = _response_next_url(response, auth_base)
    print(f"  Signup username continue: {response.status_code}" + (f" {next_url}" if next_url else ""))
    if response.status_code != 200:
        return {"ok": False, "status": response.status_code, "body": body, "url": next_url}

    final_url = next_url
    if next_url and not next_url.endswith("/api/accounts/authorize/continue"):
        try:
            follow = _follow_continue_url(
                session,
                next_url,
                base_headers,
                referer=referer,
                label="Signup username continue follow",
            )
            final_url = str(getattr(follow, "url", "") or next_url)
        except Exception as exc:
            return {"ok": False, "status": response.status_code, "body": body, "url": next_url, "error": f"continue_follow_failed:{exc}"}
    return {"ok": True, "status": response.status_code, "body": body, "url": final_url}


def _prime_email_verification_page(session, auth_base, base_headers, current_url):
    """Load /email-verification once before posting OTP resend/send.

    Browser HAR shows the 302 from /api/accounts/authorize is followed by a
    real navigation to /email-verification, and then the page issues
    /api/accounts/email-otp/resend.  If protocol mode stops at the 302 only,
    the auth session can be present but not fully advanced for the resend
    endpoint, which commonly returns HTTP 400.
    """
    if not _is_email_verification_step(current_url):
        return {"ok": True, "url": current_url, "skipped": True}
    url = _absolute_url(auth_base, current_url)
    try:
        response = request_with_retry(
            session,
            "get",
            url,
            label="Email verification page",
            headers={
                **base_headers,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": url,
            },
            allow_redirects=False,
            impersonate="chrome",
        )
        next_url = _response_next_url(response, auth_base)
        if response.status_code in (200, 204, 304) or _is_email_verification_step(next_url):
            print(f"  Email verification page: {response.status_code}")
            return {"ok": True, "status": response.status_code, "url": url}
        print(f"  Email verification page: {response.status_code} {next_url}")
        return {"ok": False, "status": response.status_code, "url": next_url}
    except Exception as exc:
        print(f"  Email verification page warning: {exc}")
        return {"ok": False, "error": str(exc), "url": current_url}


def _prepare_signup_auth_state(
    session,
    username,
    did,
    session_logging_id,
    auth_base,
    chat_base,
    base_headers,
    csrf_token,
    sentinel_token="",
    sentinel_so_token="",
    attempts=None,
):
    signin_payload = {
        "csrfToken": csrf_token,
        "callbackUrl": f"{chat_base}/",
        "json": "true",
    }
    last_state = {"ok": False, "error": "signup_auth_not_started"}

    for attempt in (attempts or _signup_signin_attempts()):
        name = attempt["name"]
        signin_url = _openai_signin_url(
            chat_base,
            did,
            session_logging_id,
            username,
            screen_hint=attempt.get("screen_hint", ""),
            prompt=attempt.get("prompt", ""),
        )
        signin_resp = request_with_retry(
            session,
            "post",
            signin_url,
            label=f"Auth signin {name}",
            data=urlencode(signin_payload),
            headers={**base_headers, "Content-Type": "application/x-www-form-urlencoded",
                     "Origin": chat_base, "Referer": f"{chat_base}/"},
            impersonate="chrome",
        )
        signin_body = _json_or_raw(signin_resp, limit=1000)
        auth_session_url = signin_body.get("url") or signin_resp.headers.get("location") or signin_resp.url
        auth_session_url = _with_query_param(auth_session_url, "device_id", did)
        if not auth_session_url:
            last_state = {"ok": False, "attempt": name, "error": "missing_auth_session_url", "body": signin_body}
            continue

        authorize_resp = request_with_retry(
            session,
            "get",
            auth_session_url,
            label=f"Auth authorize {name}",
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": f"{chat_base}/"},
            allow_redirects=False,
            impersonate="chrome",
        )
        location = (
            getattr(authorize_resp, "headers", {}).get("location")
            or getattr(authorize_resp, "headers", {}).get("Location")
            or ""
        )
        current_url = _absolute_url(auth_base, location) if location else str(authorize_resp.url or "")
        redirect_path = current_url.split("auth.openai.com")[-1]
        print(f"  Redirect[{name}]: {authorize_resp.status_code} {redirect_path}")

        if _is_existing_login_redirect(current_url):
            return {"ok": False, "attempt": name, "existing_login_redirect": True, "url": current_url}

        if _is_chatgpt_auth_login_landing(current_url):
            last_state = {"ok": False, "attempt": name, "error": "redirected_to_chatgpt_login", "url": current_url}
            continue

        if _is_signup_password_step(current_url) or _is_email_verification_step(current_url):
            return {"ok": True, "attempt": name, "status": authorize_resp.status_code, "url": current_url, "skipped": True}

        signup_state = _continue_signup_username(
            session,
            username,
            did,
            auth_base,
            base_headers,
            current_url,
            sentinel_token=sentinel_token,
            sentinel_so_token=sentinel_so_token,
        )
        signup_state["attempt"] = name
        if signup_state.get("ok") and not _is_chatgpt_auth_login_landing(signup_state.get("url", "")):
            return signup_state

        last_state = signup_state
        if signup_state.get("status") == 409 and _invalid_state_auth_response(signup_state.get("body")):
            continue
        if _is_chatgpt_auth_login_landing(signup_state.get("url", "")):
            continue
        return signup_state

    return last_state

