import importlib.util
import unittest
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "services" / "mail-otp-web" / "app.py"
_spec = importlib.util.spec_from_file_location("mail_otp_web_app", APP_PATH)
mail_otp_web_app = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
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


if __name__ == "__main__":
    unittest.main()
