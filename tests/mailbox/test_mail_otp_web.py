import importlib.util
import unittest
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[2] / "services" / "mail-otp-web" / "app.py"
_spec = importlib.util.spec_from_file_location("mail_otp_web_app", APP_PATH)
assert _spec is not None and _spec.loader is not None
mail_otp_web_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mail_otp_web_app)


class MailOtpWebTests(unittest.TestCase):
    def test_parse_chatai_client_id_then_refresh_token(self):
        parsed = mail_otp_web_app.parse_account_line(
            "user@hotmail.com----pw----8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2----refresh-token"
        )

        self.assertEqual(parsed["email"], "user@hotmail.com")
        self.assertEqual(parsed["client_id"], "8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2")
        self.assertEqual(parsed["refresh_token"], "refresh-token")

    def test_parse_chatai_refresh_token_then_uuid_client_id(self):
        parsed = mail_otp_web_app.parse_account_line(
            "user@hotmail.com----pw----refresh-token----8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2"
        )

        self.assertEqual(parsed["client_id"], "8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2")
        self.assertEqual(parsed["refresh_token"], "refresh-token")

    def test_parse_chatai_preserves_refresh_token_delimiter_tail(self):
        parsed = mail_otp_web_app.parse_account_line(
            "user@hotmail.com----pw----8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2----part-a----part-b"
        )

        self.assertEqual(parsed["client_id"], "8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2")
        self.assertEqual(parsed["refresh_token"], "part-a----part-b")

    def test_is_request_authorized_respects_configured_api_key(self):
        original_api_key = mail_otp_web_app.CFG.get("api_key")
        try:
            mail_otp_web_app.CFG["api_key"] = "secret-token-123"
            self.assertFalse(mail_otp_web_app.is_request_authorized({}, {}))
            self.assertTrue(mail_otp_web_app.is_request_authorized({"X-API-Key": "secret-token-123"}, {}))
            self.assertTrue(mail_otp_web_app.is_request_authorized({"Authorization": "Bearer secret-token-123"}, {}))
            self.assertFalse(mail_otp_web_app.is_request_authorized({"X-API-Key": "wrong"}, {}))
        finally:
            if original_api_key is None:
                mail_otp_web_app.CFG.pop("api_key", None)
            else:
                mail_otp_web_app.CFG["api_key"] = original_api_key

    def test_should_expose_refresh_token_defaults_false(self):
        original = mail_otp_web_app.CFG.get("expose_refresh_token")
        try:
            mail_otp_web_app.CFG.pop("expose_refresh_token", None)
            self.assertFalse(mail_otp_web_app.should_expose_refresh_token())
            mail_otp_web_app.CFG["expose_refresh_token"] = True
            self.assertTrue(mail_otp_web_app.should_expose_refresh_token())
        finally:
            if original is None:
                mail_otp_web_app.CFG.pop("expose_refresh_token", None)
            else:
                mail_otp_web_app.CFG["expose_refresh_token"] = original


if __name__ == "__main__":
    unittest.main()
