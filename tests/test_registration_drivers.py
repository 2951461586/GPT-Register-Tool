import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from sms_tool.batch_runner import run_batch_impl
from sms_tool.config import ConfigError, validate_registration_driver_config
from sms_tool.registration_drivers.base import normalize_registration_driver
from sms_tool.registration_drivers.browser_session import _playwright_proxy
from sms_tool.registration_drivers.playwright import build_browser_session_file
from sms_tool.registration_outcome import _browser_mailbox_snapshot
from sms_tool.registration import run_email


class RegistrationDriverTests(unittest.TestCase):
    @staticmethod
    def _session_kwargs():
        return {
            "proxy": None,
            "headless": True,
            "timeout_ms": 5_000,
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }

    def test_protocol_is_default_and_reference_aliases_normalize(self):
        self.assertEqual(normalize_registration_driver(), "protocol")
        self.assertEqual(normalize_registration_driver(config={"registration": {"driver": "playwright"}}), "playwright")
        for alias in ("protocol", "api", "http"):
            with self.subTest(alias=alias):
                self.assertEqual(normalize_registration_driver(alias), "protocol")
        for alias in ("browser", "fingerprint", "fingerprint_browser", "roxybrowser"):
            with self.subTest(alias=alias):
                self.assertEqual(normalize_registration_driver(alias), "roxy")

    def test_unknown_driver_is_not_silently_downgraded_to_protocol(self):
        with self.assertRaisesRegex(ValueError, "unsupported registration driver: mystery"):
            normalize_registration_driver("mystery")

        with self.assertRaisesRegex(ValueError, "unsupported registration driver: mystery"):
            normalize_registration_driver(config={"registration": {"driver": "mystery"}})

    def test_playwright_proxy_uses_canonical_credentials(self):
        self.assertEqual(
            _playwright_proxy("http://user:pass@example.test:8080"),
            {"server": "http://example.test:8080", "username": "user", "password": "pass"},
        )

    def test_browser_result_uses_canonical_session_contract(self):
        session = build_browser_session_file({
            "email": "user@example.com",
            "password": "Password!1",
            "access_token": "at",
            "auth_session": {"user": {"email": "user@example.com"}},
            "registration_driver": "playwright",
        })
        self.assertEqual(session["email"], "user@example.com")
        self.assertEqual(session["access_token"], "at")
        self.assertEqual(session["registration_driver"], "playwright")

    def test_browser_result_persists_mode_and_diagnostics(self):
        diagnostics = {"url": "https://chatgpt.com/", "title": "ChatGPT"}
        session = build_browser_session_file({
            "email": "user@example.com",
            "registration_driver": "playwright",
            "registration_mode": "browser",
            "browser_diagnostics": diagnostics,
        })
        self.assertEqual(session["registration_mode"], "browser")
        self.assertEqual(session["registration_driver"], "playwright")
        self.assertEqual(session["browser_diagnostics"], diagnostics)

    def test_browser_result_persists_sanitized_proxy_audit(self):
        audit = {"pool_index": 1, "expected_country": "US", "actual_country": "US", "scheme": "http"}
        session = build_browser_session_file({
            "email": "user@example.com",
            "registration_driver": "cloak",
            "proxy_audit": audit,
        })
        self.assertEqual(session["proxy_audit"], audit)

    def test_browser_result_preserves_mailbox_credentials_for_session_artifact(self):
        session = build_browser_session_file({
            "email": "user@example.com",
            "access_token": "chatgpt-at",
            "mailbox": {
                "email": "user@example.com",
                "provider": "graph",
                "token": "mailbox-token",
                "refresh_token": "mailbox-refresh-token",
            },
        })

        self.assertEqual(session["mailbox"]["token"], "mailbox-token")
        self.assertEqual(session["mailbox"]["refresh_token"], "mailbox-refresh-token")

    def test_browser_mailbox_snapshot_exposes_only_non_secret_metadata(self):
        mailbox = SimpleNamespace(
            email="user@example.com",
            source="pool",
            provider="graph",
            order_no="order-1",
            auth_mode="oauth_refresh",
            purchase_id="purchase-1",
            project_name="project-1",
            price="0.10",
            password="mail-password",
            login_password="login-password",
            refresh_token="mail-refresh-token",
            access_token="mail-access-token",
            token="provider-token",
            client_secret="client-secret",
            seen_message_id="message-1",
            message_content="verification code 123456",
            otp="123456",
            proxy="http://user:pass@proxy.test:8080",
        )

        snapshot = _browser_mailbox_snapshot(mailbox)

        self.assertEqual(snapshot, {
            "email": "user@example.com",
            "source": "pool",
            "provider": "graph",
            "order_no": "order-1",
            "auth_mode": "oauth_refresh",
            "purchase_id": "purchase-1",
            "project_name": "project-1",
            "price": "0.10",
        })
        self.assertFalse({
            "password", "login_password", "refresh_token", "access_token",
            "token", "client_secret", "seen_message_id", "message_content",
            "otp", "proxy",
        } & snapshot.keys())

    def test_example_config_uses_runtime_roxy_api_default(self):
        config_path = Path(__file__).resolve().parents[1] / "config.example.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        # 50000 is the correct Roxy default (50100 was a stale port that pointed
        # at a dead endpoint); the example file must match the runtime default.
        self.assertEqual(
            config["registration"]["drivers"]["roxy"]["api_base"],
            "http://127.0.0.1:50000",
        )

    def test_browser_registration_rejects_protocol_and_unknown_driver(self):
        from sms_tool.registration_drivers.playwright import run_browser_registration
        mailbox = type("Mailbox", (), {"email": "user@example.com"})()
        config = {"chatgpt": {"chat_base_url": "https://chatgpt.com"}, "registration": {}}
        for driver in ("protocol", "not-a-driver"):
            with self.subTest(driver=driver):
                result = run_browser_registration(
                    driver_name=driver, proxy=None, password="Password!1", mailbox=mailbox, config=config,
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["failure_class"], "configuration")

    def test_browser_driver_credentials_accept_environment_overrides(self):
        config = {"registration": {"drivers": {"roxy": {}}}}
        with patch.dict(os.environ, {"ROXY_WORKSPACE_ID": "env-workspace"}, clear=False):
            self.assertEqual(validate_registration_driver_config(config, "roxy"), "roxy")


    @patch("sms_tool.registration_drivers.playwright.run_browser_registration")
    def test_run_email_dispatches_every_explicit_browser_driver(self, browser_run):
        browser_run.return_value = {"success": False, "error": "manual_challenge_required"}
        mailbox = type("Mailbox", (), {"email": "user@example.com"})()
        for driver in ("playwright", "roxy", "cloak", "camoufox"):
            with self.subTest(driver=driver):
                browser_run.reset_mock()
                result = run_email(
                    mailbox=mailbox,
                    registration_driver=driver,
                    runtime_config={
                        "chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"},
                        "registration": {
                            "driver": "protocol",
                            "drivers": {
                                "roxy": {"workspace_id": "7"},
                            },
                        },
                    },
                )
                self.assertEqual(result["error"], "manual_challenge_required")
                browser_run.assert_called_once()
                self.assertEqual(browser_run.call_args.kwargs["driver_name"], driver)
                self.assertIsNone(browser_run.call_args.kwargs["probe_fn"])

    @patch("sms_tool.registration_drivers.playwright.run_browser_registration")
    def test_run_email_rejects_missing_driver_credentials_before_browser_dispatch(self, browser_run):
        mailbox = type("Mailbox", (), {"email": "user@example.com"})()
        expected = {
            "roxy": "roxy_workspace_id_missing",
        }
        for driver, error_code in expected.items():
            with self.subTest(driver=driver):
                browser_run.reset_mock()
                with self.assertRaisesRegex(ConfigError, f"^{error_code}$"):
                    run_email(
                        mailbox=mailbox,
                        registration_driver=driver,
                        runtime_config={
                            "chatgpt": {
                                "auth_base_url": "https://auth.openai.com",
                                "chat_base_url": "https://chatgpt.com",
                            },
                            "registration": {"driver": "protocol"},
                        },
                    )
                browser_run.assert_not_called()

    @patch("sms_tool.registration_drivers.playwright.create_browser_session")
    def test_missing_playwright_dependency_is_sanitized(self, session_cls):
        session_cls.side_effect = RuntimeError("browser_dependency_missing:playwright")
        mailbox = type("Mailbox", (), {
            "email": "user@example.com",
            "provider": "graph",
            "password": "mail-password",
            "refresh_token": "mail-refresh-token",
            "token": "provider-token",
        })()
        result = __import__("sms_tool.registration_drivers.playwright", fromlist=["run_playwright_registration"]).run_playwright_registration(
            proxy=None,
            password="Password!1",
            mailbox=mailbox,
            config={"chatgpt": {"chat_base_url": "https://chatgpt.com"}, "registration": {}},
            session_factory=session_cls,
        )
        self.assertFalse(result["success"])
        self.assertIn("browser_dependency_missing", result["error"])
        self.assertEqual(result["mailbox"], {"email": "user@example.com", "provider": "graph"})

    def test_explicit_protocol_driver_overrides_browser_config_in_batch(self):
        calls = []

        def run_email(**kwargs):
            calls.append(kwargs)
            return {"success": True, "email": kwargs["mailbox"].email}

        with patch("sms_tool.batch_runner.CFG", {
            "registration": {"driver": "roxy"},
            "email_registration": {},
        }):
            result = run_batch_impl(
                count=1,
                workers=1,
                mailboxes=[SimpleNamespace(email="user@example.com")],
                registration_driver="protocol",
                run_email_func=run_email,
            )

        self.assertTrue(result[0]["success"])
        self.assertEqual(calls[0]["registration_driver"], "protocol")

    @patch("sms_tool.batch_runner.infer_proxy_country", side_effect=["US", "DE"])
    @patch("sms_tool.batch_runner.refresh_proxy_sid", side_effect=lambda value: value + "-sid")
    @patch("sms_tool.batch_runner.select_registration_proxy_pool", side_effect=lambda pool, _fallback: pool)
    def test_roxy_country_mismatch_keeps_pinned_proxy_on_retry(
        self, _select_pool, _refresh_sid, _infer_country,
    ):
        # Audit #4: a retry must NOT rotate to a different pool member -- that
        # egress churn is a ban trigger.  The account stays pinned to pool[0];
        # on retry only the sticky session id is refreshed.
        calls = []

        def run_email(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "success": False,
                    "error": "roxy_proxy_country_mismatch:country_mismatch:DE",
                    "failure_class": "network",
                }
            return {"success": True}

        result = run_batch_impl(
            count=1, workers=1, max_attempts=2, retry_delay_seconds=0,
            proxy_pool=["http://pool-a.test:8000", "http://pool-b.test:8000"],
            registration_driver="roxy", run_email_func=run_email,
        )

        self.assertTrue(result[0]["success"])
        # Both attempts stay on the pinned pool[0] egress (only the sid is
        # refreshed each time, which the patched refresh_proxy_sid makes a
        # no-op here).
        self.assertEqual([call["proxy"] for call in calls], [
            "http://pool-a.test:8000-sid", "http://pool-a.test:8000-sid",
        ])
        self.assertEqual(calls[0]["proxy_metadata"]["pool_index"], 0)
        self.assertEqual(calls[1]["proxy_metadata"]["pool_index"], 0)
        self.assertEqual(calls[0]["proxy_metadata"]["expected_country"], "US")
        self.assertNotIn("proxy", calls[0]["proxy_metadata"])


if __name__ == "__main__":
    unittest.main()
