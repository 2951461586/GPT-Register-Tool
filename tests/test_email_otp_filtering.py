import unittest
from sms_tool import codex_oauth
from sms_tool.mailbox import MailboxAccount, _email_otp_candidate
from sms_tool.registration import (
    LOGIN_EMAIL_OTP_SUBJECT_KEYWORD,
    REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD,
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

    def test_login_keyword_is_separate_from_registration_keyword(self):
        self.assertEqual(codex_oauth.LOGIN_EMAIL_OTP_SUBJECT_KEYWORD, LOGIN_EMAIL_OTP_SUBJECT_KEYWORD)
        self.assertNotEqual(LOGIN_EMAIL_OTP_SUBJECT_KEYWORD, REGISTRATION_EMAIL_OTP_SUBJECT_KEYWORD)

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


if __name__ == "__main__":
    unittest.main()
