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
