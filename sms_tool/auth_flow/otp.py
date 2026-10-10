"""Email-OTP dispatch, challenge detection and the login OTP phase."""

from __future__ import annotations

import json
import time

from . import deps, steps, totp
from ..registration_protocol_helpers import _safe_int


def _otp_challenge_established(body):
    """Return True when an email-OTP send response carries a real challenge.

    ``/api/accounts/email-otp/resend`` can answer ``200 {"success": true}`` --
    a bare acknowledgement that does **not** move the auth session into the
    login email-verification state.  Treating that as success returned before
    ``/api/accounts/email-otp/send`` was ever tried, and ``send`` is the
    endpoint that returns the actual challenge (recorded live on 2026-09-12)::

        {"continue_url": "https://auth.openai.com/email-verification",
         "method": "GET",
         "page": {"type": "email_otp_verification", ...,
                  "payload": {"email_verification_mode": "login..."}}}

    Without the challenge the OTP mail is not bound to a login transaction, so
    ``/api/accounts/email-otp/validate`` answers ``401 login_failed`` -- which
    is what every existing-login attempt did on 2026-09-14.

    The ordering that this check is paired with has since moved on: the caller
    now posts ``email-otp/send`` **first** and stops after a bare
    acknowledgement rather than falling through to the sibling endpoint (see
    ``_send_existing_login_otp``).  This predicate is unchanged.
    """
    if not isinstance(body, dict):
        return False
    if str(body.get("continue_url") or "").strip():
        return True
    page = body.get("page")
    page = page if isinstance(page, dict) else {}
    if str(page.get("type") or "").strip() == "email_otp_verification":
        return True
    payload = page.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    for source in (body, payload):
        if str(source.get("email_verification_mode") or "").strip():
            return True
    return False


def _send_existing_login_otp(
    session, auth_base, base_headers, current_url, did, sentinel_token="", sentinel_so_token=""
):
    headers = steps._auth_request_headers(
        base_headers,
        did=did,
        referer=current_url or f"{auth_base}/email-verification",
        origin=auth_base,
        sentinel_token=sentinel_token,
        sentinel_so_token=sentinel_so_token,
        extra={"Content-Type": "application/json"},
    )
    last_response = None
    acknowledged_response = None
    # Browser order is: authorize -> GET /email-verification -> the page issues
    # the OTP POST.  That POST is ``email-otp/send``: it dispatches the first
    # code for the login challenge and it is the endpoint that returns the
    # challenge payload itself.
    #
    # ``email-otp/resend`` only re-arms an *existing* challenge.  Measured over
    # the whole of ``runtime/logs/backend_stdout.log`` (2026-09-14): resend was
    # called 24 times and established a challenge **0** times -- 15x
    # ``200 {"success": true}`` bare acknowledgement, 6x ``409 "Your sign-in
    # session is no longer valid. Please start over."``, 3x ``429`` -- while
    # ``send`` established it 18/18 times it was reached.  It was also the only
    # endpoint that ever answered 429, so having it first made the *useless*
    # call the one that aborted the attempt: three recorded
    # ``existing_login_otp_send_failed:429`` runs never reached ``send`` at all.
    # The six ``409`` runs prove ``send`` does not depend on a prior ``resend``
    # having succeeded.
    #
    # Hence ``send`` first.  ``resend`` is kept only as the fallback for the
    # endpoint-level rejections below (400/404/405), which ``send`` has never
    # returned on this lane.  ``passwordless/send-otp`` is deliberately NOT in
    # this list: it belongs to account creation and answers 409 invalid_state.
    #
    # 🔴 2026-09-21 -- the method is **per endpoint**, and ``send`` must be
    # ``GET``.  It used to be ``POST`` for both, which armed the *wrong*
    # challenge: ``send`` answered 200 with a well-formed
    # ``page.type=email_otp_verification`` payload whose
    # ``email_verification_mode`` was ``login_challenge``, the transaction dump
    # stayed healthy right up to the validate call, and then
    # ``/api/accounts/email-otp/validate`` **always** answered
    # ``409 {"code": "invalid_state"}`` ("Your sign-in session is no longer
    # valid") -- after which the dump answered 404, i.e. the POST'd challenge
    # had never been bound to the login transaction at all.
    #
    # Measured on 2026-09-21, single account, clean log:
    #     POST send -> mode=login_challenge -> validate 409   (5/5 in the batch
    #                  runner, plus 4/4 in targeted probes)
    #     GET  send -> mode=passwordless_login -> validate 200
    #                  ({"continue_url": ".../api/auth/callback/openai?code=ac_..."})
    #
    # The 409-vs-401 discriminator is what pinned it: with the mailbox poll
    # replaced by a garbage code the same lane answers ``401
    # wrong_email_otp_code`` (session provably valid) while 20s of idle, a real
    # mailbox read, and an unchanged egress IP all leave it at 401.  Only
    # submitting a *genuinely issued* code turns it into 409 -- because that
    # code belongs to the ``passwordless_login`` challenge the GET creates, not
    # to the ``login_challenge`` the POST created.
    #
    # This is the same class of bug the signup lane already documents at
    # ``registration_handlers.user_register``: the server names the method it
    # wants (``"method": "GET"`` in its own response bodies) and ignoring it
    # makes every later ``email-otp/validate`` fail.  Do not "simplify" this
    # back to a single POST.
    for endpoint, method in (
        ("/api/accounts/email-otp/send", "get"),
        ("/api/accounts/email-otp/resend", "post"),
    ):
        response = deps.request_with_retry(
            session,
            method,
            deps._absolute_url(auth_base, endpoint),
            label=f"Existing account OTP send {endpoint}",
            **({} if method == "get" else {"json": {}}),
            headers=headers,
            impersonate=deps.auth_impersonate(),
        )
        last_response = response
        body = None
        try:
            body = response.json()
        except Exception:
            body = None
        body_preview = ""
        try:
            body_preview = json.dumps(body, ensure_ascii=False)[:200]
        except Exception:
            body_preview = (response.text or "")[:200]
        print(f"  Existing account OTP send: {endpoint} {response.status_code} {body_preview}")
        if response.status_code in (200, 202, 204):
            if _otp_challenge_established(body):
                print(f"  Existing account OTP challenge established by {endpoint}")
                return True, response
            if acknowledged_response is None:
                acknowledged_response = response
            # A bare 2xx acknowledgement carries no challenge.  Posting the
            # *other* endpoint now would dispatch a second code for the same
            # transaction, which abai's protocol client treats as
            # transaction-invalidating -- and it cannot help, because ``resend``
            # has never carried a challenge (0/24 measured) while ``send``
            # carries it 18/18.  Stop here and let the caller continue on the
            # acknowledgement rather than paying a second dispatch to learn
            # nothing.
            print(
                f"  Existing account OTP {endpoint} acknowledged without a challenge; "
                f"not trying the other endpoint (would dispatch a second code)"
            )
            break
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
    # No endpoint carried a challenge.  Fall back to the first bare
    # acknowledgement so this can never be worse than the previous behaviour.
    if acknowledged_response is not None:
        print("  Existing account OTP: no endpoint returned a challenge payload; continuing with acknowledgement")
        return True, acknowledged_response
    # All endpoints exhausted; return last response so caller can decide
    if last_response is not None:
        return False, last_response
    return False, None


def _existing_login_otp(
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
):
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
        if status == 429:
            # The signup lane already answers this exact 429 by arming the
            # process-wide registration rate-limit circuit
            # (``registration_handlers._prepare_signup_auth_state``).  This
            # lane used to collapse it to a bare status code instead, which
            # classified ``unknown`` and left the batch marching: the throttle
            # is per-exit, not per-address, so every later account in the run
            # walked into it.  Measured 2026-09-14: three addresses in one run,
            # each preceded by a full signup attempt, with no pause between
            # them.
            #
            # Reuse the signup lane's circuit, its ``Retry-After`` parser and
            # its wording rather than inventing a second policy here.  The
            # string keeps the hard-stop semantics it already has
            # (``registration_rate_limited`` is both a ``rate_limit`` marker and
            # a terminal marker), so the address is still not retried -- what
            # changes is that the rest of the batch waits instead of being
            # spent against a throttled endpoint.
            retry_after = float(deps._retry_after_seconds(otp_send_response, default=300.0))
            deps.mark_registration_rate_limited(retry_after)
            return ({"ok": False, "error": f"registration_rate_limited:retry_after={retry_after:.0f}s"}, None)
        return ({"ok": False, "error": f"existing_login_otp_send_failed:{status}"}, None)

    email_cfg = deps.current_config_data().get("email_registration", {})
    poll_timeout = _safe_int(otp_timeout or email_cfg.get("otp_timeout", 300), 300)
    code = deps._poll_email_otp(
        mailbox,
        subject_keyword=steps.LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
        timeout=poll_timeout,
        issued_after_unix=otp_send_started,
        proxy=proxy,
    )
    if not code:
        return ({"ok": False, "error": "existing_login_otp_poll_timeout"}, None)

    # Match the signup lane, which is the only OTP path that returns 200 in the
    # same run: ``registration_handlers.validate_email_otp`` calls
    # ``_validate_email_otp(..., use_sentinel=False)``.  This lane instead
    # attached the sentinel minted for ``authorize/continue``, and every
    # validate came back ``401 login_failed`` on 2026-09-14 -- even after
    # ``email-otp/send`` had established a correct login challenge
    # (``continue_url`` + ``page.type=email_otp_verification``).
    # ``codex_oauth`` proves a sentinel is not inherently fatal on this endpoint,
    # but it attaches one cached for the passwordless flow rather than the
    # authorize/continue one, so the *flow binding* is the suspect and the
    # proven lane's choice is the safest parity target.
    otp_ok, otp_data = deps._validate_email_otp(
        session,
        auth_base,
        base_headers,
        code,
        sentinel_data={
            "sentinel_token": fresh_token or sentinel_token,
            "sentinel_so_token": fresh_so or sentinel_so_token,
        },
        use_sentinel=False,
    )
    if not otp_ok:
        # The signup lane dumps the client auth session on this exact failure
        # (``registration_handlers.py`` ``after_otp_validate_failed``).  Without
        # it a 401 here is indistinguishable from a plain wrong-code rejection.
        try:
            deps._fetch_client_auth_session_dump(
                session, auth_base, base_headers, "existing_login_after_otp_validate_failed"
            )
        except Exception as exc:
            print(f"  Existing account otp-validate dump warning: {exc}")
        # 🔴 2026-09-16: ``identity_provider_mismatch`` is deliberately **not**
        # checked here.  It is a real server verdict, but not one this endpoint
        # can produce: its message reads *"You tried signing in as \"…\" **using
        # a password**"*, while this lane posts only ``{"code": code}`` to
        # ``email-otp/validate``.  It was observed exactly once, from
        # ``create_account`` (400, 2026-09-15 07:33:37, run ``b93f598d``), and
        # zero times under any ``email_otp_validate:`` /
        # ``existing_login_otp_validate:`` prefix across every process log.
        # ``registration_handlers.create_account`` owns that branch; see the
        # comment there and ``failure_registry.PASSWORDLESS_SIGNUP_CODE``.
        return (
            {"ok": False, "error": f"existing_login_otp_validate:{json.dumps(otp_data, ensure_ascii=False)[:200]}"},
            None,
        )
    mfa_result = totp._complete_existing_login_totp(
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
        return (mfa_result, None)
    mfa_data = mfa_result.get("data")
    otp_data = mfa_data if isinstance(mfa_data, dict) else otp_data
    if not isinstance(otp_data, dict):
        # ``http_utils._validate_email_otp`` documents ``(ok, body: dict)``, but
        # the body is whatever the server sent: a 200 whose JSON decodes to
        # ``null`` (or to a scalar) reaches here as None/str and used to fail on
        # the next line as an opaque ``AttributeError: 'NoneType' object has no
        # attribute 'get'``.  Name the broken contract instead, and settle the
        # attempt the same way every other validate failure does.
        return ({"ok": False, "error": "existing_login_otp_validate_non_dict_body"}, None)
    final_url = str(otp_data.get("continue_url") or "")
    if steps._is_about_you_step(final_url, otp_data):
        # The OTP verified, but the server routed this login into the *signup*
        # profile step.  Following it cannot produce a NextAuth session -- the
        # client auth session is still the signup transaction -- so the
        # caller's readiness poll would spend four requests to learn
        # ``keys=['WARNING_BANNER']``.  Measured 2026-09-14: 5/5 landings on
        # ``/about-you``, 0 sessions.  Report it as a failure so the caller
        # stops instead of polling a known-dead state.
        print(f"  Existing account OTP continue: profile step, not a login landing ({final_url[:80]})")
        return (
            {
                "ok": False,
                "error": f"existing_login_landed_on_profile_step:{final_url[:120]}",
            },
            None,
        )
    try:
        deps._follow_continue_url(
            session,
            final_url,
            base_headers,
            referer=f"{auth_base}/email-verification",
            label="Existing account OTP continue",
        )
    except Exception as e:
        print(f"  Existing account OTP continue transport warning: {e}")
    return ({"ok": True}, None)
