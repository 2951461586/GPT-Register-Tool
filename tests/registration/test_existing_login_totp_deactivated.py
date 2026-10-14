"""``mfa/verify`` is where a deactivated account is revealed on the email-OTP login lane."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from sms_tool.auth_flow import totp

_PAYLOAD = {
    "page": {"type": "mfa_challenge"},
    "oai-client-auth-session": {"mfa_challenge_factors": [{"factor_type": "totp", "id": "factor-1"}]},
}
_DEACTIVATED = {
    "error": {
        "message": "You do not have an account because it has been deleted or deactivated.",
        "code": "account_deactivated",
    }
}


def _run(verify_status, verify_body):
    issue = Mock(status_code=200)
    verify = Mock(status_code=verify_status)
    verify.json.return_value = verify_body
    with patch.object(totp.deps, "request_with_retry", Mock(side_effect=[issue, verify])), patch(
        "pyotp.TOTP"
    ) as totp_cls:
        totp_cls.return_value.now.return_value = "123456"
        return totp._complete_existing_login_totp(
            Mock(),
            "https://auth.openai.com",
            {"User-Agent": "test"},
            _PAYLOAD,
            did="device-id",
            totp_secret="JBSWY3DPEHPK3PXP",
        )


class TotpVerifyDeactivationTests(unittest.TestCase):
    def test_a_deactivated_account_keeps_the_status_prefix_and_adds_the_verdict(self):
        result = _run(403, _DEACTIVATED)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "existing_login_totp_verify_failed:403:account_deactivated")

    def test_an_unrelated_rejection_stays_a_bare_status(self):
        result = _run(401, {"error": {"code": "invalid_totp"}})

        self.assertEqual(result["error"], "existing_login_totp_verify_failed:401")

    def test_a_non_json_body_with_the_marker_is_still_recognised(self):
        issue = Mock(status_code=200)
        verify = Mock(status_code=403, text="account_deactivated")
        verify.json.side_effect = ValueError("not json")
        with patch.object(totp.deps, "request_with_retry", Mock(side_effect=[issue, verify])), patch(
            "pyotp.TOTP"
        ) as totp_cls:
            totp_cls.return_value.now.return_value = "123456"
            result = totp._complete_existing_login_totp(
                Mock(),
                "https://auth.openai.com",
                {},
                _PAYLOAD,
                did="device-id",
                totp_secret="JBSWY3DPEHPK3PXP",
            )

        self.assertTrue(result["error"].endswith(":account_deactivated"))

    def test_the_recovery_chain_reads_the_verdict_as_terminal(self):
        from sms_tool.accounts.account_recovery import _looks_account_deactivated

        self.assertTrue(_looks_account_deactivated({"error": "existing_login_totp_verify_failed:403:account_deactivated"}))
        self.assertFalse(_looks_account_deactivated({"error": "existing_login_totp_verify_failed:403"}))


if __name__ == "__main__":
    unittest.main()
