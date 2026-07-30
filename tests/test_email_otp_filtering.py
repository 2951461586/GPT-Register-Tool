import unittest
from sms_tool import codex_oauth
from sms_tool import mailbox as mailbox_module
from sms_tool.mailbox import (
    MailboxAccount,
    _email_otp_candidate,
    _extract_otp_from_text,
    _provider_otp_issued_after,
)
from sms_tool.registration import (
    LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
    REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD,
    REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS,
)


class EmailOtpFilteringTests(unittest.TestCase):
    def _message(self, subject, received_at="2026-05-28T02:06:44Z"):
        return {
            "id": "msg-1",
            "receivedDateTime": received_at,
            "subject": subject,
            "bodyPreview": "Your code is 123456.",
            "body": {"content": ""},
            "toRecipients": [{"emailAddress": {"address": "target@hotmail.com"}}],
        }

    def test_registration_keyword_rejects_login_code_subject(self):
        mailbox = MailboxAccount(email="target@hotmail.com", provider="chatai")

        login_candidate = _email_otp_candidate(
            mailbox,
            self._message("Your temporary ChatGPT login code"),
            keyword=REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD,
            issued_after_unix=0,
        )
        verification_candidate = _email_otp_candidate(
            mailbox,
            self._message("Your temporary ChatGPT verification code"),
            keyword=REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD,
            issued_after_unix=0,
        )

        self.assertIsNone(login_candidate)
        self.assertEqual(verification_candidate["otp"], "123456")

    def test_registration_poll_keyword_accepts_login_code_fallback(self):
        mailbox = MailboxAccount(email="target@hotmail.com", provider="chatai")

        login_candidate = _email_otp_candidate(
            mailbox,
            self._message("Your temporary ChatGPT login code"),
            keyword=REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORDS,
            issued_after_unix=0,
        )

        self.assertEqual(login_candidate["otp"], "123456")

    def test_cfworker_registration_otp_issued_after_has_small_grace(self):
        mailbox = MailboxAccount(email="target@edu.liziai.cloud", provider="cfworker")
        adjusted = _provider_otp_issued_after(mailbox, 1779934004)

        self.assertEqual(adjusted, 1779933994)

    def test_remail_registration_otp_issued_after_covers_observed_clock_skew(self):
        mailbox = MailboxAccount(email="target@outlook.com", provider="remail")
        adjusted = _provider_otp_issued_after(mailbox, 1779934004)

        self.assertEqual(adjusted, 1779933914)

    def test_login_keyword_is_separate_from_registration_keyword(self):
        self.assertEqual(codex_oauth.LOGIN_EMAIL_OTP_SUBJECT_KEYWORD, LOGIN_EMAIL_OTP_SUBJECT_KEYWORD)
        self.assertNotEqual(LOGIN_EMAIL_OTP_SUBJECT_KEYWORD, REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD)

    def test_otp_extractor_ignores_hex_color_context(self):
        text = "style=\"color:#123456\" Your ChatGPT verification code is 654321."
        self.assertEqual(_extract_otp_from_text(text), "654321")

    def test_otp_candidate_rejects_shadow_tm1_sender(self):
        mailbox = MailboxAccount(email="target@hotmail.com", provider="chatai")
        msg = self._message("Your temporary ChatGPT verification code")
        msg["from"] = "OpenAI <noreply@tm1.openai.com>"
        msg["bodyPreview"] = "Your code is 493682."

        self.assertIsNone(_email_otp_candidate(mailbox, msg, keyword="verification"))

    def test_otp_candidate_accepts_tm1_bounce_delivery_sender(self):
        mailbox = MailboxAccount(email="target@liziai.cloud", provider="cfworker")
        msg = self._message("Your temporary ChatGPT login code")
        msg["from"] = "bounce+abc.target=liziai.cloud@tm1.openai.com"
        msg["bodyPreview"] = "Your login code is 213244."
        msg["toRecipients"] = [{"emailAddress": {"address": "target@liziai.cloud"}}]

        candidate = _email_otp_candidate(mailbox, msg, keyword="login code")

        self.assertEqual(candidate["otp"], "213244")

    def test_otp_candidate_rejects_non_openai_sender_for_outlook_or_gmail(self):
        mailbox = MailboxAccount(email="target@hotmail.com", provider="chatai")
        msg = self._message("Your temporary ChatGPT verification code")
        msg["from"] = "Alerts <alerts@example.net>"
        msg["bodyPreview"] = "Your verification code is 654321."

        self.assertIsNone(_email_otp_candidate(mailbox, msg, keyword="verification"))

    def test_otp_candidate_rejects_tracking_noise_without_otp_context(self):
        mailbox = MailboxAccount(email="target@gmail.com", provider="gmail")
        msg = self._message("Delivery notice")
        msg["bodyPreview"] = "Tracking id 123456. Unsubscribe below."
        msg["toRecipients"] = [{"emailAddress": {"address": "target@gmail.com"}}]

        self.assertIsNone(_email_otp_candidate(mailbox, msg))

    def test_otp_extractor_rejects_tracking_id_before_real_code(self):
        text = "Tracking id 123456. Your ChatGPT verification code is 654321."
        self.assertEqual(_extract_otp_from_text(text), "654321")

    def test_issued_after_filters_pre_send_mail(self):
        mailbox = MailboxAccount(email="target@hotmail.com", provider="chatai")

        old_candidate = _email_otp_candidate(
            mailbox,
            self._message("Your temporary ChatGPT verification code", received_at="2026-05-28T02:06:43Z"),
            keyword=REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD,
            issued_after_unix=1779934004,
        )
        new_candidate = _email_otp_candidate(
            mailbox,
            self._message("Your temporary ChatGPT verification code", received_at="2026-05-28T02:06:44Z"),
            keyword=REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD,
            issued_after_unix=1779934004,
        )

        self.assertIsNone(old_candidate)
        self.assertEqual(new_candidate["otp"], "123456")

    def test_chatai_poll_waits_for_newer_code_during_settle_window(self):
        mailbox = MailboxAccount(email="target@hotmail.com", provider="chatai")
        first = self._message(
            "Your temporary ChatGPT verification code",
            received_at="2026-06-06T03:45:42Z",
        )
        first["id"] = "first"
        first["bodyPreview"] = "Enter this temporary verification code to continue:\n\n851900"
        newer = self._message(
            "Your temporary ChatGPT verification code",
            received_at="2026-06-06T03:45:45Z",
        )
        newer["id"] = "newer"
        newer["bodyPreview"] = "Enter this temporary verification code to continue:\n\n169441"

        with unittest.mock.patch.object(mailbox_module, "_email_cfg", return_value={"otp_settle_seconds": 0.01, "otp_poll_interval": 0.01}):
            with unittest.mock.patch.object(mailbox_module, "_fetch_mailbox_messages", side_effect=[[first], [newer, first], [newer, first]]):
                code = mailbox_module._poll_email_otp(
                    mailbox,
                    subject_keyword=REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD,
                    timeout=1,
                    issued_after_unix=0,
                )

        self.assertEqual(code, "169441")

    def test_gmail_alias_recipient_does_not_match_primary_mailbox(self):
        mailbox = MailboxAccount(email="migueladorno236@gmail.com", provider="gmail")
        message = {
            "id": "msg-gmail-1",
            "receivedDateTime": "2026-07-05T10:00:00Z",
            "subject": "Your temporary ChatGPT verification code",
            "bodyPreview": "Your code is 654321.",
            "body": {"content": ""},
            "toRecipients": [{"emailAddress": {"address": "M.i.g.u.EL.A.D.orno236+qrzzsw@gmail.com"}}],
        }

        candidate = _email_otp_candidate(
            mailbox,
            message,
            keyword=REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD,
            issued_after_unix=0,
        )

        self.assertIsNone(candidate)

    def test_gmail_googlemail_plus_recipient_does_not_match(self):
        mailbox = MailboxAccount(email="liziaicloudxm@gmail.com", provider="gmail")
        message = {
            "id": "msg-gmail-2",
            "receivedDateTime": "2026-07-05T10:00:01Z",
            "subject": "Your temporary ChatGPT verification code",
            "bodyPreview": "Your code is 123456.",
            "body": {"content": ""},
            "internetMessageHeaders": [
                {"name": "Delivered-To", "value": "li.zi.aicl.oudxm+ri1ug@googlemail.com"},
            ],
        }

        candidate = _email_otp_candidate(
            mailbox,
            message,
            keyword=REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD,
            issued_after_unix=0,
        )

        self.assertIsNone(candidate)


if __name__ == "__main__":
    unittest.main()
