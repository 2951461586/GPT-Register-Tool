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

    def test_icloud_url_gets_a_second_send_window_by_default(self):
        """``icloud_url`` 是转发 URL 渠道，晚到是常态 ⇒ 必须进默认名单。"""
        class Mailbox:
            provider = "icloud_url"

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
        self.assertEqual(calls, [30, 270])
        self.assertTrue(self._poll_events()[0].otp_resend_enabled)

    def test_single_shot_reason_separates_no_callback_from_no_capability(self):
        """``resend=none`` 曾经把两种**修法完全不同**的情况混成一句话。

        「调用方没接回调」= 本模块的 bug；「这个渠道没有重发能力」= 能力缺口。
        """
        class Mailbox:
            provider = "gmail"

        def poll(*args, **kwargs):
            return None

        otp_strategy._poll_registration_email_otp(
            Mailbox(), subject_keyword="k", timeout=300, issued_after_unix=0,
            resend_callback=lambda: FakeResponse(200), poll_otp_fn=poll,
        )
        self.assertEqual(self._poll_events()[0].otp_resend_reason, "provider_not_eligible")

        self.records.clear()
        otp_strategy._poll_registration_email_otp(
            Mailbox(), subject_keyword="k", timeout=300, issued_after_unix=0,
            poll_otp_fn=poll,
        )
        self.assertEqual(self._poll_events()[0].otp_resend_reason, "no_callback")

    def test_the_disabled_resend_window_still_uses_the_injected_poller(self):
        """窗口被关掉时曾经直连模块级 ``_poll_email_otp``，绕过注入的轮询器。

        注册泳道注入的是 ``MailboxService.poll_otp``；绕过它等于同一件事两条
        路径（本项目铁律 5）。把默认轮询器换成会抛异常的替身来钉住这一点。
        """
        class Mailbox:
            provider = "remail"

        seen = []

        def fake_poll(*args, **kwargs):
            seen.append(kwargs["timeout"])
            return "111111"

        with patch.object(otp_strategy, "_poll_email_otp", side_effect=AssertionError("bypassed")):
            code = otp_strategy._poll_registration_email_otp(
                Mailbox(), subject_keyword="k", timeout=300, issued_after_unix=0,
                resend_callback=lambda: FakeResponse(200),
                resend_after_seconds=0,
                poll_otp_fn=fake_poll,
            )

        self.assertEqual(code, "111111")
        self.assertEqual(seen, [300])
        self.assertEqual(self._poll_events()[0].otp_resend_reason, "window_disabled")


class OtpResendAllowlistTests(unittest.TestCase):
    """``resend_callback`` 从「只对 remail 生效」改成渠道名单（round-2 审计 P1-2）。

    旧门槛是硬编码的 ``provider != "remail"``，于是 gmail / imap / icloud /
    icloud_url / smailr 的 ``resend_callback`` 全是**死参数**：邮件只是晚到，
    也只能烧满整个 ``otp_timeout`` 然后失败。
    """

    class _Mailbox:
        def __init__(self, provider):
            self.provider = provider

    def _poll(self, provider, *, cfg=None):
        resent = []

        def callback():
            resent.append(1)
            return FakeResponse(200)

        calls = []

        def fake_poll(*args, **kwargs):
            calls.append(kwargs["timeout"])
            return "654321" if len(calls) > 1 else None

        with patch.object(
            otp_strategy, "CFG", {"email_registration": {}} if cfg is None else cfg
        ):
            code = otp_strategy._poll_registration_email_otp(
                self._Mailbox(provider),
                subject_keyword="verification code",
                timeout=300,
                issued_after_unix=0,
                resend_callback=callback,
                resend_after_seconds=30,
                poll_otp_fn=fake_poll,
            )
        return code, calls, resent

    def test_a_channel_outside_the_allowlist_never_resends(self):
        code, calls, resent = self._poll("gmail")
        self.assertIsNone(code)
        self.assertEqual(calls, [300])
        self.assertEqual(resent, [])

    def test_the_allowlist_is_configurable(self):
        cfg = {"email_registration": {"otp_resend_providers": "gmail, imap"}}
        code, calls, resent = self._poll("gmail", cfg=cfg)
        self.assertEqual(code, "654321")
        self.assertEqual(calls, [30, 270])
        self.assertEqual(resent, [1])
        # 名单被换掉后 remail 反而失去能力 —— 证明读的是配置而不是常量。
        _, calls, resent = self._poll("remail", cfg=cfg)
        self.assertEqual(calls, [300])
        self.assertEqual(resent, [])

    def test_a_comma_separated_string_and_a_list_are_equivalent(self):
        with patch.object(otp_strategy, "CFG", {"email_registration": {"otp_resend_providers": "a, B"}}):
            self.assertEqual(otp_strategy.otp_resend_providers(), ("a", "b"))
        with patch.object(otp_strategy, "CFG", {"email_registration": {"otp_resend_providers": ["a", " B "]}}):
            self.assertEqual(otp_strategy.otp_resend_providers(), ("a", "b"))

    def test_a_broken_allowlist_degrades_to_the_default(self):
        for broken in ("", "   ", [], ["", " "], 42, {"a": 1}, None):
            with patch.object(otp_strategy, "CFG", {"email_registration": {"otp_resend_providers": broken}}):
                self.assertEqual(
                    otp_strategy.otp_resend_providers(),
                    otp_strategy.DEFAULT_OTP_RESEND_PROVIDERS,
                    msg=repr(broken),
                )

    def test_a_broken_config_section_degrades_to_the_default(self):
        for cfg in ({}, {"email_registration": None}, {"email_registration": "junk"}):
            with patch.object(otp_strategy, "CFG", cfg):
                self.assertEqual(
                    otp_strategy.otp_resend_providers(),
                    otp_strategy.DEFAULT_OTP_RESEND_PROVIDERS,
                    msg=repr(cfg),
                )

    def test_eligibility_is_case_and_whitespace_insensitive(self):
        with patch.object(otp_strategy, "CFG", {"email_registration": {}}):
            self.assertTrue(otp_strategy.otp_resend_eligible("  ICloud_URL "))
            self.assertFalse(otp_strategy.otp_resend_eligible("gmail"))
            self.assertFalse(otp_strategy.otp_resend_eligible(None))
            self.assertFalse(otp_strategy.otp_resend_eligible(""))

    def test_remail_keeps_its_old_behaviour(self):
        """改成名单不能把老行为丢掉 —— remail 原本就走两段式。"""
        code, calls, resent = self._poll("remail")
        self.assertEqual(code, "654321")
        self.assertEqual(calls, [30, 270])
        self.assertEqual(resent, [1])


if __name__ == "__main__":
    unittest.main()
