import json
import time
from urllib.parse import parse_qs, quote, urlencode, urlparse

from .account_creation import _validate_email_otp
from .auth_headers import auth_impersonate, openai_auth_headers
from .config import current_config_data
from .http_client import request_with_retry
from .http_utils import _absolute_url, _follow_continue_url, _json_or_raw
from .mailbox import _poll_email_otp
from .phone_proxy import redact_proxy_url


_PASSKEY_CLIENT_CAPABILITIES = "11111"
_CC_CAPS = "login_methods"


def _is_existing_login_redirect(url):
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if host and host != "auth.openai.com" and not host.endswith(".auth.openai.com"):
        return False
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


def _ensure_authorize_context(url, did, session_logging_id, login_hint, *, screen_hint="", prompt=""):
    parsed = urlparse(str(url or ""))
    if not parsed.netloc.endswith("auth.openai.com"):
        return str(url or "")
    values = parse_qs(parsed.query, keep_blank_values=True)
    required = {
        "device_id": did,
        "ext-oai-did": did,
        "auth_session_logging_id": session_logging_id,
        "ext-passkey-client-capabilities": _PASSKEY_CLIENT_CAPABILITIES,
        "ccaps": _CC_CAPS,
        "login_hint": login_hint,
    }
    if screen_hint:
        required["screen_hint"] = screen_hint
    if prompt:
        required["prompt"] = prompt
    for key, value in required.items():
        if value and not values.get(key):
            values[key] = [str(value)]
    return parsed._replace(query=urlencode(values, doseq=True)).geturl()


def _openai_signin_url(chat_base, did, session_logging_id, login_hint, *, screen_hint="", prompt=""):
    params = {
        "ext-oai-did": did,
        "device_id": did,
        "auth_session_logging_id": session_logging_id,
        "ext-passkey-client-capabilities": _PASSKEY_CLIENT_CAPABILITIES,
        "ccaps": _CC_CAPS,
        "login_hint": login_hint,
    }
    if screen_hint:
        params["screen_hint"] = screen_hint
    if prompt:
        params["prompt"] = prompt
    return f"{chat_base}/api/auth/signin/openai?{urlencode(params)}"


def _cookie_presence(session):
    names = set()
    try:
        names = {
            str(getattr(cookie, "name", cookie) or "")
            for cookie in session.cookies
        }
    except Exception:
        try:
            names = set(session.cookies.get_dict())
        except Exception:
            names = set()
    names = {name for name in names if name}
    return {
        "oai_did": any(name.lower() == "oai-did" for name in names),
        "oai_login_csrf": any("oai-login-csrf" in name.lower() for name in names),
        "login_session": any("login_session" in name.lower() for name in names),
        "client_auth_session": any("client_auth_session" in name.lower() for name in names),
        "nextauth_state": any("next-auth.state" in name.lower() for name in names),
        "cookie_count": len(names),
    }


def _protocol_diagnostic(*, response=None, final_url="", session=None, sentinel_source="", sentinel_flow="", proxy="", **extra):
    status = int(getattr(response, "status_code", 0) or 0) if response is not None else 0
    raw_url = str(final_url or getattr(response, "url", "") or "")
    parsed_url = urlparse(raw_url)
    safe_url = (
        f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
        if parsed_url.scheme and parsed_url.netloc
        else str(parsed_url.path or "")
    )
    return {
        "final_url": safe_url,
        "cookie_presence": _cookie_presence(session) if session is not None else {},
        "sentinel_source": str(sentinel_source or ""),
        "sentinel_flow": str(sentinel_flow or ""),
        "http_status": status,
        "proxy": redact_proxy_url(proxy),
        **extra,
    }


def _print_protocol_diagnostic(stage, diagnostic):
    safe = dict(diagnostic or {})
    url = urlparse(str(safe.get("final_url") or ""))
    safe["final_url"] = f"{url.scheme}://{url.netloc}{url.path}" if url.scheme and url.netloc else str(url.path or "")
    print(f"  Protocol diagnostic[{stage}]: {json.dumps(safe, ensure_ascii=False, sort_keys=True)}")


def _authorize_continue_sentinel(
    session,
    did,
    proxy="",
    sentinel_token="",
    sentinel_so_token="",
):
    """Fetch a fresh, same-flow Sentinel challenge for API continue calls."""
    from .sentinel import issue_sentinel_flow

    issued = issue_sentinel_flow(
        flow="authorize_continue",
        device_id=did,
        session=session,
        proxy=proxy,
        supplied_data={
            "sentinel_authorize_continue_token": sentinel_token,
            "sentinel_authorize_continue_so_token": sentinel_so_token,
        },
    )
    data = {
        "sentinel_authorize_continue_token": issued.token,
        "sentinel_authorize_continue_so_token": issued.so_token,
        "sentinel_source": "node_sdk_runner",
        "oai_did": issued.device_id,
    }
    return data, issued.token, issued.so_token


def _signup_signin_attempts():
    return (
        {"name": "signup_screen_hint", "screen_hint": "signup", "prompt": ""},
        {"name": "signup_prompt_signup", "screen_hint": "signup", "prompt": "signup"},
        {"name": "signup_legacy_prompt_login", "screen_hint": "signup", "prompt": "login"},
    )


def _passwordless_signin_attempts():
    return (
        # Match the stable browser signup entry. The prompt is intentionally
        # omitted; prompt=login selects the existing-login password page for
        # unregistered mailboxes on some exits.
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


def _continue_signup_username(session, username, did, auth_base, base_headers, current_url, sentinel_token="", sentinel_so_token="", proxy=""):
    """Ensure auth.openai.com has an active signup state before user/register.

    Recent auth flows may bounce the initial NextAuth authorize request back to
    chatgpt.com/auth/login.  Posting user/register from that landing page always
    returns invalid_state, so advance the auth session with the username first.
    """
    if _is_signup_password_step(current_url) or _is_email_verification_step(current_url):
        return {"ok": True, "url": current_url, "skipped": True}

    fresh_data, fresh_token, fresh_so = _authorize_continue_sentinel(
        session,
        did,
        proxy=proxy,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
    )
    referer = current_url if str(current_url or "").startswith(auth_base) else f"{auth_base}/create-account"
    headers = {
        **base_headers,
        **openai_auth_headers(
            did,
            referer=referer,
            origin=auth_base,
            sentinel_token=fresh_token,
            sentinel_so_token=fresh_so,
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
        impersonate=auth_impersonate(),
    )
    body = _json_or_raw(response, limit=1000)
    next_url = _response_next_url(response, auth_base)
    print(f"  Signup username continue: {response.status_code}" + (f" {next_url}" if next_url else ""))
    diagnostic = _protocol_diagnostic(response=response, final_url=next_url, session=session,
                                      sentinel_source=fresh_data.get("sentinel_source", ""),
                                      sentinel_flow="authorize_continue", proxy=proxy)
    _print_protocol_diagnostic("authorize_continue", diagnostic)
    if response.status_code != 200:
        circuit = getattr(session, "_openai_registration_circuit", {})
        retry_after = circuit.get("retry_after", 0) if isinstance(circuit, dict) else 0
        return {
            "ok": False,
            "status": response.status_code,
            "body": body,
            "url": next_url,
            "retry_after_seconds": retry_after,
        }

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
    diagnostic["final_url"] = final_url
    return {"ok": True, "status": response.status_code, "body": body, "url": final_url,
            "diagnostic": diagnostic}


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
            impersonate=auth_impersonate(),
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
    authorize_sentinel_token="",
    sentinel_so_token="",
    proxy="",
    passwordless_web=False,
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
            impersonate=auth_impersonate(),
        )
        signin_body = _json_or_raw(signin_resp, limit=1000)
        auth_session_url = signin_body.get("url") or signin_resp.headers.get("location") or signin_resp.url
        auth_session_url = _ensure_authorize_context(
            auth_session_url,
            did,
            session_logging_id,
            username,
            screen_hint=attempt.get("screen_hint", ""),
            prompt=attempt.get("prompt", ""),
        )
        if not auth_session_url:
            last_state = {"ok": False, "attempt": name, "error": "missing_auth_session_url", "body": signin_body}
            continue

        authorize_resp = request_with_retry(
            session,
            "get",
            auth_session_url,
            label=f"Auth authorize {name}",
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": f"{chat_base}/"},
            allow_redirects=True,
            impersonate=auth_impersonate(),
        )
        current_url = str(authorize_resp.url or "")
        location = (
            getattr(authorize_resp, "headers", {}).get("location")
            or getattr(authorize_resp, "headers", {}).get("Location")
            or ""
        )
        # Some HTTP adapters retain the authorize URL even after a redirect;
        # use Location as a compatibility fallback when no navigation occurred.
        if location and urlparse(current_url).path.rstrip("/") in {"", "/api/accounts/authorize"}:
            current_url = _absolute_url(auth_base, location)
        redirect_path = urlparse(current_url).path or "/"
        diagnostic = _protocol_diagnostic(response=authorize_resp, final_url=current_url, session=session,
                                          sentinel_source="", sentinel_flow="", proxy=proxy,
                                          signin_attempt=name)
        print(f"  Redirect[{name}]: {authorize_resp.status_code} {redirect_path} "
              f"cookies={diagnostic['cookie_presence']}")
        _print_protocol_diagnostic("authorize", diagnostic)

        login_redirect_seen = _is_existing_login_redirect(current_url)

        if _is_chatgpt_auth_login_landing(current_url):
            last_state = {"ok": False, "attempt": name, "error": "redirected_to_chatgpt_login", "url": current_url}
            continue

        if _is_signup_password_step(current_url) or _is_email_verification_step(current_url):
            return {"ok": True, "attempt": name, "status": authorize_resp.status_code, "url": current_url,
                    "skipped": True, "diagnostic": diagnostic}

        # Passwordless Web/HAR flow is complete after authorize navigation.
        # The browser sends the OTP from this state; do not POST authorize/continue.
        if passwordless_web:
            if _is_chatgpt_auth_login_landing(current_url):
                last_state = {"ok": False, "attempt": name, "status": authorize_resp.status_code,
                              "url": current_url, "error": "authorize_redirect_not_advanced",
                              "diagnostic": diagnostic}
                continue
            if _is_existing_login_redirect(current_url):
                # The server can explicitly disable passwordless signup for an
                # exit (the client_auth_session dump reports
                # passwordless_disabled=true) and route to /log-in/password.
                # This is no longer the passwordless Web path; use the legacy
                # username transition only for this explicit password fallback.
                signup_state = _continue_signup_username(
                    session, username, did, auth_base, base_headers, current_url,
                    sentinel_token=authorize_sentinel_token or sentinel_token,
                    sentinel_so_token=sentinel_so_token, proxy=proxy,
                )
                signup_state["attempt"] = name
                signup_state["password_fallback"] = True
                signup_state.setdefault("diagnostic", diagnostic)
                if signup_state.get("ok") and not _is_chatgpt_auth_login_landing(signup_state.get("url", "")):
                    return signup_state
                last_state = {**signup_state, "error": "authorize_login_page"}
                continue
            # The normal Web path receives the OTP from authorize and proceeds
            # via email verification without /authorize/continue.
            return {"ok": True, "attempt": name, "status": authorize_resp.status_code,
                    "url": current_url, "diagnostic": diagnostic}

        signup_state = _continue_signup_username(
            session,
            username,
            did,
            auth_base,
            base_headers,
            current_url,
            sentinel_token=authorize_sentinel_token or sentinel_token,
            sentinel_so_token=sentinel_so_token,
            proxy=proxy,
        )
        signup_state["attempt"] = name
        signup_state["login_redirect_seen"] = login_redirect_seen
        signup_state.setdefault("diagnostic", diagnostic)
        if login_redirect_seen and _is_existing_login_redirect(signup_state.get("url", "")):
            last_state = {
                **signup_state,
                "ok": False,
                "error": "login_redirect_not_advanced",
            }
            continue
        if signup_state.get("ok") and not _is_chatgpt_auth_login_landing(signup_state.get("url", "")):
            return signup_state

        last_state = signup_state
        if signup_state.get("status") == 409 and _invalid_state_auth_response(signup_state.get("body")):
            continue
        if _is_chatgpt_auth_login_landing(signup_state.get("url", "")):
            continue
        return signup_state

    return last_state


# ==========================================
# Existing-account login flow (email OTP + TOTP challenge)
# ==========================================
LOGIN_EMAIL_OTP_SUBJECT_KEYWORD = "login code"


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
    # authorize/continue already creates the existing-account OTP challenge.
    # Reuse that state first; passwordless/send-otp belongs to account creation
    # and invalidates the login challenge with a 409 invalid_state response.
    for endpoint in (
        "/api/accounts/email-otp/resend",
        "/api/accounts/email-otp/send",
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
    totp_secret="",
    otp_timeout=None,
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
    fresh_data, fresh_token, fresh_so = _authorize_continue_sentinel(session, did, proxy=proxy)
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
            sentinel_token=fresh_token,
            sentinel_so_token=fresh_so,
            extra={"Content-Type": "application/json"},
        ),
        impersonate=auth_impersonate(),
    )
    print(f"  Existing account continue: {continue_resp.status_code}")
    _print_protocol_diagnostic(
        "existing_authorize_continue",
        _protocol_diagnostic(
            response=continue_resp,
            final_url=_response_next_url(continue_resp, auth_base),
            session=session,
            sentinel_source=fresh_data.get("sentinel_source", ""),
            sentinel_flow="authorize_continue",
            proxy=proxy,
        ),
    )
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
        sentinel_token=fresh_token or sentinel_token,
        sentinel_so_token=fresh_so or sentinel_so_token,
    )
    if not ok:
        status = getattr(otp_send_response, "status_code", 0)
        return {"ok": False, "error": f"existing_login_otp_send_failed:{status}"}

    email_cfg = current_config_data().get("email_registration", {})
    poll_timeout = int(otp_timeout or email_cfg.get("otp_timeout", 300))
    code = _poll_email_otp(
        mailbox,
        subject_keyword=LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
        timeout=poll_timeout,
        issued_after_unix=otp_send_started,
        proxy=proxy,
    )
    if not code:
        return {"ok": False, "error": "existing_login_otp_poll_timeout"}

    otp_ok, otp_data = _validate_email_otp(session, auth_base, base_headers, code,
        sentinel_data={
            "sentinel_token": fresh_token or sentinel_token,
            "sentinel_so_token": fresh_so or sentinel_so_token,
        })
    if not otp_ok:
        return {"ok": False, "error": f"existing_login_otp_validate:{json.dumps(otp_data, ensure_ascii=False)[:200]}"}
    mfa_result = _complete_existing_login_totp(
        session,
        auth_base,
        base_headers,
        otp_data,
        did=did,
        sentinel_token=fresh_token or sentinel_token,
        sentinel_so_token=fresh_so or sentinel_so_token,
        totp_secret=totp_secret,
    )
    if not mfa_result.get("ok"):
        return mfa_result
    otp_data = mfa_result.get("data") if isinstance(mfa_result.get("data"), dict) else otp_data
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


def _complete_existing_login_totp(
    session,
    auth_base,
    base_headers,
    payload,
    *,
    did,
    sentinel_token="",
    sentinel_so_token="",
    totp_secret="",
):
    """Complete a saved TOTP challenge after email OTP verification."""
    if not _is_mfa_challenge_payload(payload):
        return {"ok": True, "data": payload}
    secret = str(totp_secret or "").strip()
    if not secret:
        return {"ok": False, "error": "existing_login_totp_secret_missing"}
    factor_id = _totp_factor_id(payload)
    if not factor_id:
        return {"ok": False, "error": "existing_login_totp_factor_missing"}
    try:
        import pyotp

        code = pyotp.TOTP(secret).now()
    except Exception:
        return {"ok": False, "error": "existing_login_totp_code_failed"}

    referer = _response_next_url_from_data(payload, auth_base) or f"{auth_base}/mfa-challenge/{factor_id}"
    headers = _auth_request_headers(
        base_headers,
        did=did,
        referer=referer,
        origin=auth_base,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
        extra={"Content-Type": "application/json"},
    )
    issue = request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/mfa/issue_challenge",
        label="Existing account TOTP challenge",
        json={"type": "totp", "id": factor_id, "force_fresh_challenge": False},
        headers=headers,
        impersonate=auth_impersonate(),
    )
    if issue.status_code not in (200, 201, 202, 204):
        return {"ok": False, "error": f"existing_login_totp_issue_failed:{issue.status_code}"}
    verify = request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/mfa/verify",
        label="Existing account TOTP verify",
        json={"type": "totp", "id": factor_id, "code": code},
        headers=headers,
        impersonate=auth_impersonate(),
    )
    verify_data = _json_or_raw(verify, limit=1000)
    if verify.status_code != 200:
        return {"ok": False, "error": f"existing_login_totp_verify_failed:{verify.status_code}"}
    return {"ok": True, "data": verify_data}


def _is_mfa_challenge_payload(payload):
    if not isinstance(payload, dict):
        return False
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    if str(page.get("type") or "").strip().lower() == "mfa_challenge":
        return True
    return "/mfa-challenge/" in str(_response_next_url_from_data(payload, "") or "").lower()


def _totp_factor_id(payload):
    auth_session = payload.get("oai-client-auth-session") if isinstance(payload, dict) else {}
    if not isinstance(auth_session, dict):
        return ""
    factors = []
    for key in ("mfa_challenge_factors", "mfa_factors"):
        values = auth_session.get(key)
        if isinstance(values, list):
            factors.extend(item for item in values if isinstance(item, dict))
    for factor in factors:
        if str(factor.get("factor_type") or "").strip().lower() == "totp":
            factor_id = str(factor.get("id") or "").strip()
            if factor_id:
                return factor_id
    return ""


def _response_next_url_from_data(payload, auth_base):
    if not isinstance(payload, dict):
        return ""
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    value = str(payload.get("continue_url") or page_payload.get("url") or "").strip()
    return _absolute_url(auth_base, value) if value and auth_base else value
