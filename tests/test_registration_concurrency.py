import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sms_tool.mailbox import _parse_chatai_mailbox_file
from sms_tool.registration import (
    _create_account_continue_url,
    _cookie_jar_header,
    _email_otp_send_url,
    _invalid_state_auth_response,
    _is_chatgpt_auth_login_landing,
    _is_email_verification_step,
    _is_existing_login_redirect,
    _is_signup_password_step,
    _is_user_already_exists,
    _normalize_registration_mode,
    _openai_signin_url,
    _passwordless_signin_attempts,
    _create_account_sentinel_token,
    _sentinel_device_id,
    _send_registration_email_otp,
    _validate_email_otp,
    _signup_signin_attempts,
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
        self.assertTrue(_is_chatgpt_auth_login_landing("https://chatgpt.com/auth/login?callbackUrl=https%3A%2F%2Fchatgpt.com%2F"))
        self.assertTrue(_is_signup_password_step("https://auth.openai.com/create-account/password"))
        self.assertFalse(_is_signup_password_step("https://chatgpt.com/auth/login"))
        self.assertTrue(_is_email_verification_step("https://auth.openai.com/email-verification"))
        self.assertFalse(_is_email_verification_step("https://chatgpt.com/auth/login"))

    def test_signup_signin_primary_attempt_does_not_force_login_prompt(self):
        attempts = _signup_signin_attempts()

        self.assertEqual(attempts[0]["name"], "signup_screen_hint")
        self.assertEqual(attempts[0]["screen_hint"], "signup")
        self.assertEqual(attempts[0]["prompt"], "")

        url = _openai_signin_url(
            "https://chatgpt.com",
            "did-123",
            "log-456",
            "a+oai01@hotmail.com",
            screen_hint=attempts[0]["screen_hint"],
            prompt=attempts[0]["prompt"],
        )

        self.assertIn("screen_hint=signup", url)
        self.assertIn("login_hint=a%2Boai01%40hotmail.com", url)
        self.assertNotIn("prompt=login", url)

    def test_passwordless_signin_primary_attempt_matches_har_login_or_signup(self):
        attempts = _passwordless_signin_attempts()

        self.assertEqual(attempts[0]["name"], "login_or_signup")
        self.assertEqual(attempts[0]["screen_hint"], "login_or_signup")
        self.assertEqual(attempts[0]["prompt"], "")

    def test_registration_mode_defaults_to_passwordless_and_keeps_legacy_escape(self):
        self.assertEqual(_normalize_registration_mode(None), "passwordless")
        self.assertEqual(_normalize_registration_mode("har"), "passwordless")
        self.assertEqual(_normalize_registration_mode("passwordless_signup"), "passwordless")
        self.assertEqual(_normalize_registration_mode("legacy"), "password")

    def test_create_account_uses_oauth_create_sentinel_when_available(self):
        self.assertEqual(
            _create_account_sentinel_token({
                "sentinel_token": "username-password-token",
                "sentinel_oauth_token": "oauth-create-token",
            }),
            "oauth-create-token",
        )

    def test_invalid_state_auth_response_detection(self):
        self.assertTrue(_invalid_state_auth_response({
            "error": {
                "code": "invalid_state",
                "message": "Your sign-in session is no longer valid. Please start over to continue.",
            }
        }))
        self.assertFalse(_invalid_state_auth_response({"error": {"code": "user_already_exists"}}))

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

    def test_passwordless_email_otp_resend_400_falls_back_to_send(self):
        resend = Mock(status_code=400, text='{"error":"bad resend"}')
        resend.json.return_value = {"error": "bad resend"}
        send = Mock(status_code=200, text='{"success":true}')
        send.json.return_value = {"success": True}
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append(url)
            return resend if url.endswith("/resend") else send

        with patch("sms_tool.registration.request_with_retry", side_effect=fake_request):
            result = _send_registration_email_otp(
                Mock(),
                "https://auth.openai.com",
                {"User-Agent": "test"},
                current_url="https://auth.openai.com/email-verification",
                mode="passwordless",
            )

        self.assertIs(result, send)
        self.assertTrue(calls[0].endswith("/api/accounts/email-otp/resend"))
        self.assertTrue(calls[1].endswith("/api/accounts/email-otp/send"))

    def test_passwordless_email_otp_resend_is_json_request(self):
        response = Mock(status_code=200, text='{"success":true}')
        response.json.return_value = {"success": True}
        seen = {}

        def fake_request(session, method, url, **kwargs):
            seen.update(kwargs)
            return response

        with patch("sms_tool.registration.request_with_retry", side_effect=fake_request):
            result = _send_registration_email_otp(
                Mock(),
                "https://auth.openai.com",
                {"User-Agent": "test"},
                current_url="https://auth.openai.com/email-verification",
                mode="passwordless",
            )

        self.assertIs(result, response)
        self.assertEqual(seen["json"], {})
        self.assertEqual(seen["headers"]["Content-Type"], "application/json")

    def test_passwordless_validate_can_skip_sentinel_headers(self):
        response = Mock(status_code=200, text='{"continue_url":"/about-you"}')
        response.json.return_value = {"continue_url": "/about-you"}
        seen = {}

        def fake_request(session, method, url, **kwargs):
            seen.update(kwargs)
            return response

        with patch("sms_tool.registration.request_with_retry", side_effect=fake_request):
            ok, _ = _validate_email_otp(
                Mock(),
                "https://auth.openai.com",
                {"User-Agent": "test"},
                "123456",
                sentinel_data={"sentinel_token": "sentinel", "sentinel_so_token": "so"},
                use_sentinel=False,
            )

        self.assertTrue(ok)
        self.assertNotIn("openai-sentinel-token", seen["headers"])
        self.assertNotIn("openai-sentinel-so-token", seen["headers"])

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

    def test_run_batch_does_not_share_sentinel_between_parallel_workers(self):
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

        with patch("sms_tool.registration._extract_sentinel") as extract:
            with patch("sms_tool.registration.run_email", side_effect=fake_run_email):
                results = run_batch(count=2, proxy="socks5h://127.0.0.1:7897", mailboxes=mailboxes, paypal_link=True, workers=2)

        self.assertEqual(extract.call_count, 0)
        self.assertEqual(len(seen_sentinels), 2)
        self.assertEqual(seen_sentinels, [None, None])
        self.assertEqual([r["email"] for r in results], ["a+oai01@hotmail.com", "b+oai01@hotmail.com"])

    def test_sentinel_device_id_reads_cache_field_first_then_token_id(self):
        self.assertEqual(_sentinel_device_id({"oai_did": "did-cache"}), "did-cache")
        self.assertEqual(
            _sentinel_device_id({"sentinel_token": '{"id":"did-token","flow":"username_password_create"}'}),
            "did-token",
        )
        self.assertEqual(_sentinel_device_id({"sentinel_token": "not-json"}), "")

    def test_cookie_jar_header_handles_dict_like_cookie_jar(self):
        class CookieJar:
            def get_dict(self):
                return {"a": "1", "b": "2"}

        self.assertEqual(_cookie_jar_header(CookieJar()), "a=1; b=2")

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
