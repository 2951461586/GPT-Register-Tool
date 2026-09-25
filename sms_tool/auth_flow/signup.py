"""Signup lane: signin -> authorize -> continue, and the OTP page prime."""
from __future__ import annotations

from urllib.parse import urlencode, urlparse

from . import deps, sentinel_flow, steps

def _continue_signup_username(session, username, did, auth_base, base_headers, current_url, sentinel_token="", sentinel_so_token="", proxy=""):
    """Ensure auth.openai.com has an active signup state before user/register.

    Recent auth flows may bounce the initial NextAuth authorize request back to
    chatgpt.com/auth/login.  Posting user/register from that landing page always
    returns invalid_state, so advance the auth session with the username first.
    """
    if steps._is_signup_password_step(current_url) or steps._is_email_verification_step(current_url):
        return {"ok": True, "url": current_url, "skipped": True}

    fresh_data, fresh_token, fresh_so = sentinel_flow._authorize_continue_sentinel(
        session,
        did,
        proxy=proxy,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
    )
    referer = current_url if str(current_url or "").startswith(auth_base) else f"{auth_base}/create-account"
    headers = {
        **base_headers,
        **deps.openai_auth_headers(
            did,
            referer=referer,
            origin=auth_base,
            sentinel_token=fresh_token,
            sentinel_so_token=fresh_so,
            extra={"Content-Type": "application/json"},
        ),
    }

    response = deps.request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/authorize/continue",
        label="Signup username continue",
        json={"username": {"value": username, "kind": "email"}},
        headers=headers,
        impersonate=deps.auth_impersonate(),
    )
    body = deps._json_or_raw(response, limit=1000)
    next_url = steps._response_next_url(response, auth_base)
    print(f"  Signup username continue: {response.status_code}" + (f" {next_url}" if next_url else ""))
    diagnostic = steps._protocol_diagnostic(response=response, final_url=next_url, session=session,
                                      sentinel_source=fresh_data.get("sentinel_source", ""),
                                      sentinel_flow="authorize_continue", proxy=proxy)
    steps._print_protocol_diagnostic("authorize_continue", diagnostic)
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
            follow = deps._follow_continue_url(
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
    if not steps._is_email_verification_step(current_url):
        return {"ok": True, "url": current_url, "skipped": True}
    url = deps._absolute_url(auth_base, current_url)
    try:
        response = deps.request_with_retry(
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
            impersonate=deps.auth_impersonate(),
        )
        next_url = steps._response_next_url(response, auth_base)
        if response.status_code in (200, 204, 304) or steps._is_email_verification_step(next_url):
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

    for attempt in (attempts or steps._signup_signin_attempts()):
        name = attempt["name"]
        signin_url = steps._openai_signin_url(
            chat_base,
            did,
            session_logging_id,
            username,
            screen_hint=attempt.get("screen_hint", ""),
            prompt=attempt.get("prompt", ""),
        )
        signin_resp = deps.request_with_retry(
            session,
            "post",
            signin_url,
            label=f"Auth signin {name}",
            data=urlencode(signin_payload),
            headers={**base_headers, "Content-Type": "application/x-www-form-urlencoded",
                     "Origin": chat_base, "Referer": f"{chat_base}/"},
            impersonate=deps.auth_impersonate(),
        )
        signin_body = deps._json_or_raw(signin_resp, limit=1000)
        auth_session_url = signin_body.get("url") or signin_resp.headers.get("location") or signin_resp.url
        auth_session_url = steps._ensure_authorize_context(
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

        authorize_resp = deps.request_with_retry(
            session,
            "get",
            auth_session_url,
            label=f"Auth authorize {name}",
            headers={**base_headers, "Accept": "text/html,application/xhtml+xml", "Referer": f"{chat_base}/"},
            allow_redirects=True,
            impersonate=deps.auth_impersonate(),
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
            current_url = deps._absolute_url(auth_base, location)
        redirect_path = urlparse(current_url).path or "/"
        diagnostic = steps._protocol_diagnostic(response=authorize_resp, final_url=current_url, session=session,
                                          sentinel_source="", sentinel_flow="", proxy=proxy,
                                          signin_attempt=name)
        print(f"  Redirect[{name}]: {authorize_resp.status_code} {redirect_path} "
              f"cookies={diagnostic['cookie_presence']}")
        steps._print_protocol_diagnostic("authorize", diagnostic)

        login_redirect_seen = steps._is_existing_login_redirect(current_url)

        if steps._is_chatgpt_auth_login_landing(current_url):
            last_state = {"ok": False, "attempt": name, "error": "redirected_to_chatgpt_login", "url": current_url}
            continue

        if steps._is_signup_password_step(current_url) or steps._is_email_verification_step(current_url):
            return {"ok": True, "attempt": name, "status": authorize_resp.status_code, "url": current_url,
                    "skipped": True, "diagnostic": diagnostic}

        # Passwordless Web/HAR flow is complete after authorize navigation.
        # The browser sends the OTP from this state; do not POST authorize/continue.
        if passwordless_web:
            if steps._is_chatgpt_auth_login_landing(current_url):
                last_state = {"ok": False, "attempt": name, "status": authorize_resp.status_code,
                              "url": current_url, "error": "authorize_redirect_not_advanced",
                              "diagnostic": diagnostic}
                continue
            if steps._is_existing_login_redirect(current_url):
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
                if signup_state.get("ok") and not steps._is_chatgpt_auth_login_landing(signup_state.get("url", "")):
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
        if login_redirect_seen and steps._is_existing_login_redirect(signup_state.get("url", "")):
            last_state = {
                **signup_state,
                "ok": False,
                "error": "login_redirect_not_advanced",
            }
            continue
        if signup_state.get("ok") and not steps._is_chatgpt_auth_login_landing(signup_state.get("url", "")):
            return signup_state

        last_state = signup_state
        if signup_state.get("status") == 409 and steps._invalid_state_auth_response(signup_state.get("body")):
            continue
        if steps._is_chatgpt_auth_login_landing(signup_state.get("url", "")):
            continue
        return signup_state

    return last_state
