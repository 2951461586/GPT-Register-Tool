import unittest
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from sms_tool import codex_oauth
from sms_tool.error_classification import classify_error
from sms_tool.failure_registry import BATCH_DROPPED_CLASSES
from sms_tool.registration_outcome import _oauth_result_summary


class CodexOauthTests(unittest.TestCase):
    def test_refresh_skips_terminal_account_without_network(self):
        with patch("sms_tool.codex_oauth.collect_codex_oauth_tokens") as collect:
            result = codex_oauth.refresh_codex_oauth_session({
                "email": "user@example.com",
                "status": "account_deactivated",
            })

        self.assertFalse(result["ok"])
        self.assertTrue(result["terminal"])
        collect.assert_not_called()

    def test_refresh_can_collect_without_persisting(self):
        collected = {"ok": True, "tokens": {"access_token": "at_new", "refresh_token": "rt_new"}}
        with patch("sms_tool.codex_oauth.collect_codex_oauth_tokens", return_value=collected), \
             patch("sms_tool.codex_oauth._save_oauth_tokens") as save:
            result = codex_oauth.refresh_codex_oauth_session(
                {"email": "user@example.com"},
                persist=False,
                force_email_otp_login=True,
            )

        self.assertIs(result, collected)
        save.assert_not_called()

    def test_mailbox_from_data_falls_back_to_config_for_gmail(self):
        fallback = codex_oauth.MailboxAccount(
            email="liziaicloudxm@gmail.com",
            provider="gmail",
            password="abcd efgh ijkl mnop",
            auth_mode="app_password",
            source="config",
        )
        with patch("sms_tool.codex_oauth.mailbox_has_inbox_credentials", side_effect=[False, True]), \
             patch("sms_tool.mailbox._mailbox_from_config", return_value=fallback) as from_config:
            result = codex_oauth._mailbox_from_data({"email": "liziaicloudxm@gmail.com"})

        self.assertIs(result, fallback)
        from_config.assert_called_once()

    def test_mailbox_from_data_does_not_fallback_to_config_for_non_gmail(self):
        with patch("sms_tool.codex_oauth.mailbox_has_inbox_credentials", return_value=False), \
             patch("sms_tool.mailbox._mailbox_from_config") as from_config:
            result = codex_oauth._mailbox_from_data({"email": "user@example.com"})

        self.assertIsNone(result)
        from_config.assert_not_called()

    def test_mailbox_from_data_rehydrates_flat_smailr_storage_fields(self):
        result = codex_oauth._mailbox_from_data({
            "email": "user@smailr.com",
            "mailbox_provider": "smailr",
            "mailbox_source": "purchase",
            "mailbox_token": "mailbox-id",
        })

        self.assertIsNotNone(result)
        self.assertEqual(result.provider, "smailr")
        self.assertEqual(result.source, "purchase")
        self.assertEqual(result.token, "mailbox-id")

    def test_cfworker_mailbox_does_not_inherit_chatgpt_account_password(self):
        data = {
            "email": "target@liziai.cloud",
            "password": "ChatGPTPassword!A1",
            "mailbox": {
                "email": "target@liziai.cloud",
                "provider": "cfworker",
                "password": "",
                "source": "https://worker.example",
            },
        }
        with patch("sms_tool.codex_oauth.mailbox_has_inbox_credentials", return_value=True):
            result = codex_oauth._mailbox_from_data(data)

        self.assertEqual(result.provider, "cfworker")
        self.assertEqual(result.password, "")

    def test_account_deactivated_response_is_terminal(self):
        body = '{"error":{"code":"account_deactivated","message":"You do not have an account because it has been deleted or deactivated."}}'

        self.assertTrue(codex_oauth._is_account_deactivated_response(403, body))
        self.assertFalse(codex_oauth._is_account_deactivated_response(401, body))
        self.assertFalse(codex_oauth._is_account_deactivated_response(403, '{"error":"wrong code"}'))

    def test_phone_verification_url_detection(self):
        self.assertTrue(codex_oauth._needs_phone_verification("https://auth.openai.com/add-phone"))
        self.assertTrue(codex_oauth._needs_phone_verification("https://auth.openai.com/phone-verification"))
        self.assertFalse(codex_oauth._needs_phone_verification("https://auth.openai.com/consent"))

    def test_phone_probe_only_does_not_complete_sms(self):
        with patch("sms_tool.codex_oauth.complete_phone_verification") as complete:
            result = codex_oauth._finish_authorization(
                Mock(),
                {"state": "s", "code_verifier": "v", "redirect_uri": "http://localhost"},
                "did",
                "https://auth.openai.com/add-phone",
                phone_probe_only=True,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["phone_verification_required"])
        self.assertEqual(result["error"], "add_phone_required")
        complete.assert_not_called()

    def test_protocol_stage_detection_matches_oauth_flow_urls(self):
        self.assertEqual(
            codex_oauth._detect_protocol_stage("http://localhost:1455/auth/callback?code=a&state=b"),
            "callback",
        )
        self.assertEqual(codex_oauth._detect_protocol_stage("https://auth.openai.com/consent"), "consent")
        self.assertEqual(codex_oauth._detect_protocol_stage("https://auth.openai.com/log-in/password"), "password")
        self.assertEqual(codex_oauth._detect_protocol_stage("https://auth.openai.com/email-verification"), "email_otp")
        self.assertEqual(codex_oauth._detect_protocol_stage("https://auth.openai.com/add-phone"), "add_phone")

    def test_logged_in_oauth_does_not_force_passwordless_otp(self):
        session = Mock()
        session.cookies.set = Mock()
        response = Mock(status_code=200)
        session.post.return_value = response

        with patch("sms_tool.codex_oauth.load_cached_sentinel", return_value={}), \
             patch("sms_tool.codex_oauth.attach_sentinel"), \
             patch("sms_tool.codex_oauth._next_url", return_value="https://auth.openai.com/consent"), \
             patch("sms_tool.codex_oauth._follow_redirects", return_value=(None, "https://auth.openai.com/consent")), \
             patch("sms_tool.codex_oauth._passwordless_login_and_exchange") as passwordless, \
             patch("sms_tool.codex_oauth._finish_authorization", return_value={"ok": True, "tokens": {"access_token": "at", "refresh_token": "rt_1"}}) as finish:
            result = codex_oauth._login_and_exchange(
                session=session,
                oauth={"auth_url": "https://auth.openai.com/oauth/authorize", "state": "s", "code_verifier": "v", "redirect_uri": "http://localhost"},
                email="user@example.com",
                data={"device_id": "did"},
                current_url="https://auth.openai.com/authorize",
                force_email_otp_login=False,
            )

        self.assertTrue(result["ok"])
        finish.assert_called_once()
        passwordless.assert_not_called()

    def test_authorize_invalid_state_restarts_with_fresh_oauth_request(self):
        session = Mock()
        session.cookies.set = Mock()
        session.post.side_effect = [
            Mock(status_code=409, text='{"error":{"code":"invalid_state"}}'),
            Mock(status_code=200, text="{}"),
        ]
        fresh_oauth = {
            "auth_url": "https://auth.openai.com/oauth/authorize?fresh=1",
            "state": "fresh",
            "code_verifier": "v2",
            "redirect_uri": "http://localhost",
        }

        with patch("sms_tool.codex_oauth._new_oauth_request", return_value=fresh_oauth) as new_oauth, \
             patch("sms_tool.codex_oauth.load_cached_sentinel", return_value={}), \
             patch("sms_tool.codex_oauth.attach_sentinel"), \
             patch("sms_tool.codex_oauth._next_url", return_value="https://auth.openai.com/consent"), \
             patch("sms_tool.codex_oauth._follow_redirects", return_value=(None, "https://auth.openai.com/consent")) as follow, \
             patch("sms_tool.codex_oauth._finish_authorization", return_value={"ok": True, "tokens": {"access_token": "at"}}):
            result = codex_oauth._login_and_exchange(
                session=session,
                oauth={"auth_url": "https://auth.openai.com/oauth/authorize?stale=1", "state": "stale"},
                email="user@example.com",
                data={"device_id": "did"},
                current_url="https://auth.openai.com/authorize",
            )

        self.assertTrue(result["ok"])
        new_oauth.assert_called_once()
        self.assertGreaterEqual(follow.call_count, 1)
        self.assertEqual(session.post.call_count, 2)

    def test_password_login_uses_password_verify_endpoint(self):
        session = Mock()
        response = Mock(status_code=200)
        session.post.return_value = response

        with patch("sms_tool.codex_oauth.load_cached_sentinel", return_value={}), \
             patch("sms_tool.codex_oauth._next_url", return_value="https://auth.openai.com/consent"), \
             patch("sms_tool.codex_oauth._follow_redirects", return_value=(None, "https://auth.openai.com/consent")), \
             patch("sms_tool.codex_oauth._finish_authorization", return_value={"ok": True, "tokens": {"access_token": "at", "refresh_token": "rt_1"}}):
            result = codex_oauth._password_login_and_exchange(
                session=session,
                oauth={"state": "s", "code_verifier": "v", "redirect_uri": "http://localhost"},
                data={"password": "Secret!A1"},
                did="did",
                current_url="https://auth.openai.com/log-in/password",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["login_method"], "password")
        self.assertEqual(session.post.call_args.args[0], "https://auth.openai.com/api/accounts/password/verify")
        self.assertEqual(session.post.call_args.kwargs["json"], {"password": "Secret!A1"})

    def test_forced_password_stage_preserves_passwordless_failure(self):
        session = Mock()

        with patch("sms_tool.codex_oauth._detect_protocol_stage", return_value="password"), \
             patch("sms_tool.codex_oauth._password_login_and_exchange", return_value={"ok": False, "error": "password_verify_failed:400"}), \
             patch("sms_tool.codex_oauth._passwordless_login_and_exchange", return_value={"ok": False, "error": "passwordless_email_otp_poll_timeout"}):
            result = codex_oauth._run_protocol_login_stages(
                session=session,
                oauth={"state": "s", "code_verifier": "v", "redirect_uri": "http://localhost"},
                email="user@example.com",
                data={"email": "user@example.com"},
                did="did",
                current_url="https://auth.openai.com/log-in/password",
                force_email_otp_login=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "passwordless_email_otp_poll_timeout")
        self.assertEqual(result["protocol_stage"], "email_otp")
        self.assertEqual(result["fallback_from"], "email_otp_forced")

    def test_forced_email_otp_preserves_passwordless_failure(self):
        session = Mock()
        phone_attempt = {
            "ok": False,
            "error": "phone_pool_exhausted",
            "message": "all phones exhausted; total remaining capacity=0",
        }

        with patch("sms_tool.codex_oauth._detect_protocol_stage", return_value="password"), \
             patch("sms_tool.codex_oauth._passwordless_login_and_exchange", return_value={
                 "ok": False,
                 "error": "phone_pool_exhausted",
                 "phone_attempt": phone_attempt,
             }):
            result = codex_oauth._run_protocol_login_stages(
                session=session,
                oauth={"state": "s", "code_verifier": "v", "redirect_uri": "http://localhost"},
                email="user@example.com",
                data={"email": "user@example.com"},
                did="did",
                current_url="https://auth.openai.com/log-in/password",
                force_email_otp_login=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "phone_pool_exhausted")
        self.assertEqual(result["protocol_stage"], "email_otp")
        self.assertEqual(result["phone_attempt"], phone_attempt)

    def test_passwordless_send_409_continues_to_mailbox_polling(self):
        response = Mock(status_code=409, text='{"error":"already pending"}')
        session = Mock()
        session.post.return_value = response

        with patch("sms_tool.codex_oauth.load_cached_sentinel", return_value={}):
            result = codex_oauth._send_passwordless_otp(session, "did", "https://auth.openai.com/email-verification")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 409)

    def test_resend_409_keeps_existing_otp_search_window(self):
        send = Mock(status_code=200, text="{}")
        validate1 = Mock(status_code=400, text='{"error":"bad_code"}')
        resend = Mock(status_code=409, text='{"error":"already pending"}')
        validate2 = Mock(status_code=400, text='{"error":"bad_code"}')
        session = Mock()
        session.post.side_effect = [send, validate1, resend, validate2]
        issued_after_values = []

        def poll(*args, **kwargs):
            issued_after_values.append(kwargs.get("issued_after_unix"))
            return "123456"

        # ``time.time`` is patched on the shared ``time`` module, so any incidental
        # caller (e.g. auth-header/sentinel fingerprint building) also sees it.
        # Anchor the first read (the initial OTP window) to 1000 and every later
        # read to 1005 so the assertion still proves a 409 resend keeps the
        # original window without depending on the exact number of time reads.
        def fake_time():
            fake_time.calls += 1
            return 1000 if fake_time.calls == 1 else 1005
        fake_time.calls = 0

        with patch.dict(codex_oauth.CFG, {"email_registration": {"max_otp_retries": 2}}, clear=False), \
             patch("sms_tool.codex_oauth.time.time", side_effect=fake_time), \
             patch("sms_tool.codex_oauth._poll_email_otp", side_effect=poll), \
             patch("sms_tool.codex_oauth.load_cached_sentinel", return_value={}):
            result = codex_oauth._passwordless_login_and_exchange(
                session=session,
                oauth={"state": "s", "code_verifier": "v", "redirect_uri": "http://localhost"},
                data={"email": "user@example.com", "mailbox": {"email": "user@example.com", "refresh_token": "rt", "token": "cid"}},
                did="did",
                current_url="https://auth.openai.com/email-verification",
                timeout=30,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(issued_after_values, [1000, 1000])

    def test_passwordless_oauth_excludes_registration_and_rejected_otps(self):
        send = Mock(status_code=200, text="{}")
        validate1 = Mock(status_code=401, text='{"error":{"code":"login_failed"}}')
        resend = Mock(status_code=200, text="{}")
        validate2 = Mock(status_code=200, text="{}")
        session = Mock()
        session.post.side_effect = [send, validate1, resend, validate2]
        excluded_values = []

        def poll(*args, **kwargs):
            excluded_values.append(set(kwargs.get("excluded_otps") or ()))
            return "111111" if len(excluded_values) == 1 else "222222"

        with patch.dict(codex_oauth.CFG, {"email_registration": {"max_otp_retries": 2}}, clear=False), \
             patch("sms_tool.codex_oauth._poll_email_otp", side_effect=poll), \
             patch("sms_tool.codex_oauth.load_cached_sentinel", return_value={}), \
             patch("sms_tool.codex_oauth._next_url", return_value="https://auth.openai.com/consent"), \
             patch("sms_tool.codex_oauth._follow_redirects", return_value=(None, "https://auth.openai.com/consent")), \
             patch("sms_tool.codex_oauth._finish_authorization", return_value={"ok": True, "tokens": {"access_token": "at", "refresh_token": "rt"}}):
            result = codex_oauth._passwordless_login_and_exchange(
                session=session,
                oauth={"state": "s", "code_verifier": "v", "redirect_uri": "http://localhost"},
                data={
                    "email": "user@example.com",
                    "registration_email_otp": "000000",
                    "mailbox": {"email": "user@example.com", "refresh_token": "mail-rt", "token": "cid"},
                },
                did="did",
                current_url="https://auth.openai.com/email-verification",
                timeout=30,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(excluded_values[0], {"000000"})
        self.assertEqual(excluded_values[1], {"000000", "111111"})

    def test_single_phone_oauth_lane_stays_locked_until_token_exchange(self):
        class TrackingLock:
            def __init__(self):
                self.acquired = False

            def __enter__(self):
                self.acquired = True
                return self

            def __exit__(self, exc_type, exc, tb):
                self.acquired = False

        phone_pool = Mock()
        phone_pool.lock = TrackingLock()

        def follow_redirects(*args, **kwargs):
            self.assertTrue(phone_pool.lock.acquired)
            return None, "http://localhost:1455/auth/callback?code=abc&state=s"

        def exchange_callback(*args, **kwargs):
            self.assertTrue(phone_pool.lock.acquired)
            return {"access_token": "at", "refresh_token": "rt"}

        with patch("sms_tool.codex_oauth.complete_phone_verification", return_value={"ok": True, "next_url": "https://auth.openai.com/continue"}), \
             patch("sms_tool.codex_oauth._follow_redirects", side_effect=follow_redirects), \
             patch("sms_tool.codex_oauth._exchange_callback", side_effect=exchange_callback):
            result = codex_oauth._finish_authorization(
                session=Mock(),
                oauth={"state": "s"},
                did="did",
                current_url="https://auth.openai.com/add-phone",
                phone_pool=phone_pool,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["tokens"]["refresh_token"], "rt")

    def test_phone_verified_redirect_failure_preserves_phone_attempt(self):
        phone_attempt = {"ok": True, "phone": "+233555123456", "next_url": "https://auth.openai.com/continue"}
        with patch("sms_tool.codex_oauth.complete_phone_verification", return_value=phone_attempt), \
             patch("sms_tool.codex_oauth._follow_redirects", side_effect=RuntimeError("curl52")):
            result = codex_oauth._finish_phone_authorization_locked(
                session=Mock(),
                oauth={"state": "s"},
                did="did",
                current_url="https://auth.openai.com/add-phone",
                phone_pool=Mock(),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["phone_attempt"], phone_attempt)
        self.assertIn("phone_verified_oauth_redirect_failed", result["error"])

    def test_saved_oauth_result_exposes_phone_for_batch_mapping(self):
        phone_attempt = {"ok": True, "phone": "+233555123456", "provider": "smsbower"}
        with TemporaryDirectory() as tmp, \
             patch("sms_tool.codex_oauth.upsert_account") as upsert:
            json_path = f"{tmp}/session.json"
            result = codex_oauth._save_oauth_tokens(
                {"email": "user@example.com"},
                json_path,
                {"access_token": "at", "refresh_token": "rt"},
                "user@example.com",
                "codex_oauth_pkce",
                result={"phone_attempt": phone_attempt},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["phone"], "+233555123456")
        self.assertEqual(result["phone_attempt"], phone_attempt)
        upsert.assert_called_once()


class CompleteAboutYouTests(unittest.TestCase):
    """The ``/about-you`` profile step that used to dead-end passwordless login.

    Landing there means the account exists server-side but its profile was never
    finished, so every retry used to cost another OTP and still fail.  These
    tests pin the completion attempt *and* the negative control: the step must
    only run on that landing page, never as an unconditional extra request.
    """

    @staticmethod
    def _response(status_code=200, body=None, text=""):
        response = Mock()
        response.status_code = status_code
        response.text = text
        if body is None:
            response.json.side_effect = ValueError("not json")
        else:
            response.json.return_value = body
        return response

    def test_completes_the_profile_and_resumes_from_the_continue_url(self):
        sentinel = Mock(token="tok", so_token="so")
        response = self._response(body={"continue_url": "https://auth.openai.com/oauth/authorize?x=1"})
        with patch("sms_tool.codex_oauth.issue_sentinel_flow", return_value=sentinel) as issue, \
             patch("sms_tool.codex_oauth.request_with_retry", return_value=response) as request, \
             patch(
                 "sms_tool.codex_oauth._follow_redirects",
                 return_value=(None, "https://chatgpt.com/api/auth/callback/openai?code=abc"),
             ) as follow, \
             patch("sms_tool.codex_oauth._random_name", return_value=("James", "Smith")), \
             patch("sms_tool.codex_oauth._random_birthdate", return_value="1990-01-02"):
            result = codex_oauth._complete_about_you(
                Mock(), "did-1", "https://auth.openai.com/about-you"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://chatgpt.com/api/auth/callback/openai?code=abc")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(request.call_args.args[2], "https://auth.openai.com/api/accounts/create_account")
        self.assertEqual(request.call_args.kwargs["json"], {"name": "James Smith", "birthdate": "1990-01-02"})
        # The sentinel must be minted for the flow this endpoint expects.  The
        # recovery lane's cached sentinel is bound to a different flow, which is
        # why ``issue_sentinel_flow`` is called instead of ``with_sentinel``.
        self.assertEqual(issue.call_args.kwargs["flow"], "oauth_create_account")
        self.assertEqual(follow.call_args.args[1], "https://auth.openai.com/oauth/authorize?x=1")

    def test_an_existing_account_is_reported_instead_of_walking_the_loop(self):
        """Measured 2026-09-14: 23/23 existing addresses answer this exact body."""
        body = {
            "error": {
                "code": "user_already_exists",
                "message": "An account already exists for this email address, please login instead.",
                "redirect_uri": "https://chatgpt.com/auth/login_with?callback_path=/",
                "userAlreadyExistsRecovery": {"action": "continue_to_login"},
            }
        }
        response = self._response(status_code=400, body=body)
        with patch("sms_tool.codex_oauth.issue_sentinel_flow", return_value=Mock(token="t", so_token="s")), \
             patch("sms_tool.codex_oauth.request_with_retry", return_value=response), \
             patch("sms_tool.codex_oauth._follow_redirects") as follow:
            result = codex_oauth._complete_about_you(
                Mock(), "did-1", "https://auth.openai.com/about-you"
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "about_you_existing_account_user_already_exists:continue_to_login",
        )
        self.assertEqual(result["recovery_action"], "continue_to_login")
        self.assertEqual(result["redirect_uri"], "https://chatgpt.com/auth/login_with?callback_path=/")
        # Following ``chatgpt.com/auth/login_with`` is the loop that cost an OTP
        # per lap and still produced no access token (4/4).  It must never run.
        follow.assert_not_called()

    def test_the_dead_end_classifies_as_account_so_the_address_stops_retrying(self):
        """The error string *is* the retry gate, so classify the code's own output.

        Asserting on a literal here would pass even if the implementation stopped
        emitting the marker -- which is the whole point of the marker.
        """
        body = {
            "error": {
                "code": "user_already_exists",
                "userAlreadyExistsRecovery": {"action": "continue_to_login"},
            }
        }
        response = self._response(status_code=400, body=body)
        with patch("sms_tool.codex_oauth.issue_sentinel_flow", return_value=Mock(token="t", so_token="s")), \
             patch("sms_tool.codex_oauth.request_with_retry", return_value=response), \
             patch("sms_tool.codex_oauth._follow_redirects"):
            result = codex_oauth._complete_about_you(
                Mock(), "did-1", "https://auth.openai.com/about-you"
            )

        self.assertEqual(classify_error(result["error"]), "account")
        self.assertIn("account", BATCH_DROPPED_CLASSES)

    def test_the_dead_end_never_uses_the_terminal_flag(self):
        """``terminal`` makes the recovery chain persist ``account_deactivated``.

        That would brand a perfectly live account as deactivated, so the stop
        signal has to come from the error class instead.
        """
        body = {
            "error": {
                "code": "user_already_exists",
                "userAlreadyExistsRecovery": {"action": "continue_to_login"},
            }
        }
        response = self._response(status_code=400, body=body)
        with patch("sms_tool.codex_oauth.issue_sentinel_flow", return_value=Mock(token="t", so_token="s")), \
             patch("sms_tool.codex_oauth.request_with_retry", return_value=response), \
             patch("sms_tool.codex_oauth._follow_redirects") as follow:
            result = codex_oauth._complete_about_you(
                Mock(), "did-1", "https://auth.openai.com/about-you"
            )

        self.assertNotIn("terminal", result)
        follow.assert_not_called()

    def test_missing_continue_url_is_reported_with_the_status(self):
        response = self._response(status_code=500, body={"error": {"message": "boom"}})
        with patch("sms_tool.codex_oauth.issue_sentinel_flow", return_value=Mock(token="t", so_token="s")), \
             patch("sms_tool.codex_oauth.request_with_retry", return_value=response), \
             patch("sms_tool.codex_oauth._follow_redirects") as follow:
            result = codex_oauth._complete_about_you(
                Mock(), "did-1", "https://auth.openai.com/about-you"
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "about_you_no_continue_url")
        self.assertEqual(result["status_code"], 500)
        follow.assert_not_called()

    def test_a_sentinel_failure_stops_before_posting(self):
        with patch("sms_tool.codex_oauth.issue_sentinel_flow", side_effect=RuntimeError("no sdk")), \
             patch("sms_tool.codex_oauth.request_with_retry") as request:
            result = codex_oauth._complete_about_you(
                Mock(), "did-1", "https://auth.openai.com/about-you"
            )

        self.assertFalse(result["ok"])
        self.assertIn("about_you_sentinel_failed", result["error"])
        request.assert_not_called()


class PasswordlessAboutYouIntegrationTests(unittest.TestCase):
    """``_passwordless_login_and_exchange`` must finish the page, not abandon it."""

    @staticmethod
    def _session():
        send = Mock(status_code=200, text="{}")
        validate = Mock(status_code=200, text='{"continue_url":"/about-you"}')
        session = Mock()
        session.post.side_effect = [send, validate]
        return session

    def _call(self, session, **patches):
        return codex_oauth._passwordless_login_and_exchange(
            session=session,
            oauth={"state": "s", "code_verifier": "v", "redirect_uri": "http://localhost"},
            data={
                "email": "user@example.com",
                "mailbox": {"email": "user@example.com", "refresh_token": "mail-rt", "token": "cid"},
            },
            did="did",
            current_url="https://auth.openai.com/email-verification",
            timeout=30,
        )

    def test_landing_on_about_you_completes_it_and_finishes_authorization(self):
        session = self._session()
        with patch.dict(codex_oauth.CFG, {"email_registration": {"max_otp_retries": 1}}, clear=False), \
             patch("sms_tool.codex_oauth._poll_email_otp", return_value="123456"), \
             patch("sms_tool.codex_oauth.load_cached_sentinel", return_value={}), \
             patch("sms_tool.codex_oauth._next_url", return_value="https://auth.openai.com/about-you"), \
             patch(
                 "sms_tool.codex_oauth._follow_redirects",
                 return_value=(None, "https://auth.openai.com/about-you"),
             ), \
             patch(
                 "sms_tool.codex_oauth._complete_about_you",
                 return_value={"ok": True, "url": "https://auth.openai.com/consent", "continued": True},
             ) as about, \
             patch(
                 "sms_tool.codex_oauth._finish_authorization",
                 return_value={"ok": True, "tokens": {"access_token": "at", "refresh_token": "rt"}},
             ) as finish:
            result = self._call(session)

        self.assertTrue(result["ok"])
        self.assertTrue(result["completed_about_you"])
        about.assert_called_once()
        # Authorization must resume from the URL the completion landed on, not
        # from the page we were stuck on.
        self.assertEqual(finish.call_args.args[3], "https://auth.openai.com/consent")

    def test_the_dead_end_reason_reaches_the_caller_verbatim(self):
        """A generic ``passwordless_about_you_required`` would hide the cause."""
        session = self._session()
        attempt = {
            "ok": False,
            "error": "about_you_existing_account_user_already_exists:continue_to_login",
            "recovery_action": "continue_to_login",
            "status_code": 400,
        }
        with patch.dict(codex_oauth.CFG, {"email_registration": {"max_otp_retries": 1}}, clear=False), \
             patch("sms_tool.codex_oauth._poll_email_otp", return_value="123456"), \
             patch("sms_tool.codex_oauth.load_cached_sentinel", return_value={}), \
             patch("sms_tool.codex_oauth._next_url", return_value="https://auth.openai.com/about-you"), \
             patch(
                 "sms_tool.codex_oauth._follow_redirects",
                 return_value=(None, "https://auth.openai.com/about-you"),
             ), \
             patch("sms_tool.codex_oauth._complete_about_you", return_value=attempt), \
             patch("sms_tool.codex_oauth._finish_authorization") as finish:
            result = self._call(session)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], attempt["error"])
        self.assertEqual(result["about_you"], attempt)
        self.assertNotIn("terminal", result)
        # Authorization cannot be resumed from a page we know is a dead end.
        finish.assert_not_called()

    def test_other_landings_never_trigger_the_completion(self):
        session = self._session()
        with patch.dict(codex_oauth.CFG, {"email_registration": {"max_otp_retries": 1}}, clear=False), \
             patch("sms_tool.codex_oauth._poll_email_otp", return_value="123456"), \
             patch("sms_tool.codex_oauth.load_cached_sentinel", return_value={}), \
             patch("sms_tool.codex_oauth._next_url", return_value="https://auth.openai.com/consent"), \
             patch(
                 "sms_tool.codex_oauth._follow_redirects",
                 return_value=(None, "https://auth.openai.com/consent"),
             ), \
             patch("sms_tool.codex_oauth._complete_about_you") as about, \
             patch(
                 "sms_tool.codex_oauth._finish_authorization",
                 return_value={"ok": True, "tokens": {"access_token": "at", "refresh_token": "rt"}},
             ):
            result = self._call(session)

        self.assertTrue(result["ok"])
        self.assertNotIn("completed_about_you", result)
        about.assert_not_called()


class LiveSessionShortCircuitTests(unittest.TestCase):
    """The claim that lets the registration lane collect a refresh token for free.

    ``collect_codex_oauth_tokens`` follows the authorize URL first.  When the
    redirect already carries ``code`` and ``state`` -- which is what a *live*
    session produces -- it exchanges the callback and returns before the login
    stage machine is entered at all: no email code, no browser.

    That claim is load-bearing: ``registration.obtain_refresh_token`` (opt-in,
    default off) is only cheap because of it, and the docstring of
    ``RegistrationEmailWorkflow.obtain_oauth_refresh_token`` says so.  It was
    resting on a code read -- no test drove *this* entry point with a callback
    URL, and the only codex_oauth run in the logs took the ``email_otp`` path
    (it ran on an already-dead session, so the sample could not have shown
    otherwise).  These tests are the evidence.
    """

    CALLBACK_URL = "http://localhost:1455/auth/callback?code=abc&state=s"

    def test_a_live_session_exchanges_without_entering_the_login_stages(self):
        with patch("sms_tool.codex_oauth.select_auth_fingerprint"), \
             patch("sms_tool.codex_oauth._follow_redirects", return_value=(None, self.CALLBACK_URL)), \
             patch(
                 "sms_tool.codex_oauth._exchange_callback",
                 return_value={"access_token": "at", "refresh_token": "rt"},
             ) as exchange, \
             patch("sms_tool.codex_oauth._login_and_exchange") as login, \
             patch("sms_tool.codex_oauth._run_protocol_login_stages") as stages, \
             patch("sms_tool.codex_oauth._passwordless_login_and_exchange") as passwordless:
            result = codex_oauth.collect_codex_oauth_tokens(
                data={"email": "user@example.com", "cookie_header": "c"},
                session=Mock(),
                proxy=None,
                force_email_otp_login=False,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["tokens"]["refresh_token"], "rt")
        self.assertEqual(result["login_stage"], "live_session")
        exchange.assert_called_once()
        # The OTP machinery is the expensive part; none of it may run.
        login.assert_not_called()
        stages.assert_not_called()
        passwordless.assert_not_called()

    def test_a_session_without_a_callback_code_takes_the_expensive_path(self):
        # The negative case.  Without it the test above cannot distinguish "the
        # short circuit fired" from "nothing in that area runs from here".
        with patch("sms_tool.codex_oauth.select_auth_fingerprint"), \
             patch(
                 "sms_tool.codex_oauth._follow_redirects",
                 return_value=(None, "https://auth.openai.com/log-in"),
             ), \
             patch(
                 "sms_tool.codex_oauth._login_and_exchange",
                 return_value={"ok": True, "tokens": {"refresh_token": "rt2"}},
             ) as login, \
             patch("sms_tool.codex_oauth._exchange_callback") as exchange:
            result = codex_oauth.collect_codex_oauth_tokens(
                data={"email": "user@example.com"},
                session=Mock(),
                proxy=None,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["login_stage"], "login_required")
        login.assert_called_once()
        exchange.assert_not_called()

    def test_the_callback_signature_needs_both_parameters(self):
        # ``_has_callback_code`` is the gate the short circuit hangs on, so pin
        # its boundary: either parameter alone is not a callback.
        self.assertTrue(codex_oauth._has_callback_code(self.CALLBACK_URL))
        self.assertFalse(codex_oauth._has_callback_code("http://x/auth/callback?code=abc"))
        self.assertFalse(codex_oauth._has_callback_code("http://x/auth/callback?state=s"))
        self.assertFalse(codex_oauth._has_callback_code(""))
        self.assertFalse(codex_oauth._has_callback_code(None))

    def test_a_failure_after_walking_the_stages_still_says_so(self):
        # Worst case for the opt-in switch: the email code was spent and the
        # run still failed.  Marking only the *successful* login branch would
        # leave this result with no ``login_stage`` at all, so the case that
        # argues hardest against turning the switch on would be the one case
        # the log cannot show.
        with patch("sms_tool.codex_oauth.select_auth_fingerprint"), \
             patch(
                 "sms_tool.codex_oauth._follow_redirects",
                 return_value=(None, "https://auth.openai.com/log-in"),
             ), \
             patch(
                 "sms_tool.codex_oauth._login_and_exchange",
                 return_value={"ok": False, "error": "email_otp_poll_timeout"},
             ):
            result = codex_oauth.collect_codex_oauth_tokens(
                data={"email": "user@example.com"},
                session=Mock(),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["login_stage"], "login_required")

    def test_the_two_costs_survive_into_the_summary(self):
        # The field only earns its keep if it survives into what is actually
        # printed and persisted: ``obtain_oauth_refresh_token`` logs
        # ``_oauth_result_summary(s.oauth_result)`` and ``finalize`` stores the
        # same summary under ``codex_oauth``.  A field the summary drops is the
        # same as no field at all.
        live = {
            "ok": True,
            "mode": "codex_oauth_pkce",
            "login_stage": "live_session",
            "tokens": {"access_token": "at", "refresh_token": "rt"},
        }
        spent = {
            "ok": True,
            "mode": "codex_oauth_pkce",
            "login_stage": "login_required",
            "tokens": {"refresh_token": "rt"},
        }
        self.assertEqual(_oauth_result_summary(live)["login_stage"], "live_session")
        self.assertEqual(_oauth_result_summary(spent)["login_stage"], "login_required")
        # ...while the tokens themselves must not leak into the summary.
        self.assertNotIn("tokens", _oauth_result_summary(live))

    def test_a_missing_email_never_touches_the_network(self):
        with patch("sms_tool.codex_oauth.select_auth_fingerprint"), \
             patch("sms_tool.codex_oauth._follow_redirects") as follow:
            result = codex_oauth.collect_codex_oauth_tokens(data={}, session=Mock())

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_email")
        follow.assert_not_called()


class AboutYouResponseSurfaceTests(unittest.TestCase):
    """The no-continue-url branch reports the answer's *shape*, not the answer.

    ``_complete_about_you``'s failure dict is folded into the caller's result
    under ``about_you`` (``codex_oauth.py:504`` / ``:544``), and
    ``_oauth_result_summary`` keeps every key except ``tokens`` -- so whatever
    that branch returns lands on the ``obtain_oauth_refresh_token`` log line and
    in the ``finalize`` payload.  Returning the raw response body therefore put
    a whole document on one line: measured 2026-09-14 with
    ``runtime/_probe_oauth_summary_surface.py``, a 3 KB body rendered a
    3418-character summary.
    """

    URL = "https://auth.openai.com/about-you"

    def _about_you(self, response):
        with patch("sms_tool.codex_oauth.issue_sentinel_flow") as sentinel, \
             patch("sms_tool.codex_oauth.request_with_retry", return_value=response):
            sentinel.return_value = Mock(token="t", so_token="s")
            return codex_oauth._complete_about_you(Mock(), "did", self.URL)

    @staticmethod
    def _response(payload=None, *, text="", explode=False):
        response = Mock()
        response.status_code = 200
        response.text = text
        if explode:
            response.json.side_effect = ValueError("not json")
        else:
            response.json.return_value = payload
        return response

    def test_the_body_is_reduced_to_its_key_names(self):
        # The keys are deliberately *not* in alphabetical order.  With an
        # already-sorted body, ``sorted(body)`` and ``list(body)`` produce the
        # same list and the mutant that drops the sort survives -- which is
        # exactly what happened on the first run of
        # ``runtime/_mut_about_you_body_surface.py``.
        result = self._about_you(self._response({"zeta": 1, "alpha": "x" * 3000}))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "about_you_no_continue_url")
        self.assertNotIn("body", result)
        self.assertEqual(result["body_digest"], {"keys": ["alpha", "zeta"]})

    def test_the_raw_text_never_reaches_the_summary(self):
        # The assertion is on the *summary*, because that is the thing that is
        # actually printed and persisted -- a raw body dropped before the
        # summary is the same as one dropped after it, and only this catches
        # a future branch that puts it back.
        result = self._about_you(self._response({"detail": "SECRET" * 500}))
        rendered = repr(_oauth_result_summary({**result, "about_you": result}))

        self.assertNotIn("SECRET", rendered)
        self.assertIn("body_digest", rendered)

    def test_a_non_json_answer_keeps_its_truncated_text(self):
        # ``json()`` raising stores ``{"_raw": <first 300 chars>}``.  Keys alone
        # would say only "not JSON"; the text is what tells a Cloudflare block
        # page apart from an empty body, and it is already capped.
        result = self._about_you(self._response(explode=True, text="y" * 4000))

        self.assertNotIn("body", result)
        digest = result["body_digest"]
        self.assertEqual(sorted(digest), ["raw"])
        self.assertEqual(len(digest["raw"]), 300)
        self.assertEqual(digest["raw"], "y" * 300)

    def test_the_key_list_is_capped(self):
        # The whole point is an upper bound on one log line, so the bound needs
        # an owner: an answer with many keys must not reopen the door this
        # change closed.
        result = self._about_you(self._response({f"k{i}": i for i in range(40)}))

        self.assertEqual(len(result["body_digest"]["keys"]), 12)


if __name__ == "__main__":
    unittest.main()
