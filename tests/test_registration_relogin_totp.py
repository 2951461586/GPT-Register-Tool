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
    signin = Mock(status_code=200, headers={}, url="https://chatgpt.com/api/auth/signin/openai")
    authorize = Mock(status_code=200, headers={}, url="https://auth.openai.com/email-verification")
    continued = Mock(status_code=200, headers={}, url="https://auth.openai.com/email-verification")
    follow = Mock(status_code=200, headers={}, url="https://auth.openai.com/email-verification")
    mailbox = Mock(provider="remail")

    with (
        patch.object(auth_flow, "request_with_retry", side_effect=[signin, authorize, continued]),
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


def test_existing_login_resends_without_starting_passwordless_signup_challenge():
    response = Mock(status_code=200, text='{"ok":true}')
    response.json.return_value = {"ok": True}
    with patch.object(auth_flow, "request_with_retry", return_value=response) as request:
        ok, returned = auth_flow._send_existing_login_otp(
            Mock(),
            "https://auth.openai.com",
            {},
            "https://auth.openai.com/email-verification",
            "device-id",
        )

    assert ok is True
    assert returned is response
    assert request.call_args.args[2] == "https://auth.openai.com/api/accounts/email-otp/resend"
    assert "passwordless/send-otp" not in request.call_args.args[2]
