import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sms_tool import account_creation, auth_flow, batch_runner, otp_strategy, registration
from sms_tool.auth_flow import _ensure_authorize_context
from sms_tool.auth_flow import _protocol_diagnostic
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
    _poll_registration_email_otp,
    _probe_registration_access_token,
    _registration_outcome,
    _create_account_sentinel_token,
    _sentinel_device_id,
    _send_registration_email_otp,
    _validate_email_otp,
    _signup_signin_attempts,
    _stored_registration_password,
    run_batch,
)


class RegistrationConcurrencyTests(unittest.TestCase):
    def test_protocol_diagnostic_redacts_query_parameters(self):
        diagnostic = _protocol_diagnostic(final_url="https://auth.openai.com/email-verification?state=secret&code=secret")
        self.assertEqual(diagnostic["final_url"], "https://auth.openai.com/email-verification")
        self.assertNotIn("secret", str(diagnostic))

    def test_stability_probe_uses_registration_proxy_and_releases_gate_while_waiting(self):
        stages = []
        with patch.object(registration, "CFG", {"registration": {
                 "at_stability_probe_count": 2,
                 "at_stability_probe_delay_seconds": 10,
             }}), \
             patch("sms_tool.account_liveness.probe_account_liveness", return_value={"status_code": 200}) as probe, \
             patch.object(registration, "registration_stage", side_effect=stages.append), \
             patch.object(registration.time, "sleep") as sleep:
            result = _probe_registration_access_token("at", {}, proxy="http://proxy.example:8080")

        self.assertEqual(result["stability_status_codes"], [200, 200])
        self.assertEqual(probe.call_count, 2)
        self.assertTrue(all(call.kwargs["proxy"] == "http://proxy.example:8080" for call in probe.call_args_list))
        self.assertEqual(stages, ["access_token_stability_wait", "access_token_probe"])
        sleep.assert_called_once_with(10.0)

    def test_http_200_access_token_is_registration_success_even_when_create_step_warned(self):
        success, error, warning = _registration_outcome(
            False,
            {"error": {"code": "invalid_auth_step", "message": "Invalid authorization step."}},
            "header.payload.signature",
            {"status_code": 200, "status": "active"},
        )

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertIn("invalid_auth_step", warning)

    def test_access_token_401_is_not_registration_success(self):
        success, error, warning = _registration_outcome(
            True,
            {},
            "header.payload.signature",
            {"status_code": 401, "status": "token_invalid"},
        )

        self.assertFalse(success)
        self.assertEqual(error, "access_token_probe_http_401")
        self.assertEqual(warning, "")

    def test_registration_reexports_focused_module_implementations(self):
        self.assertIs(registration._response_next_url, auth_flow._response_next_url)
        self.assertIs(registration._openai_signin_url, auth_flow._openai_signin_url)
        self.assertIs(registration._signup_signin_attempts, auth_flow._signup_signin_attempts)
        self.assertIs(registration._passwordless_signin_attempts, auth_flow._passwordless_signin_attempts)
        self.assertIs(registration._invalid_state_auth_response, auth_flow._invalid_state_auth_response)
        self.assertIs(registration._validate_email_otp, account_creation._validate_email_otp)
        self.assertIs(registration._send_registration_email_otp, otp_strategy.send_registration_email_otp)

    def test_prompt_login_query_is_not_existing_login_redirect(self):
        self.assertFalse(_is_existing_login_redirect(
            "https://chatgpt.com/api/auth/signin/openai?prompt=login&screen_hint=signup"
        ))
        self.assertFalse(_is_existing_login_redirect(
            "/api/accounts/authorize?prompt=login&screen_hint=signup"
        ))
        self.assertTrue(_is_existing_login_redirect("https://auth.openai.com/log-in"))
        self.assertFalse(_is_existing_login_redirect("https://example.com/login"))
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

    def test_signup_login_redirect_advances_username_instead_of_aborting(self):
        signin_response = Mock()
        signin_response.status_code = 200
        signin_response.json.return_value = {"url": "https://auth.openai.com/api/accounts/authorize"}
        signin_response.headers = {}
        signin_response.url = "https://chatgpt.com/api/auth/signin/openai"

        authorize_response = Mock()
        authorize_response.status_code = 302
        authorize_response.headers = {"location": "/log-in"}
        authorize_response.url = "https://auth.openai.com/api/accounts/authorize"

        session = Mock()
        session.post.return_value = signin_response
        session.get.return_value = authorize_response
        advanced = {
            "ok": True,
            "status": 200,
            "url": "https://auth.openai.com/email-verification",
        }

        with patch.object(auth_flow, "_continue_signup_username", return_value=advanced) as continue_signup:
            state = auth_flow._prepare_signup_auth_state(
                session,
                "user@example.com",
                "device-id",
                "logging-id",
                "https://auth.openai.com",
                "https://chatgpt.com",
                {},
                "csrf-token",
                attempts=({"name": "login_or_signup", "screen_hint": "login_or_signup", "prompt": ""},),
            )

        self.assertTrue(state["ok"])
        self.assertTrue(state["login_redirect_seen"])
        continue_signup.assert_called_once()

    def test_passwordless_signin_primary_attempt_matches_har_login_or_signup(self):
        attempts = _passwordless_signin_attempts()

        self.assertEqual(attempts[0]["name"], "login_or_signup")
        self.assertEqual(attempts[0]["screen_hint"], "login_or_signup")
        self.assertEqual(attempts[0]["prompt"], "")

    def test_authorize_url_preserves_current_browser_context_parameters(self):
        url = _ensure_authorize_context(
            "https://auth.openai.com/api/accounts/authorize?state=state-1",
            "did-1", "logging-1", "user@example.com",
            screen_hint="login_or_signup",
        )
        self.assertIn("ext-passkey-client-capabilities=11111", url)
        self.assertIn("ccaps=login_methods", url)
        self.assertIn("device_id=did-1", url)
        self.assertIn("ext-oai-did=did-1", url)
        self.assertIn("login_hint=user%40example.com", url)

    def test_passwordless_web_auth_does_not_call_authorize_continue(self):
        signin_response = Mock(status_code=200, url="https://chatgpt.com/api/auth/signin/openai")
        signin_response.json.return_value = {"url": "https://auth.openai.com/api/accounts/authorize?state=state-1"}
        signin_response.headers = {}
        authorize_response = Mock(status_code=302, url="https://auth.openai.com/email-verification")
        authorize_response.headers = {"location": "/email-verification"}
        session = Mock()
        session.post.return_value = signin_response
        session.get.return_value = authorize_response

        state = auth_flow._prepare_signup_auth_state(
            session, "user@example.com", "did-1", "logging-1",
            "https://auth.openai.com", "https://chatgpt.com", {}, "csrf",
            passwordless_web=True,
            attempts=({"name": "login_or_signup", "screen_hint": "login_or_signup", "prompt": ""},),
        )

        self.assertTrue(state["ok"])
        session.post.assert_called_once()

    def test_passwordless_login_page_uses_guarded_password_fallback(self):
        signin_response = Mock(status_code=200, url="https://chatgpt.com/api/auth/signin/openai")
        signin_response.json.return_value = {"url": "https://auth.openai.com/api/accounts/authorize?state=state-1"}
        signin_response.headers = {}
        authorize_response = Mock(status_code=200, url="https://auth.openai.com/log-in/password")
        authorize_response.headers = {}
        session = Mock()
        session.post.return_value = signin_response
        session.get.return_value = authorize_response
        advanced = {"ok": True, "status": 200, "url": "https://auth.openai.com/create-account/password"}

        with patch.object(auth_flow, "_continue_signup_username", return_value=advanced) as continue_signup:
            state = auth_flow._prepare_signup_auth_state(
                session, "user@example.com", "did-1", "logging-1",
                "https://auth.openai.com", "https://chatgpt.com", {}, "csrf",
                passwordless_web=True,
                attempts=({"name": "login_or_signup", "screen_hint": "login_or_signup", "prompt": ""},),
            )

        self.assertTrue(state["ok"])
        self.assertTrue(state["password_fallback"])
        continue_signup.assert_called_once()

    def test_cookie_presence_handles_curl_cookie_names(self):
        session = Mock()
        session.cookies = ["oai-did", "__Secure-next-auth.state"]
        presence = auth_flow._cookie_presence(session)
        self.assertTrue(presence["oai_did"])
        self.assertTrue(presence["nextauth_state"])

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

    def test_create_account_requires_oauth_sentinel_token(self):
        with self.assertRaisesRegex(RuntimeError, "sentinel_extract_failed"):
            _create_account_sentinel_token({
                "sentinel_token": '{"id":"did-1","flow":"username_password_create"}',
                "oai_did": "did-1",
            }, proxy="http://proxy.example:8080")

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

    def test_passwordless_email_otp_resend_400_falls_back_to_send_when_opted_in(self):
        resend = Mock(status_code=400, text='{"error":"bad resend"}')
        resend.json.return_value = {"error": "bad resend"}
        send = Mock(status_code=200, text='{"success":true}')
        send.json.return_value = {"success": True}
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append(url)
            return resend if url.endswith("/resend") else send

        with patch("sms_tool.otp_strategy.CFG", {"email_registration": {"otp_fallback_send_on_resend_failure": True}}), \
             patch("sms_tool.otp_strategy.request_with_retry", side_effect=fake_request):
            result = _send_registration_email_otp(
                Mock(),
                "https://auth.openai.com",
                {"User-Agent": "test"},
                current_url="https://auth.openai.com/email-verification",
                mode="passwordless",
            )

        self.assertIs(result, send)
        self.assertTrue(calls[0].endswith("/api/accounts/email-otp/resend"))
        self.assertTrue(calls[1].endswith("/api/accounts/passwordless/send-otp"))

    def test_passwordless_email_otp_resend_is_json_request(self):
        response = Mock(status_code=200, text='{"success":true}')
        response.json.return_value = {"success": True}
        seen = {}

        def fake_request(session, method, url, **kwargs):
            seen.update(kwargs)
            return response

        with patch("sms_tool.otp_strategy.request_with_retry", side_effect=fake_request):
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

        with patch("sms_tool.account_creation.request_with_retry", side_effect=fake_request):
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

    def test_remail_otp_poll_resends_once_and_preserves_original_time_window(self):
        mailbox = Mock(provider="remail")
        resend_response = Mock(status_code=200)
        with patch.object(registration, "CFG", {"email_registration": {}}), \
             patch("sms_tool.otp_strategy._poll_email_otp", side_effect=[None, "654321"]) as poll:
            code = _poll_registration_email_otp(
                mailbox,
                subject_keyword="verification code|login code",
                timeout=300,
                issued_after_unix=1_000,
                proxy="proxy",
                resend_callback=lambda: resend_response,
            )

        self.assertEqual(code, "654321")
        self.assertEqual(poll.call_count, 2)
        self.assertEqual(poll.call_args_list[0].kwargs["timeout"], 30)
        self.assertEqual(poll.call_args_list[1].kwargs["timeout"], 270)
        self.assertEqual(poll.call_args_list[0].kwargs["issued_after_unix"], 1_000)
        self.assertEqual(poll.call_args_list[1].kwargs["issued_after_unix"], 1_000)

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

        with patch("sms_tool.registration.run_email", side_effect=fake_run_email):
            results = run_batch(count=4, proxy=None, mailboxes=mailboxes, workers=4)

        self.assertEqual([r["email"] for r in results], ["a+oai01@hotmail.com", "b+oai01@hotmail.com"])
        self.assertCountEqual(seen, ["a+oai01@hotmail.com", "b+oai01@hotmail.com"])

    def test_run_batch_never_shares_sentinel_data_between_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text(
                "a+oai01@hotmail.com----pw----client----refresh-a\n"
                "b+oai01@hotmail.com----pw----client----refresh-b\n"
                "c+oai01@hotmail.com----pw----client----refresh-c\n",
                encoding="utf-8",
            )
            mailboxes = _parse_chatai_mailbox_file(path)

            seen_sentinels = []
            def fake_run_email(**kwargs):
                seen_sentinels.append(kwargs["sentinel_data"])
                return {"success": True, "email": kwargs["mailbox"].email}

            with patch("sms_tool.registration.run_email", side_effect=fake_run_email), \
                 patch("sms_tool.batch_runner.CFG", {"email_registration": {}}):
                results = run_batch(count=3, proxy="socks5h://127.0.0.1:7897", mailboxes=mailboxes, workers=2)

            self.assertEqual(seen_sentinels, [None, None, None])
            self.assertEqual([r["email"] for r in results], [
                "a+oai01@hotmail.com", "b+oai01@hotmail.com", "c+oai01@hotmail.com",
            ])

    def test_opt_in_sentinel_prewarm_is_one_to_one(self):
        mailboxes = [Mock(email=f"account-{index}@example.com") for index in range(3)]
        seen = []
        sequence = iter(range(10))

        def extract(**_kwargs):
            index = next(sequence)
            return {"sentinel_token": f"token-{index}", "oai_did": f"did-{index}"}

        def run_email_func(**kwargs):
            seen.append(kwargs.get("sentinel_data"))
            return {"success": True, "email": kwargs["mailbox"].email}

        with patch.object(batch_runner, "CFG", {"email_registration": {"sentinel_prewarm_window": 2}}), \
             patch("sms_tool.sentinel_tokens._extract_sentinel", side_effect=extract):
            results = batch_runner.run_batch_impl(
                count=3, mailboxes=mailboxes, workers=3, run_email_func=run_email_func,
            )
        warmed = [item for item in seen if item]
        self.assertEqual(len(results), 3)
        self.assertEqual(len(warmed), 2)
        self.assertEqual(len({item["oai_did"] for item in warmed}), 2)

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

    def test_run_batch_has_no_payment_arguments(self):
        parameters = inspect.signature(run_batch).parameters

        self.assertNotIn("paypal_link", parameters)
        self.assertNotIn("payment_method", parameters)
        self.assertNotIn("paypal_generation_type", parameters)

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
