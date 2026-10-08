"""Signup lane: signin -> authorize -> continue, and the OTP page prime."""

from __future__ import annotations

import logging
from urllib.parse import urlencode, urlparse

from . import deps, sentinel_flow, steps

# New operator-facing lines must go through ``operator_output.emit`` (stdout AND
# the structured log from one source).  The 5 bare ``print`` calls this module
# already had are frozen by ``scripts/bare_print_ratchet.py``.
_LOGGER = logging.getLogger(__name__)


def _emit_operator_line(level, message, *args):
    """Emit one operator-facing line through ``operator_output``.

    New operator lines must go through ``operator_output.emit`` (stdout AND the
    structured log from one source), not a new bare ``print``
    (``scripts/bare_print_ratchet.py``).  ``operator_output`` lives at
    ``sms_tool`` root, so a top-level import here would add an
    ``auth_flow -> sms_tool`` edge that ``scripts/import_layer_ratchet.py``
    freezes; a function-local import is the form both ratchets name for this
    case.  Keeping it in one helper means
    ``scripts/delayed_import_ratchet.py`` counts **one** function-level import
    for this module no matter how many lines are emitted.
    """
    from ..operator_output import emit as _emit

    _emit(level, message, *args)


def _continue_signup_username(
    session,
    username,
    did,
    auth_base,
    base_headers,
    current_url,
    sentinel_token="",
    sentinel_so_token="",
    proxy="",
    force=False,
):
    """Ensure auth.openai.com has an active signup state before user/register.

    Recent auth flows may bounce the initial NextAuth authorize request back to
    chatgpt.com/auth/login.  Posting user/register from that landing page always
    returns invalid_state, so advance the auth session with the username first.

    ``force`` (default **False**) bypasses the entry guard below so the POST can
    also be issued *from* a password/email-verification landing, and makes the
    body declare ``screen_hint: "signup"``.  Only
    ``_prepare_signup_auth_state``'s email-verification branch passes it, behind
    ``steps._signup_email_verification_continue_hint_enabled``; the request then
    logs under the distinct ``Email verification continue hint`` label so the
    A/B harness can tell this mechanism from the sibling toggle's.
    """
    if not force and (steps._is_signup_password_step(current_url) or steps._is_email_verification_step(current_url)):
        return {"ok": True, "url": current_url, "skipped": True}

    # The forced call must be distinguishable in the run log: the harness's
    # mechanism check for ``p1-8-email-verification-continue-hint`` greps the
    # emit line below.  ``p1-5-signup-continue-screen-hint`` does **not** grep
    # the ``Signup username continue:`` print -- that print belongs to the
    # baseline path (it fires with the toggle on or off, and the forced P1-8
    # path prints it too), so a gate keyed on it would false-fail a control arm
    # that merely reached this function and false-pass a P1-5 arm that ran
    # P1-8's path.  P1-5's marker is the branch-internal line further down.
    label = "Email verification continue hint" if force else "Signup username continue"
    if force:
        _emit_operator_line(
            _LOGGER,
            "  Email verification continue hint: posting authorize/continue from %s",
            current_url,
        )

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

    # ``steps._signup_continue_screen_hint_enabled`` owns the why; this call
    # site only owns the shape.  SunnyRegister's ``_authorize_email`` declares
    # the screen on every continue ("login" if existing_account else
    # "signup") and this lane's body is the only one of the three clients that
    # leaves it absent -- the same field-by-field delta the login lane already
    # traced on 2026-09-16.  Gated because the request succeeds today on some
    # exits; a wire change to a working lane needs its own A/B first.
    continue_body: dict[str, object] = {"username": {"value": username, "kind": "email"}}
    if force or steps._signup_continue_screen_hint_enabled():
        continue_body["screen_hint"] = "signup"
        if not force:
            # Mechanism line for ``p1-5-signup-continue-screen-hint``: emitted
            # **inside the toggle's own branch**, so it is present iff the
            # toggle added the field.  ``not force`` keeps it exclusive to
            # P1-5 -- the forced P1-8 path sets the same field but must not
            # satisfy P1-5's manipulation check (2026-10-07 scan §5.2).
            _emit_operator_line(_LOGGER, "  Signup continue declares screen_hint=signup")

    response = deps.request_with_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/authorize/continue",
        label=label,
        json=continue_body,
        headers=headers,
        impersonate=deps.auth_impersonate(),
    )
    body = deps._json_or_raw(response, limit=1000)
    next_url = steps._response_next_url(response, auth_base)
    print(f"  Signup username continue: {response.status_code}" + (f" {next_url}" if next_url else ""))
    diagnostic = steps._protocol_diagnostic(
        response=response,
        final_url=next_url,
        session=session,
        sentinel_source=fresh_data.get("sentinel_source", ""),
        sentinel_flow="authorize_continue",
        proxy=proxy,
    )
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
                label=f"{label} follow",
            )
            final_url = str(getattr(follow, "url", "") or next_url)
        except Exception as exc:
            return {
                "ok": False,
                "status": response.status_code,
                "body": body,
                "url": next_url,
                "error": f"continue_follow_failed:{exc}",
            }
    diagnostic["final_url"] = final_url
    return {"ok": True, "status": response.status_code, "body": body, "url": final_url, "diagnostic": diagnostic}


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


def _prime_create_account_password_page(session, auth_base, base_headers, current_url):
    """GET ``/create-account/password`` so the password page state exists.

    Called by the password lane immediately before ``user/register``, gated by
    ``steps._prime_create_account_password_page_enabled``.

    **Why.**  ``_post_user_register`` sends ``Referer: {auth_base}/create-account/
    password``, so the request already asserts a page state that nothing
    established.  turb navigates first (``navigate_create_account_password``) and
    states the reason: "进入注册密码页，确保协议注册不再走无密码 OTP 分支".  Ours
    lands on ``/email-verification`` and posts from there; the POST is accepted
    (200, ``page.type=email_otp_send``) and the send is accepted (200, lands on
    ``/email-verification``), yet no code is ever dispatched.  See
    ``steps._prime_create_account_password_page_enabled`` for the measurement and
    for the configurations this is meant to separate.

    Deliberately non-fatal.  An unexpected landing is *reported* and returned as
    ``ok=False`` rather than raised, because the arm's whole job is to show what
    the server does when the page state is present: aborting here would fail the
    run for a different reason and mask the comparison.
    """
    if steps._is_signup_password_page(current_url):
        return {"ok": True, "url": current_url, "skipped": True}
    url = f"{auth_base}/create-account/password"
    referer = current_url if str(current_url or "").startswith(auth_base) else f"{auth_base}/"
    navigation_headers = dict(base_headers)
    if steps._prime_navigation_headers_enabled():
        # ``registration.prime_navigation_headers`` (2026-10-07 scan P1-A): a
        # real top-level navigation sends these; ``_follow_continue_url`` alone
        # sends only ``Accept`` + ``Referer``, so the prime's "correct landing"
        # did not prove the server treated it as a navigation.  Marker line is
        # P1-9's manipulation check and is emitted only inside this branch.
        navigation_headers.update({"sec-fetch-site": "same-origin", "sec-fetch-user": "?1"})
        _emit_operator_line(_LOGGER, "  Password page navigation headers: sec-fetch-site=same-origin sec-fetch-user=?1")
    try:
        response = deps._follow_continue_url(
            session,
            url,
            navigation_headers,
            referer=referer,
            label="Create account password page",
        )
    except Exception as exc:
        _emit_operator_line(_LOGGER, "  Create account password page warning: %s", exc)
        return {"ok": False, "error": str(exc), "url": current_url}
    if response is None:
        return {"ok": False, "error": "password_page_not_followed", "url": current_url}
    final_url = str(getattr(response, "url", "") or url)
    status = int(getattr(response, "status_code", 0) or 0)
    if status < 200 or status >= 400:
        return {"ok": False, "status": status, "url": final_url, "error": "password_page_http_error"}
    if steps._is_signup_password_page(final_url):
        return {"ok": True, "status": status, "url": final_url}
    # 🔴 判据是**落点**，不是状态码。  2xx 只说明请求没被拒；如果最终不在密码步，
    # 密码页状态就**没有**建立起来，而这正是本开关要建立的那一件事。  按状态码
    # 判成功，会在最需要诊断的时候给出假阳性（本轮失败形状正是「发码 200 且落点
    # 正确，但派发未发生」）。  turb 对此直接抛异常，这里改为报告 —— 不抛才能看清
    # 服务端在状态未建立时到底做什么。
    _emit_operator_line(_LOGGER, "  Create account password page landed off the password step: %s", final_url)
    return {"ok": False, "status": status, "url": final_url}


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

    if not passwordless_web and steps._signin_screen_hint_login_or_signup_enabled():
        # Mechanism line for ``p1-10-signin-screen-hint``: emitted inside the
        # toggle's own branch, and only on the password lane.  The passwordless
        # lane also sends ``login_or_signup`` (its first attempt), so without
        # ``not passwordless_web`` a passwordless run would satisfy the check
        # without the toggle being read.
        _emit_operator_line(_LOGGER, "  Signin screen_hint=login_or_signup")

    if not passwordless_web and steps._signin_prompt_login_enabled():
        # Mechanism line for ``p1-11-signin-prompt-login``: emitted inside the
        # toggle's own branch, and only on the password lane.
        _emit_operator_line(_LOGGER, "  Signin prompt=login")

    if not passwordless_web and steps._signin_locale_ja_jp_enabled():
        # Mechanism line for ``p1-12-signin-locale``: emitted inside the
        # toggle's own branch, and only on the password lane.
        _emit_operator_line(_LOGGER, "  Signin locale=ja-JP")

    for attempt in attempts or steps._signup_signin_attempts():
        name = attempt["name"]
        signin_url = steps._openai_signin_url(
            chat_base,
            did,
            session_logging_id,
            username,
            screen_hint=attempt.get("screen_hint", ""),
            prompt=attempt.get("prompt", ""),
            locale=attempt.get("locale", ""),
        )
        signin_resp = deps.request_with_retry(
            session,
            "post",
            signin_url,
            label=f"Auth signin {name}",
            data=urlencode(signin_payload),
            headers={
                **base_headers,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": chat_base,
                "Referer": f"{chat_base}/",
            },
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
            locale=attempt.get("locale", ""),
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
        diagnostic = steps._protocol_diagnostic(
            response=authorize_resp,
            final_url=current_url,
            session=session,
            sentinel_source="",
            sentinel_flow="",
            proxy=proxy,
            signin_attempt=name,
        )
        print(
            f"  Redirect[{name}]: {authorize_resp.status_code} {redirect_path} cookies={diagnostic['cookie_presence']}"
        )
        steps._print_protocol_diagnostic("authorize", diagnostic)

        login_redirect_seen = steps._is_existing_login_redirect(current_url)

        # Do not infer a valid auth transaction from a landing URL alone: some
        # adapters retain redirect URLs on 4xx/5xx responses.
        if int(getattr(authorize_resp, "status_code", 0) or 0) >= 400:
            last_state = {
                "ok": False,
                "attempt": name,
                "status": authorize_resp.status_code,
                "url": current_url,
                "error": f"authorize_http_{authorize_resp.status_code}",
                "diagnostic": diagnostic,
            }
            continue

        if steps._is_chatgpt_auth_login_landing(current_url):
            last_state = {"ok": False, "attempt": name, "error": "redirected_to_chatgpt_login", "url": current_url}
            continue

        if steps._is_signup_password_step(current_url) or steps._is_email_verification_step(current_url):
            # The measured failure shape lands here (``/email-verification``)
            # with the server's transaction armed on the passwordless arm even
            # though the POST carried a password.  ``signup_continue_screen_hint``
            # cannot reach it -- its read point sits in
            # ``_continue_signup_username``, which the return below skips -- so
            # the declared-screen hypothesis gets its own gated POST here, on
            # the password lane only.  See
            # ``steps._signup_email_verification_continue_hint_enabled``.
            if (
                not passwordless_web
                and steps._is_email_verification_step(current_url)
                and steps._signup_email_verification_continue_hint_enabled()
            ):
                hinted = _continue_signup_username(
                    session,
                    username,
                    did,
                    auth_base,
                    base_headers,
                    current_url,
                    sentinel_token=authorize_sentinel_token or sentinel_token,
                    sentinel_so_token=sentinel_so_token,
                    proxy=proxy,
                    force=True,
                )
                hinted["attempt"] = name
                hinted["email_verification_continue_hint"] = True
                hinted.setdefault("diagnostic", diagnostic)
                if hinted.get("ok"):
                    return hinted
                last_state = {**hinted, "error": "email_verification_continue_hint_failed"}
                continue
            return {
                "ok": True,
                "attempt": name,
                "status": authorize_resp.status_code,
                "url": current_url,
                "skipped": True,
                "diagnostic": diagnostic,
            }

        # Passwordless Web/HAR flow is complete after authorize navigation.
        # The browser sends the OTP from this state; do not POST authorize/continue.
        if passwordless_web:
            if steps._is_chatgpt_auth_login_landing(current_url):
                last_state = {
                    "ok": False,
                    "attempt": name,
                    "status": authorize_resp.status_code,
                    "url": current_url,
                    "error": "authorize_redirect_not_advanced",
                    "diagnostic": diagnostic,
                }
                continue
            if steps._is_existing_login_redirect(current_url):
                # The server can explicitly disable passwordless signup for an
                # exit (the client_auth_session dump reports
                # passwordless_disabled=true) and route to /log-in/password.
                # This is no longer the passwordless Web path; use the legacy
                # username transition only for this explicit password fallback.
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
                signup_state["password_fallback"] = True
                signup_state.setdefault("diagnostic", diagnostic)
                if signup_state.get("ok") and not steps._is_chatgpt_auth_login_landing(signup_state.get("url", "")):
                    return signup_state
                last_state = {**signup_state, "error": "authorize_login_page"}
                continue
            # Only known auth steps are sufficient evidence that the server
            # established the passwordless signup transaction.
            last_state = {
                "ok": False,
                "attempt": name,
                "status": authorize_resp.status_code,
                "url": current_url,
                "error": "authorize_unrecognized_landing",
                "diagnostic": diagnostic,
            }
            continue

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
