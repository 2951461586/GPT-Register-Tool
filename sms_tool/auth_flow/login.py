"""Existing-login lane: signin -> continue -> password probe -> OTP -> TOTP."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from . import deps, otp, password_step, sentinel_flow, steps


def _fetch_session_csrf_token(session, chat_base, base_headers, did, session_logging_id):
    """Mint a NextAuth CSRF token on ``session`` itself.

    NextAuth binds ``csrfToken`` to the ``__Host-next-auth.csrf-token`` cookie
    of the *same* client.  A token minted on another session is rejected, and
    NextAuth answers with ``chatgpt.com/auth/login`` instead of the
    auth.openai.com authorize URL -- after which every later
    ``authorize/continue`` is answered with ``409 invalid_state``.

    The signup lane never hits this because it primes ``chat_base`` and reads
    ``/api/auth/csrf`` on the very session it then posts with.  The
    existing-login lane used to reuse the caller's token across a freshly built
    session, so ``registration_handlers``' two call sites behaved differently:
    the one passing ``s.session`` was self-consistent, the one passing a brand
    new ``s.login_session`` was not.

    Returns ``""`` on any transport failure so the caller can fall back to the
    token it was given -- this must never turn a recoverable login into a hard
    transport error.
    """
    try:
        deps.request_with_retry(
            session,
            "get",
            f"{chat_base}/",
            label="Existing account prime",
            headers={
                **base_headers,
                "Accept": "text/html,application/xhtml+xml",
                "Referer": f"{chat_base}/",
            },
            impersonate=deps.auth_impersonate(),
        )
    except Exception as exc:
        print(f"  Existing account prime transport warning: {exc}")
    try:
        response = deps.request_with_retry(
            session,
            "get",
            f"{chat_base}/api/auth/csrf",
            label="Existing account csrf",
            headers=deps.nextauth_headers(
                did,
                session_id=session_logging_id,
                referer=f"{chat_base}/",
                origin=chat_base,
            ),
            impersonate=deps.auth_impersonate(),
        )
        body = deps._json_or_raw(response, limit=1000)
        if not isinstance(body, dict):
            return ""
        return str(body.get("csrfToken") or "").strip()
    except Exception as exc:
        print(f"  Existing account csrf transport warning: {exc}")
        return ""


def _existing_login_signin(session, username, did, session_logging_id, auth_base, chat_base, base_headers, csrf_token):
    print("  Existing account login: probing the login method before spending an email code")
    # The caller may hand us a token minted on a *different* session: one of
    # registration_handlers' two call sites builds a brand new ``login_session``
    # and still passes the main session's ``s.csrf_token``.  Mint the token on
    # the session we are about to post with, and only fall back to the caller's
    # value when that fails -- so this can never be worse than the old
    # behaviour, only more consistent.
    session_csrf_token = _fetch_session_csrf_token(session, chat_base, base_headers, did, session_logging_id)
    if session_csrf_token:
        csrf_token = session_csrf_token
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
    signin_resp = deps.request_with_retry(
        session,
        "post",
        signin_url,
        label="Existing account signin",
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
    # ``_ensure_authorize_context`` fills the same context parameters the signup
    # lane relies on (device_id / ext-oai-did / auth_session_logging_id /
    # passkey capabilities / ccaps / login_hint / screen_hint).  Adding only
    # ``device_id`` left the authorize request unattributable to this mailbox's
    # login transaction.  It is a no-op for non auth.openai.com URLs and never
    # overwrites a parameter the URL already carries.
    auth_session_url = steps._ensure_authorize_context(
        auth_session_url,
        did,
        session_logging_id,
        username,
        screen_hint="login",
        prompt="login",
    )
    authorize_resp = deps.request_with_retry(
        session,
        "get",
        auth_session_url,
        label="Existing account authorize",
        headers={
            **base_headers,
            "Accept": "text/html,application/xhtml+xml",
            "Origin": auth_base,
            "Referer": f"{chat_base}/",
        },
        impersonate=deps.auth_impersonate(),
    )
    current_url = str(authorize_resp.url or "")
    print(f"  Existing account authorize: {authorize_resp.status_code} {current_url}")

    current_lower = current_url.lower()
    if "chatgpt.com" in current_lower and (
        "/api/auth/callback/openai" in current_lower or current_lower.rstrip("/") == chat_base.lower().rstrip("/")
    ):
        return ({"ok": True}, None)

    if steps._is_chatgpt_auth_login_landing(current_url):
        # Observed live on 2026-09-13: when NextAuth does not establish a
        # session on this client, the authorize request bounces to
        # ``chatgpt.com/auth/login`` instead of auth.openai.com.  Posting
        # authorize/continue from that landing page is answered with
        # ``409 invalid_state`` -- and so is every OTP call after it, which made
        # the whole failure read as an OTP problem (``existing_login_otp_validate``)
        # while burning ~90s of mailbox polling per account.  Stop here instead.
        # The signup lane already treats this landing as fatal
        # (``_prepare_signup_auth_state`` retries the next attempt); this lane
        # used to walk straight into the doomed continue.
        # ``invalid_state`` appears in the message on purpose: it keeps this
        # classified as the retryable ``auth_state`` class, exactly like the
        # failure it pre-empts, instead of degrading to ``unknown`` (terminal).
        return (
            {
                "ok": False,
                "error": f"existing_login_signin_not_established:invalid_state:{current_url[:120]}",
            },
            None,
        )

    # The signup lane documents the opposite rule for this exact landing:
    # ``_prepare_signup_auth_state`` returns early when the authorize redirect
    # already landed on ``/email-verification`` -- "the browser sends the OTP
    # from this state; do not POST authorize/continue".  Posting it here was
    # added as a workaround for "OTP send returns 409", a symptom of the CSRF
    # bug that ``_fetch_session_csrf_token`` has since removed.  It now corrupts
    # the session instead: after it, the client auth session still carries
    # signup-era state (``passwordless_login_magic_link_sent``) and a *different*
    # ``email_verification_mode`` than the 2026-09-12 successful login, and is
    # missing ``username`` / ``passwordless_disabled`` /
    # ``passwordless_otp_from_password_redirect`` -- after which every
    # ``email-otp/validate`` answers ``401 login_failed`` (3/3 on 2026-09-14).
    # The transaction's own answer is the cheapest signal available, so keep the
    # payload and the continue target -- not just the status code.  The password
    # probe below reads them before any email code is spent.
    return (None, {"csrf_token": csrf_token, "current_url": current_url})


def _existing_login_continue(session, username, did, auth_base, base_headers, proxy, current_url):
    continue_payload = {}
    continue_next_url = ""
    on_verified_page = steps._is_email_verification_step(current_url)
    if on_verified_page and not steps._existing_login_continue_enabled():
        # Only the POST is dropped -- the sentinel is still minted so the OTP
        # send and TOTP steps keep using a token fresher than the caller's.
        fresh_data, fresh_token, fresh_so = sentinel_flow._authorize_continue_sentinel(session, did, proxy=proxy)
        print("  Existing account continue: skipped (already at email-verification)")
    else:
        fresh_data, fresh_token, fresh_so = sentinel_flow._authorize_continue_sentinel(session, did, proxy=proxy)
        # ``screen_hint`` is a plain string while the username entry is an
        # object, so the literal's inferred type (dict[str, dict[str, str]])
        # is too narrow for this assignment.  Annotate the body explicitly.
        continue_body_json: dict[str, Any] = {"username": {"value": username, "kind": "email"}}
        if on_verified_page:
            # 🔴 2026-09-16: the OTP page is a *generic shell*, not the
            # transaction's answer.  Dropping this POST **was** the whole bug:
            # the password probe reads the ``continue_url`` this response
            # carries, so with the POST dropped the probe could only ever
            # answer ``None`` (``no_transaction_state``) -- and on the signup
            # lane, which passes ``allow_passwordless=False`` for an address
            # the server already reported as registered, ``None`` is terminal
            # rather than a fallback.  Measured that day: 19/19 attempts died
            # on exactly this path, zero exceptions.
            #
            # ``screen_hint: "login"`` is the one field abai's
            # ``_submit_login_email`` sends that this body did not.  The
            # 09-14 corruption the POST was dropped for ("the session still
            # carries signup-era state ... missing username /
            # passwordless_disabled / passwordless_otp_from_password_redirect")
            # is precisely what a *signup* continue produces -- and a body of
            # ``username`` alone is what the signup lane posts
            # (``_prepare_signup_auth_state``).  Declaring the screen hint is
            # therefore the minimal change that can restore the transaction
            # state without asking the server to continue a signup.
            #
            # Toggle: ``registration.existing_login_continue_on_verified_page``
            # (default True).  Set it to false to restore the 09-14 skip.
            continue_body_json["screen_hint"] = "login"
        continue_resp = None
        try:
            continue_resp = deps.request_with_retry(
                session,
                "post",
                f"{auth_base}/api/accounts/authorize/continue",
                label="Existing account continue",
                json=continue_body_json,
                headers=steps._auth_request_headers(
                    base_headers,
                    did=did,
                    referer=current_url or f"{auth_base}/log-in",
                    origin=auth_base,
                    sentinel_token=fresh_token,
                    sentinel_so_token=fresh_so,
                    extra={"Content-Type": "application/json"},
                ),
                impersonate=deps.auth_impersonate(),
            )
        except Exception as exc:
            # On the verified page this POST is an *addition* to the old
            # behaviour, so it must never be able to fail the lane: fall back
            # to the skip -- no transaction state, probe answers
            # ``no_transaction_state`` -- exactly as before.  Off that page the
            # POST is the original path and a transport error still propagates.
            if not on_verified_page:
                raise
            print(f"  Existing account continue transport warning: {exc}")
        if continue_resp is not None:
            print(f"  Existing account continue: {continue_resp.status_code}")
            continue_body = deps._json_or_raw(continue_resp, limit=1000)
            if isinstance(continue_body, dict):
                continue_payload = continue_body
            steps._print_protocol_diagnostic(
                "existing_authorize_continue",
                steps._protocol_diagnostic(
                    response=continue_resp,
                    final_url=steps._response_next_url(continue_resp, auth_base),
                    session=session,
                    sentinel_source=fresh_data.get("sentinel_source", ""),
                    sentinel_flow="authorize_continue",
                    proxy=proxy,
                ),
            )
            if continue_resp.status_code == 200:
                next_url = steps._response_next_url(continue_resp, auth_base)
                continue_next_url = next_url
                if next_url:
                    try:
                        follow_resp = deps._follow_continue_url(
                            session,
                            next_url,
                            base_headers,
                            referer=next_url,
                            label="Existing account continue follow",
                        )
                        current_url = str(getattr(follow_resp, "url", "") or next_url)
                    except Exception as e:
                        print(f"  Existing account continue follow transport warning: {e}")
            elif on_verified_page:
                # A refusal here is not new information -- the old behaviour
                # never asked, so it must not become a new failure mode.  Keep
                # the skip and let the probe report why it could not answer.
                print(
                    "  Existing account continue refused on the verified page: "
                    f"{continue_resp.status_code} (keeping the skip)"
                )
            elif continue_resp.status_code not in (409, 400):
                return ({"ok": False, "error": f"existing_login_continue_failed:{continue_resp.status_code}"}, None)

    # ------------------------------------------------------------------ probe
    # ``/log-in/password`` is the sibling auth step of email OTP inside this
    # same transaction, so the transaction can answer "does this account have a
    # password?" without consuming an email code.  Ask that first: the OTP lane
    # is a measured dead end (5/5 landings on the signup profile step
    # ``/about-you``, 0 NextAuth sessions) and costs a ~59s mailbox poll plus a
    # code to rediscover.  abai's protocol client reaches the same verdict from
    # the same signal and refuses to fall back to email OTP at all.
    return (
        None,
        {
            "current_url": current_url,
            "continue_payload": continue_payload,
            "continue_next_url": continue_next_url,
            "fresh_token": fresh_token,
            "fresh_so": fresh_so,
        },
    )


def _existing_login_probe(
    session,
    did,
    auth_base,
    base_headers,
    sentinel_token,
    sentinel_so_token,
    totp_secret,
    password,
    allow_passwordless,
    current_url,
    continue_payload,
    continue_next_url,
    fresh_token,
    fresh_so,
):
    probe = password_step._probe_login_password_step(
        session,
        auth_base,
        base_headers,
        current_url,
        payload=continue_payload,
        continue_url=continue_next_url,
    )
    print(
        f"  Existing account login method probe: password_step={probe['password_step']} "
        f"({probe['signal'] or 'no_signal'}; source={probe.get('source') or 'none'})"
    )
    # pi-lens-ignore: no-identity-operator-on-literals
    if probe["password_step"] is False:
        # The transaction answered and carried no password form.  The account is
        # passwordless, so an email code is the only remaining method -- and it
        # lands on the signup profile step and yields no session.  Stop here
        # rather than spending the code to learn the same thing.
        return (
            {
                "ok": False,
                "error": f"existing_login_no_password_step:{probe['signal']}",
                "login_method": "probe",
                "password_probe": probe,
            },
            None,
        )
    # pi-lens-ignore: no-identity-operator-on-literals
    if probe["password_step"] is True:
        # A password step is reachable, so the email lane cannot produce a
        # session on this transaction -- spend the password we hold instead.
        if not str(password or "").strip():
            return (
                {
                    "ok": False,
                    "error": "existing_login_password_required",
                    "login_method": "probe",
                    "password_probe": probe,
                },
                None,
            )
        return (
            password_step._password_login_existing_account(
                session,
                auth_base,
                base_headers,
                str(password).strip(),
                current_url,
                did,
                sentinel_token=fresh_token or sentinel_token,
                sentinel_so_token=fresh_so or sentinel_so_token,
                totp_secret=totp_secret,
            ),
            None,
        )
    # ``None`` -- the probe could not tell.  A probe that cannot answer is not
    # evidence of absence, so keep the email lane when the caller still allows
    # it.  A caller that already knows this address is registered
    # (``allow_passwordless=False``) refuses instead: for that address the email
    # lane is a measured dead end, and spending a code to rediscover it is the
    # exact cost this probe exists to avoid.
    if not allow_passwordless:
        return (
            {
                "ok": False,
                "error": "existing_login_password_step_unknown",
                "login_method": "probe",
                "password_probe": probe,
            },
            None,
        )

    return (None, {})


def _continuation_state(state, phase: str) -> dict:
    """Return a phase's continuation state, failing fast on a broken contract.

    Each phase returns ``(terminal, state)``: a phase that settles the login
    returns the terminal dict with ``state=None``; one that continues returns
    ``None`` with the state dict.  Reaching an unwrap with ``None`` therefore
    means a phase violated that contract, and the downstream ``state["..."]``
    reads would fail as an opaque ``TypeError: 'NoneType' object is not
    subscriptable``.  Raise a named error instead so the offending phase is
    identifiable straight from the traceback.
    """
    if not isinstance(state, dict):
        raise RuntimeError(
            f"existing-login phase {phase!r} returned {type(state).__name__} instead of a continuation state"
        )
    return state


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
    password="",
    allow_passwordless=True,
    otp_timeout=None,
):
    """Orchestrate the existing-account login: signin -> continue -> probe -> otp.

    Split 2026-09-19 (P1) from a single 415-line body into four phase
    functions.  Each phase returns ``(terminal, state)``: ``terminal`` is the
    final result dict when the phase settles the login, ``None`` while the
    pipeline should continue; ``state`` carries the locals the next phase
    reads (``current_url``, the continue payload, the fresh sentinel tokens).
    The signature and the returned dicts are unchanged, so the six production
    call sites and the seven test files need no edits.

    Every unwrap goes through :func:`_continuation_state`, so a phase that
    returns ``(None, None)`` raises a named error at the unwrap instead of an
    opaque ``TypeError`` at the first ``state["..."]`` read.
    """
    terminal, state = _existing_login_signin(
        session, username, did, session_logging_id, auth_base, chat_base, base_headers, csrf_token
    )
    if terminal is not None:
        return terminal
    state = _continuation_state(state, "signin")
    current_url = state["current_url"]

    terminal, state = _existing_login_continue(session, username, did, auth_base, base_headers, proxy, current_url)
    if terminal is not None:
        return terminal
    state = _continuation_state(state, "continue")
    current_url = state["current_url"]
    continue_payload = state["continue_payload"]
    continue_next_url = state["continue_next_url"]
    fresh_token = state["fresh_token"]
    fresh_so = state["fresh_so"]

    terminal, _ = _existing_login_probe(
        session,
        did,
        auth_base,
        base_headers,
        sentinel_token,
        sentinel_so_token,
        totp_secret,
        password,
        allow_passwordless,
        current_url,
        continue_payload,
        continue_next_url,
        fresh_token,
        fresh_so,
    )
    if terminal is not None:
        return terminal

    terminal, _ = otp._existing_login_otp(
        session,
        mailbox,
        did,
        auth_base,
        base_headers,
        proxy,
        sentinel_token,
        sentinel_so_token,
        totp_secret,
        otp_timeout,
        current_url,
        fresh_token,
        fresh_so,
    )
    return terminal
