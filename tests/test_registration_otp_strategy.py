import unittest
from unittest.mock import patch

from sms_tool import registration


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = "{}"
        self.url = "https://auth.openai.com/api/accounts/email-otp/resend"
        self.headers = {}

    def json(self):
        return self._body


class RegistrationOtpStrategyTests(unittest.TestCase):
    def test_passwordless_resend_failure_does_not_fallback_send_by_default(self):
        calls = []

        def fake_request(*args, **kwargs):
            calls.append(args[2])
            return FakeResponse(400, {"error": {"code": "bad_request"}})

        with patch.object(registration, "CFG", {"email_registration": {}}), \
             patch.object(registration, "request_with_retry", side_effect=fake_request):
            response = registration._send_registration_email_otp(
                session=object(),
                auth_base="https://auth.openai.com",
                base_headers={"User-Agent": "test"},
                current_url="https://auth.openai.com/email-verification",
                mode="passwordless",
            )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(len(calls), 1)
        self.assertIn("/email-otp/resend", calls[0])
        self.assertTrue(response.json()["assumed_pre_sent"])


if __name__ == "__main__":
    unittest.main()
