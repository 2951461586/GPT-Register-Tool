import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool.mailbox import _parse_chatai_mailbox_file
from sms_tool.registration import (
    _create_account_continue_url,
    _email_otp_send_url,
    _is_existing_login_redirect,
    _is_user_already_exists,
    _stored_registration_password,
    run_batch,
)


class RegistrationConcurrencyTests(unittest.TestCase):
    def test_prompt_login_query_is_not_existing_login_redirect(self):
        self.assertFalse(_is_existing_login_redirect(
            "https://chatgpt.com/api/auth/signin/openai?prompt=login&screen_hint=signup"
        ))
        self.assertFalse(_is_existing_login_redirect(
            "/api/accounts/authorize?prompt=login&screen_hint=signup"
        ))
        self.assertTrue(_is_existing_login_redirect("https://auth.openai.com/log-in"))

    def test_email_otp_send_url_resumes_email_verification_without_continue_url(self):
        self.assertEqual(
            _email_otp_send_url({}, "https://auth.openai.com", resume_email_verification=True),
            "https://auth.openai.com/api/accounts/email-otp/send",
        )
        self.assertEqual(
            _email_otp_send_url({"continue_url": "/custom/send"}, "https://auth.openai.com", resume_email_verification=True),
            "/custom/send",
        )
        self.assertEqual(_email_otp_send_url({}, "https://auth.openai.com"), "")

    def test_create_account_continue_url_uses_existing_account_redirect(self):
        redirect = "https://chatgpt.com/auth/login_with?callback_path=/"

        self.assertEqual(
            _create_account_continue_url({"error": {"code": "user_already_exists", "redirect_uri": redirect}}),
            redirect,
        )
        self.assertEqual(_create_account_continue_url({"continue_url": "/callback"}), "/callback")
        self.assertTrue(_is_user_already_exists({"error": {"code": "user_already_exists"}}))
        self.assertFalse(_is_user_already_exists({"error": {"code": "invalid_auth_step"}}))

    def test_chatai_parser_repairs_misplaced_alias_plus(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text(
                "CierraRiste7566@+oai01hotmail.com----pw----client----refresh\n",
                encoding="utf-8",
            )

            records = _parse_chatai_mailbox_file(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].email, "cierrariste7566+oai01@hotmail.com")

    def test_chatai_parser_accepts_cfworker_lines_for_selected_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selected_mailboxes.txt"
            path.write_text(
                "cfworker://oai-test@edu.liziai.cloud\n"
                "a+oai01@hotmail.com----pw----client----refresh-a\n",
                encoding="utf-8",
            )

            records = _parse_chatai_mailbox_file(path)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].email, "oai-test@edu.liziai.cloud")
        self.assertEqual(records[0].provider, "cfworker")
        self.assertEqual(records[1].provider, "chatai")

    def test_chatai_parser_requires_client_id_and_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text("user@hotmail.com----mail-password\n", encoding="utf-8")

            records = _parse_chatai_mailbox_file(path)

        self.assertEqual(records, [])

    def test_chatai_parser_accepts_refresh_token_before_uuid_client_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text(
                "user@hotmail.com----pw----refresh-token----8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2\n",
                encoding="utf-8",
            )

            records = _parse_chatai_mailbox_file(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].token, "8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2")
        self.assertEqual(records[0].refresh_token, "refresh-token")

    def test_chatai_parser_preserves_refresh_token_with_delimiter_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text(
                "user@hotmail.com----pw----8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2----part-a----part-b\n",
                encoding="utf-8",
            )

            records = _parse_chatai_mailbox_file(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].token, "8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2")
        self.assertEqual(records[0].refresh_token, "part-a----part-b")

    def test_run_batch_does_not_reuse_mailboxes_when_count_exceeds_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text(
                "a+oai01@hotmail.com----pw----client----refresh-a\n"
                "b+oai01@hotmail.com----pw----client----refresh-b\n",
                encoding="utf-8",
            )
            mailboxes = _parse_chatai_mailbox_file(path)

        seen = []

        def fake_run_email(**kwargs):
            mailbox = kwargs["mailbox"]
            seen.append(mailbox.email)
            return {"success": False, "email": mailbox.email, "error": "stub"}

        with patch("sms_tool.registration._extract_sentinel", return_value={"sentinel_token": "sentinel"}):
            with patch("sms_tool.registration.run_email", side_effect=fake_run_email):
                results = run_batch(count=4, proxy=None, mailboxes=mailboxes, paypal_link=True, workers=4)

        self.assertEqual([r["email"] for r in results], ["a+oai01@hotmail.com", "b+oai01@hotmail.com"])
        self.assertEqual(seen, ["a+oai01@hotmail.com", "b+oai01@hotmail.com"])

    def test_run_batch_prewarms_sentinel_once_before_parallel_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text(
                "a+oai01@hotmail.com----pw----client----refresh-a\n"
                "b+oai01@hotmail.com----pw----client----refresh-b\n",
                encoding="utf-8",
            )
            mailboxes = _parse_chatai_mailbox_file(path)

        seen_sentinels = []

        def fake_run_email(**kwargs):
            seen_sentinels.append(kwargs["sentinel_data"])
            return {"success": True, "email": kwargs["mailbox"].email}

        with patch("sms_tool.registration._extract_sentinel", return_value={"sentinel_token": "sentinel", "sentinel_so_token": "so"}) as extract:
            with patch("sms_tool.registration.run_email", side_effect=fake_run_email):
                results = run_batch(count=2, proxy="socks5h://127.0.0.1:7897", mailboxes=mailboxes, paypal_link=True, workers=2)

        self.assertEqual(extract.call_count, 1)
        self.assertEqual(len(seen_sentinels), 2)
        self.assertTrue(all(item and item.get("sentinel_token") == "sentinel" for item in seen_sentinels))
        self.assertEqual([r["email"] for r in results], ["a+oai01@hotmail.com", "b+oai01@hotmail.com"])

    def test_run_batch_passes_paypal_generation_type_to_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text("a+oai01@hotmail.com----pw----client----refresh-a\n", encoding="utf-8")
            mailboxes = _parse_chatai_mailbox_file(path)

        seen = []

        def fake_run_email(**kwargs):
            seen.append(kwargs.get("paypal_generation_type"))
            return {"success": True, "email": kwargs["mailbox"].email}

        with patch("sms_tool.registration.run_email", side_effect=fake_run_email):
            run_batch(
                count=1,
                proxy=None,
                mailboxes=mailboxes,
                paypal_link=True,
                workers=1,
                paypal_generation_type="paypal_direct_zero_due",
            )

        self.assertEqual(seen, ["paypal_direct_zero_due"])

    def test_stored_registration_password_reuses_non_terminal_failed_password(self):
        with patch("sms_tool.storage.get_account_record", return_value={
            "password": "FirstPassword!A1",
            "error": "email_otp_validate: wrong_email_otp_code",
            "raw_json": "{}",
        }):
            self.assertEqual(_stored_registration_password("a+oai01@hotmail.com"), "FirstPassword!A1")

    def test_stored_registration_password_ignores_password_verify_failures(self):
        with patch("sms_tool.storage.get_account_record", return_value={
            "password": "WrongPassword!A1",
            "error": "password_verify_failed:401",
            "raw_json": "{}",
        }):
            self.assertEqual(_stored_registration_password("a+oai01@hotmail.com"), "")


if __name__ == "__main__":
    unittest.main()
