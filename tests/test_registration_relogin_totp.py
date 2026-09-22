from unittest.mock import Mock, patch

from sms_tool import auth_flow, registration


def _mfa_payload():
    return {
        "continue_url": "https://auth.openai.com/mfa-challenge/factor-1",
        "page": {"type": "mfa_challenge"},
        "oai-client-auth-session": {
            "mfa_challenge_factors": [
                {"factor_type": "totp", "id": "factor-1"},
            ],
        },
    }


def test_existing_login_totp_requires_saved_secret():
    result = registration._complete_existing_login_totp(
        Mock(),
        "https://auth.openai.com",
        {},
        _mfa_payload(),
        did="device-id",
    )

    assert result == {"ok": False, "error": "existing_login_totp_secret_missing"}


def test_existing_login_totp_issues_and_verifies_challenge():
    issue = Mock(status_code=200)
    verify = Mock(status_code=200)
    with (
        patch("pyotp.TOTP") as totp,
        patch.object(auth_flow, "request_with_retry", side_effect=[issue, verify]) as request,
        patch.object(auth_flow, "_json_or_raw", return_value={"continue_url": "https://chatgpt.com/"}),
    ):
        totp.return_value.now.return_value = "123456"
        result = registration._complete_existing_login_totp(
            Mock(),
            "https://auth.openai.com",
            {},
            _mfa_payload(),
            did="device-id",
            totp_secret="BASE32SECRET",
        )

    assert result["ok"] is True
    assert request.call_count == 2
    assert request.call_args_list[0].kwargs["json"] == {
        "type": "totp",
        "id": "factor-1",
        "force_fresh_challenge": False,
    }
    assert request.call_args_list[1].kwargs["json"] == {
        "type": "totp",
        "id": "factor-1",
        "code": "123456",
    }


def test_existing_login_uses_fresh_authorize_sentinel_for_otp_steps():
    # The lane now primes chat_base and mints its own NextAuth CSRF token before
    # the signin POST (see tests/test_existing_login_csrf_session.py), so the
    # scripted transport needs those two responses first.  ``_json_or_raw`` is
    # patched to return the authorize URL for every call, so the csrf read
    # yields no token and the lane falls back to the ``csrf_token`` argument --
    # which is what this test has always exercised.
    prime = Mock(status_code=200, headers={}, url="https://chatgpt.com/")
    csrf = Mock(status_code=200, headers={}, url="https://chatgpt.com/api/auth/csrf")
    signin = Mock(status_code=200, headers={}, url="https://chatgpt.com/api/auth/signin/openai")
    authorize = Mock(status_code=200, headers={}, url="https://auth.openai.com/email-verification")
    continued = Mock(status_code=200, headers={}, url="https://auth.openai.com/email-verification")
    follow = Mock(status_code=200, headers={}, url="https://auth.openai.com/email-verification")
    mailbox = Mock(provider="remail")

    with (
        patch.object(auth_flow, "request_with_retry", side_effect=[prime, csrf, signin, authorize, continued]),
        patch.object(auth_flow, "_json_or_raw", return_value={"url": authorize.url}),
        patch.object(auth_flow, "_authorize_continue_sentinel", return_value=({}, "fresh-token", "fresh-so")),
        patch.object(auth_flow, "_response_next_url", return_value=authorize.url),
        patch.object(auth_flow, "_follow_continue_url", return_value=follow),
        patch.object(auth_flow, "_print_protocol_diagnostic"),
        patch.object(auth_flow, "_send_existing_login_otp", return_value=(True, Mock(status_code=200))) as send,
        patch.object(auth_flow, "_poll_email_otp", return_value="123456"),
        patch.object(auth_flow, "_validate_email_otp", return_value=(True, {"continue_url": authorize.url})) as validate,
        patch.object(auth_flow, "_complete_existing_login_totp", return_value={"ok": True, "data": {}}) as totp,
        patch.object(auth_flow, "current_config_data", return_value={"email_registration": {"otp_timeout": 1}}),
    ):
        result = auth_flow._login_existing_account_with_email_otp(
            session=Mock(),
            username="user@example.com",
            mailbox=mailbox,
            did="device-id",
            session_logging_id="logging-id",
            auth_base="https://auth.openai.com",
            chat_base="https://chatgpt.com",
            base_headers={"User-Agent": "test"},
            csrf_token="csrf",
            sentinel_token="stale-token",
            sentinel_so_token="stale-so",
        )

    assert result["ok"] is True
    assert send.call_args.kwargs["sentinel_token"] == "fresh-token"
    assert send.call_args.kwargs["sentinel_so_token"] == "fresh-so"
    assert validate.call_args.kwargs["sentinel_data"] == {
        "sentinel_token": "fresh-token",
        "sentinel_so_token": "fresh-so",
    }
    assert totp.call_args.kwargs["sentinel_token"] == "fresh-token"
    assert totp.call_args.kwargs["sentinel_so_token"] == "fresh-so"


def _otp_response(body, status_code=200):
    response = Mock(status_code=status_code, text="{}")
    response.json.return_value = body
    return response


def test_existing_login_otp_posts_the_dispatcher_endpoint_first():
    """``email-otp/send`` is the dispatcher *and* the challenge carrier.

    Measured 2026-09-14 over the whole of ``backend_stdout.log``: ``send``
    established the challenge 18/18 times it was reached, while
    ``email-otp/resend`` established it **0/24** (bare ``{"success": true}``
    acknowledgement, ``409 "Your sign-in session is no longer valid"``, or
    ``429``).  The previous order posted ``resend`` first, so every transaction
    whose ``resend`` answered the bare ack paid a *second* dispatch.
    """
    challenge = _otp_response(
        {
            "continue_url": "https://auth.openai.com/email-verification",
            "method": "GET",
            "page": {
                "type": "email_otp_verification",
                "payload": {"email_verification_mode": "login"},
            },
        }
    )
    with patch.object(
        auth_flow, "request_with_retry", side_effect=[challenge]
    ) as request:
        ok, returned = auth_flow._send_existing_login_otp(
            Mock(),
            "https://auth.openai.com",
            {},
            "https://auth.openai.com/email-verification",
            "device-id",
        )

    assert ok is True
    assert returned is challenge
    urls = [call.args[2] for call in request.call_args_list]
    assert urls == ["https://auth.openai.com/api/accounts/email-otp/send"]
    assert all("passwordless/send-otp" not in url for url in urls)


def test_existing_login_otp_never_posts_a_second_endpoint_after_a_bare_ack():
    """A bare 2xx acknowledgement must not trigger a second dispatch.

    ``resend`` has never carried a challenge (0/24 measured), so posting it
    after ``send`` could only dispatch a second code for the same transaction --
    which abai's protocol client treats as transaction-invalidating.
    """
    acknowledgement = _otp_response({"success": True})
    with patch.object(
        auth_flow, "request_with_retry", side_effect=[acknowledgement]
    ) as request:
        ok, returned = auth_flow._send_existing_login_otp(
            Mock(),
            "https://auth.openai.com",
            {},
            "https://auth.openai.com/email-verification",
            "device-id",
        )

    assert ok is True
    assert returned is acknowledgement
    assert request.call_count == 1
    assert request.call_args_list[0].args[2].endswith("/api/accounts/email-otp/send")


def test_existing_login_otp_falls_back_to_resend_on_an_endpoint_rejection():
    """400/404/405 mean "this endpoint does not apply" -- the other one may.

    This is the only path on which ``resend`` is still reached.
    """
    rejected = _otp_response({"error": {"code": "invalid_state"}}, status_code=400)
    challenge = _otp_response(
        {"continue_url": "https://auth.openai.com/email-verification"}
    )
    with patch.object(
        auth_flow, "request_with_retry", side_effect=[rejected, challenge]
    ) as request:
        ok, returned = auth_flow._send_existing_login_otp(
            Mock(),
            "https://auth.openai.com",
            {},
            "https://auth.openai.com/email-verification",
            "device-id",
        )

    assert ok is True
    assert returned is challenge
    urls = [call.args[2] for call in request.call_args_list]
    assert urls == [
        "https://auth.openai.com/api/accounts/email-otp/send",
        "https://auth.openai.com/api/accounts/email-otp/resend",
    ]


def test_existing_login_otp_send_dispatches_with_get_not_post():
    """🔴 The HTTP method is part of the contract, not an implementation detail.

    Measured 2026-09-21, single account, clean log.  ``POST`` to
    ``email-otp/send`` answers ``200`` with a well-formed
    ``page.type=email_otp_verification`` payload whose
    ``email_verification_mode`` is ``login_challenge`` -- and then **every**
    ``email-otp/validate`` answers ``409 invalid_state`` ("Your sign-in session
    is no longer valid"), 5/5 in the batch runner and 4/4 in targeted probes.
    ``GET`` to the same endpoint answers ``200`` with
    ``email_verification_mode=passwordless_login`` and the very next validate
    answers ``200`` with the NextAuth callback.

    The discriminator that pinned it: with the mailbox poll replaced by a
    garbage code the POST lane answers ``401 wrong_email_otp_code`` (session
    provably valid), and 20s of idle, a real mailbox read and an unchanged
    egress IP all keep it at 401 -- only submitting a genuinely issued code
    flips it to 409, because that code belongs to the ``passwordless_login``
    challenge the GET creates.

    Same class as ``registration_handlers.user_register``: the server names the
    method it wants in its own response bodies and ignoring it breaks every
    later validate.  Do not collapse this back to a single POST.
    """
    challenge = _otp_response(
        {
            "continue_url": "https://auth.openai.com/email-verification",
            "method": "GET",
            "page": {
                "type": "email_otp_verification",
                "payload": {"email_verification_mode": "passwordless_login"},
            },
        }
    )
    with patch.object(
        auth_flow, "request_with_retry", side_effect=[challenge]
    ) as request:
        ok, _ = auth_flow._send_existing_login_otp(
            Mock(),
            "https://auth.openai.com",
            {},
            "https://auth.openai.com/email-verification",
            "device-id",
        )

    assert ok is True
    assert request.call_args_list[0].args[1] == "get"
    assert request.call_args_list[0].args[2].endswith("/api/accounts/email-otp/send")
    # A GET carries no body; sending ``json={}`` on it would be meaningless.
    assert "json" not in request.call_args_list[0].kwargs


def test_existing_login_otp_resend_fallback_stays_a_post():
    """Only ``send`` moved to GET; the 400/404/405 fallback is unchanged."""
    rejected = _otp_response({"error": {"code": "invalid_state"}}, status_code=400)
    challenge = _otp_response(
        {"continue_url": "https://auth.openai.com/email-verification"}
    )
    with patch.object(
        auth_flow, "request_with_retry", side_effect=[rejected, challenge]
    ) as request:
        ok, _ = auth_flow._send_existing_login_otp(
            Mock(),
            "https://auth.openai.com",
            {},
            "https://auth.openai.com/email-verification",
            "device-id",
        )

    assert ok is True
    methods = [call.args[1] for call in request.call_args_list]
    assert methods == ["get", "post"]
    assert request.call_args_list[1].kwargs.get("json") == {}


def test_existing_login_otp_does_not_spend_a_second_request_on_a_rate_limit():
    """429 is terminal: the throttle is per-exit, so retrying the sibling
    endpoint only burns another request against it."""
    throttled = _otp_response(
        {"error": {"message": "Too many requests. Please try again later."}},
        status_code=429,
    )
    with patch.object(
        auth_flow, "request_with_retry", side_effect=[throttled]
    ) as request:
        ok, returned = auth_flow._send_existing_login_otp(
            Mock(),
            "https://auth.openai.com",
            {},
            "https://auth.openai.com/email-verification",
            "device-id",
        )

    assert ok is False
    assert returned is throttled
    assert request.call_count == 1


def test_existing_login_otp_reports_failure_when_no_endpoint_applies():
    """Degenerate case must stay no worse than before: last response, ok False."""
    rejected = _otp_response({"error": {"code": "invalid_state"}}, status_code=400)
    missing = _otp_response({"error": {"code": "not_found"}}, status_code=404)
    with patch.object(
        auth_flow, "request_with_retry", side_effect=[rejected, missing]
    ) as request:
        ok, returned = auth_flow._send_existing_login_otp(
            Mock(),
            "https://auth.openai.com",
            {},
            "https://auth.openai.com/email-verification",
            "device-id",
        )

    assert ok is False
    assert returned is missing
    assert request.call_count == 2
