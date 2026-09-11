import logging
import unittest
from unittest.mock import patch

from sms_tool import otp_strategy, registration
from sms_tool.auth_headers import AUTH_IMPERSONATE


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = "{}"
        self.url = "https://auth.openai.com/api/accounts/email-otp/resend"
        self.headers = {}

    def json(self):
        return self._body


class _RecordingHandler(logging.Handler):
    """Collects LogRecord objects so tests can assert on structured extras."""

    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)


class RegistrationOtpStrategyTests(unittest.TestCase):
    def test_passwordless_resend_failure_does_not_fallback_send_by_default(self):
        calls = []

        def fake_request(*args, **kwargs):
            calls.append(args[2])
            return FakeResponse(400, {"error": {"code": "bad_request"}})

        with patch.object(otp_strategy, "CFG", {"email_registration": {}}), \
             patch.object(otp_strategy, "request_with_retry", side_effect=fake_request):
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

    def test_otp_request_uses_shared_browser_impersonation(self):
        seen = {}

        def fake_request(*args, **kwargs):
            seen.update(kwargs)
            return FakeResponse(200)

        with patch.object(otp_strategy, "request_with_retry", side_effect=fake_request):
            otp_strategy.send_registration_email_otp(
                session=object(),
                auth_base="https://auth.openai.com",
                base_headers={"User-Agent": "test"},
                current_url="https://auth.openai.com/email-verification",
                mode="passwordless",
            )

        self.assertEqual(seen["impersonate"], AUTH_IMPERSONATE)

    def test_otp_impersonation_follows_the_selected_profile(self):
        """P0-3: the OTP stage must not split TLS identity from the signup.

        It used to be pinned to ``AUTH_IMPERSONATE`` (firefox144) while every
        other stage used the pooled profile, so one signup could present two
        different TLS/UA identities.
        """
        seen = {}

        def fake_request(*args, **kwargs):
            seen.update(kwargs)
            return FakeResponse(200)

        with patch.object(otp_strategy, "request_with_retry", side_effect=fake_request), \
             patch.object(otp_strategy, "auth_impersonate", return_value="chrome146"):
            otp_strategy.send_registration_email_otp(
                session=object(),
                auth_base="https://auth.openai.com",
                base_headers={"User-Agent": "test"},
                current_url="https://auth.openai.com/email-verification",
                mode="passwordless",
            )

        self.assertEqual(seen["impersonate"], "chrome146")


class OtpPollObservabilityTests(unittest.TestCase):
    """The OTP wait used to be a blank 300s gap in the operator log.

    Nothing was written until the poll resolved, so "the mail never arrived"
    and "we never looked" were indistinguishable -- the exact question the
    2026-09-11 triage could not answer when every failure sat at 300-303s.
    """

    def setUp(self):
        self.records = []
        self.handler = _RecordingHandler(self.records)
        self.logger = logging.getLogger(otp_strategy.__name__)
        self.old_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)

    def _poll_events(self):
        return [
            record
            for record in self.records
            if str(getattr(record, "event", "") or "").startswith("email_otp_poll_")
        ]

    def test_single_shot_provider_logs_budget_and_outcome(self):
        class Mailbox:
            provider = "icloud"

        code = otp_strategy._poll_registration_email_otp(
            Mailbox(),
            subject_keyword="verification code",
            timeout=300,
            issued_after_unix=0,
            poll_otp_fn=lambda *args, **kwargs: None,
        )

        self.assertIsNone(code)
        events = self._poll_events()
        self.assertEqual(
            [record.event for record in events],
            ["email_otp_poll_start", "email_otp_poll_end"],
        )
        self.assertEqual(events[0].provider, "icloud")
        self.assertEqual(events[0].otp_timeout_s, 300)
        self.assertFalse(events[0].otp_resend_enabled)
        self.assertFalse(events[1].otp_matched)

    def test_remail_logs_the_resend_window(self):
        class Mailbox:
            provider = "remail"

        calls = []

        def fake_poll(*args, **kwargs):
            calls.append(kwargs["timeout"])
            return "654321" if len(calls) > 1 else None

        code = otp_strategy._poll_registration_email_otp(
            Mailbox(),
            subject_keyword="verification code",
            timeout=300,
            issued_after_unix=0,
            resend_callback=lambda: FakeResponse(200),
            resend_after_seconds=30,
            poll_otp_fn=fake_poll,
        )

        self.assertEqual(code, "654321")
        events = self._poll_events()
        self.assertEqual(events[0].otp_resend_enabled, True)
        self.assertEqual(events[0].otp_resend_after_s, 30)
        self.assertEqual(events[-1].otp_matched, True)


if __name__ == "__main__":
    unittest.main()
