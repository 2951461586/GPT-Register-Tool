import unittest
from unittest.mock import Mock, patch

from sms_tool import codex_oauth


class CodexOauthTests(unittest.TestCase):
    def test_account_deactivated_response_is_terminal(self):
        body = '{"error":{"code":"account_deactivated","message":"You do not have an account because it has been deleted or deactivated."}}'

        self.assertTrue(codex_oauth._is_account_deactivated_response(403, body))
        self.assertFalse(codex_oauth._is_account_deactivated_response(401, body))
        self.assertFalse(codex_oauth._is_account_deactivated_response(403, '{"error":"wrong code"}'))

    def test_phone_verification_url_detection(self):
        self.assertTrue(codex_oauth._needs_phone_verification("https://auth.openai.com/add-phone"))
        self.assertTrue(codex_oauth._needs_phone_verification("https://auth.openai.com/phone-verification"))
        self.assertFalse(codex_oauth._needs_phone_verification("https://auth.openai.com/consent"))

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

    def test_password_stage_preserves_passwordless_fallback_failure(self):
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
        self.assertEqual(result["fallback_from"], "password_login_failed")
        self.assertEqual(result["password_attempt"]["error"], "password_verify_failed:400")

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


if __name__ == "__main__":
    unittest.main()
