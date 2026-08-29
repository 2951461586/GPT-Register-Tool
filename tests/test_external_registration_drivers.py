import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from sms_tool import env_loader
from sms_tool.config import ConfigError, validate_config
from sms_tool.registration_drivers.base import BrowserRegistrationError, normalize_registration_driver
from sms_tool.registration_drivers.external_sessions import (
    CloakBrowserSession,
    RoxyBrowserSession,
    _driver_config,
    create_browser_session,
    verify_browser_proxy_country,
)
from sms_tool.registration_drivers.browser_session import PlaywrightBrowserSession
from sms_tool.registration_drivers.stealth import apply_playwright_stealth
from sms_tool.registration_drivers.playwright import (
    _browser_access_token_probe,
    _browser_heartbeat,
    _click_continue,
    _click_passwordless_otp,
    _complete_profile,
    _browser_failure_class,
    _fill_email,
    _fill_password_if_present,
    _first_visible,
    _manual_challenge,
    _maybe_accept_cookies,
    _maybe_dismiss_chatgpt_onboarding,
    _poll_browser_otp,
    _post_otp_registration_state,
    _profile_completion_required,
    _quick_auth_state,
    _prepare_session_page,
    _restart_email_otp_flow,
    _safe_submit_email_form,
    _session_context_closed,
    _session_payload,
    _wait_for_profile_completion,
    _wait_for_registration_state,
    _post_registration_dwell,
    run_browser_registration,
)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class _Context:
    def __init__(self):
        self.pages = [SimpleNamespace()]
        self.closed = False
        self.timeout = None

    def set_default_timeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class _Browser:
    def __init__(self):
        self.contexts = [_Context()]
        self.closed = False

    def close(self):
        self.closed = True


class _Playwright:
    def __init__(self):
        self.browser = _Browser()
        self.connections = []
        self.stopped = False
        self.chromium = self

    def connect_over_cdp(self, address, **kwargs):
        self.connections.append((address, kwargs))
        return self.browser

    def stop(self):
        self.stopped = True


class _FlowPage:
    url = "https://chatgpt.com/"

    def goto(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, *_args, **_kwargs):
        return None

    def title(self):
        return "ChatGPT"


class _FlowSession:
    def __init__(self):
        self.page = _FlowPage()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def add_device_cookie(self, *_args):
        return None

    def cookie_header(self):
        return "session=redacted"


class _Page:
    def __init__(self, url):
        self.url = url
        self.goto_calls = []
        self.evaluate_calls = []

    def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        self.url = url

    def evaluate(self, script, arg=None):
        self.evaluate_calls.append((script, arg))
        return {"status": 200, "body": {}}


class _RequestResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def json(self):
        return self.payload

    def text(self):
        return "{}"


class ExternalRegistrationDriverTests(unittest.TestCase):
    def _session_kwargs(self):
        return {
            "proxy": None,
            "headless": True,
            "timeout_ms": 10_000,
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }

    def test_browser_proxy_country_audit_is_country_only(self):
        page = MagicMock()
        page.evaluate.return_value = {"country": "US", "status": 200}
        browser = SimpleNamespace(page=page)
        result = verify_browser_proxy_country(
            browser,
            expected_country="us",
        )
        self.assertEqual(result, {"ok": True, "actual_country": "US"})
        self.assertNotIn("proxy", result)
        self.assertNotIn("password", result)

    @patch("sms_tool.registration_drivers.playwright.random.uniform", return_value=21.5)
    @patch("sms_tool.registration_drivers.playwright.time.sleep")
    def test_post_registration_dwell_matches_reference_range(self, sleep, _uniform):
        seconds = _post_registration_dwell({
            "registration": {"post_registration_dwell_seconds_range": "18,45"},
        })
        self.assertEqual(seconds, 21.5)
        sleep.assert_called_once_with(21.5)

    def test_browser_proxy_country_audit_rejects_mismatch_without_ip(self):
        page = MagicMock()
        page.evaluate.return_value = {"country": "DE", "status": 200}
        result = verify_browser_proxy_country(
            SimpleNamespace(page=page),
            expected_country="US",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["actual_country"], "DE")
        self.assertIn("country_mismatch", result["error"])
        self.assertNotIn("proxy", str(result).lower())
        self.assertNotIn("password", str(result).lower())

    def test_all_driver_aliases_normalize(self):
        aliases = {
            "browser": "roxy",
            "fingerprint": "roxy",
            "roxybrowser": "roxy",
            "cloak-browser": "cloak",
            "camou": "camoufox",
        }
        for alias, expected in aliases.items():
            with self.subTest(alias=alias):
                self.assertEqual(normalize_registration_driver(alias), expected)

    def test_optional_playwright_stealth_applies_to_context_and_page(self):
        class FakeStealth:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.targets = []

            def apply_stealth_sync(self, target):
                self.targets.append(target)
                target.add_init_script("stealth")

        class Target:
            def __init__(self):
                self.scripts = []

            def add_init_script(self, script):
                self.scripts.append(script)

        fake_module = SimpleNamespace(Stealth=FakeStealth)
        context = Target()
        page = Target()
        with patch.dict(sys.modules, {"playwright_stealth": fake_module}):
            result = apply_playwright_stealth(context, page, label="test", provider_prefix="camoufox")

        self.assertTrue(result["playwright_stealth"])
        self.assertEqual(context.scripts, ["stealth"])
        self.assertEqual(page.scripts, ["stealth"])

    def test_missing_playwright_stealth_is_non_terminal(self):
        with patch.dict(sys.modules, {"playwright_stealth": None}):
            result = apply_playwright_stealth(SimpleNamespace(), None)
        self.assertFalse(result["playwright_stealth"])
        self.assertEqual(result["reason"], "dependency_missing_or_unavailable")

    def test_local_playwright_session_records_stealth_status(self):
        session = PlaywrightBrowserSession()
        context = SimpleNamespace(pages=[], set_default_timeout=MagicMock(), new_page=MagicMock())
        page = context.new_page.return_value
        fake = _Playwright()
        fake.browser.new_context = MagicMock(return_value=context)
        session._playwright = fake
        session.browser = fake.browser
        with patch("sms_tool.registration_drivers.browser_session.apply_playwright_stealth", return_value={"playwright_stealth": True}) as apply:
            session.context = context
            session.page = page
            session.stealth_status = apply(context, page, label="playwright", provider_prefix="playwright")
        self.assertTrue(session.stealth_status["playwright_stealth"])
        apply.assert_called_once()


    def test_cloud_access_token_probe_uses_browser_context_and_redacts_errors(self):
        class Browser:
            def __init__(self):
                self.calls = []

            def fetch_json(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return {"status": 200, "body": {"quota": {"remaining": 7, "limit": 10}}}

        browser = Browser()
        result = _browser_access_token_probe(
            browser,
            {"access_token": "at-secret", "auth_session": {}},
            timeout=9,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "browser")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(browser.calls[0][0], "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(browser.calls[0][1]["timeout_ms"], 9_000)
        self.assertEqual(browser.calls[0][1]["headers"]["Authorization"], "Bearer at-secret")

    def test_cloud_access_token_probe_handles_missing_token_without_browser_call(self):
        class Browser:
            def fetch_json(self, *_args, **_kwargs):
                raise AssertionError("browser probe should not be called by this test")

        result = _browser_access_token_probe(
            Browser(),
            {"access_token": ""},
            timeout=9,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 0)

    def test_session_factory_selects_each_independent_driver(self):
        expected = {
            "roxy": RoxyBrowserSession,
            "cloak": CloakBrowserSession,
        }
        for driver, session_type in expected.items():
            with self.subTest(driver=driver):
                session = create_browser_session(driver, config={"registration": {}}, **self._session_kwargs())
                self.assertIsInstance(session, session_type)

    def test_session_factory_rejects_protocol_and_unknown_driver(self):
        with self.assertRaises(BrowserRegistrationError) as protocol_error:
            create_browser_session("protocol", config={"registration": {}}, **self._session_kwargs())
        self.assertEqual(protocol_error.exception.code, "unsupported_registration_driver")
        with self.assertRaises(BrowserRegistrationError) as unknown_error:
            create_browser_session("not-a-driver", config={"registration": {}}, **self._session_kwargs())
        self.assertEqual(unknown_error.exception.code, "unsupported_registration_driver")



    def test_invalid_boolean_environment_override_is_ignored(self):
        config = {"registration": {"drivers": {"roxy": {"keep_browser_open": True}}}}
        with patch.dict(os.environ, {"ROXY_KEEP_BROWSER_OPEN": "not-a-boolean"}, clear=False):
            result = _driver_config(config, "roxy")

        self.assertTrue(result["keep_browser_open"])

    def test_project_env_file_loads_without_overriding_process_environment(self):
        original_loaded = env_loader._LOADED
        original_path = env_loader.ENV_PATH
        try:
            with TemporaryDirectory() as directory:
                env_path = Path(directory) / ".env"
                env_path.write_text(
                    'export ROXY_API_TOKEN="file-key"\n'
                    "ROXY_API_BASE=http://from-file\n"
                    "BROKEN KEY=value\n",
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {"ROXY_API_TOKEN": "process-key"}, clear=True):
                    env_loader._LOADED = False
                    env_loader.ENV_PATH = env_path
                    env_loader.ensure_loaded()
                    self.assertEqual(os.environ["ROXY_API_TOKEN"], "process-key")
                    self.assertEqual(os.environ["ROXY_API_BASE"], "http://from-file")
                    self.assertEqual(
                        _driver_config(
                            {"registration": {"drivers": {"roxy": {}}}},
                            "roxy",
                        )["api_token"],
                        "process-key",
                    )
                    self.assertNotIn("BROKEN KEY", os.environ)
        finally:
            env_loader._LOADED = original_loaded
            env_loader.ENV_PATH = original_path


    @patch("sms_tool.registration_drivers.external_sessions.curl_requests.request")
    def test_roxy_create_open_close_delete_lifecycle(self, request):
        request.side_effect = [
            _Response({"data": {"dirId": "42"}}),
            _Response({"data": {"ws": "ws://127.0.0.1:9222/devtools/browser/test"}}),
            _Response({"ok": True}),
            _Response({"ok": True}),
        ]
        config = {"registration": {"drivers": {"roxy": {
            "workspace_id": "7",
            "project_id": "8",
            "delete_profile_after_run": True,
        }}}}
        session = RoxyBrowserSession(
            config=config,
            proxy="http://user%40name:pass%3Aword@example.test:8080",
            **{key: value for key, value in self._session_kwargs().items() if key != "proxy"},
        )
        fake = _Playwright()
        session._start_playwright = lambda: setattr(session, "_playwright", fake)

        self.assertIs(session.__enter__(), session)
        session.close()

        self.assertEqual(request.call_count, 4)
        create_body = request.call_args_list[0].kwargs["json"]
        self.assertEqual(create_body["workspaceId"], 7)
        self.assertEqual(create_body["projectId"], 8)
        self.assertEqual(create_body["proxyInfo"]["proxyUserName"], "user@name")
        self.assertEqual(create_body["proxyInfo"]["proxyPassword"], "pass:word")
        self.assertTrue(request.call_args_list[1].args[1].endswith("/browser/open"))
        self.assertTrue(request.call_args_list[2].args[1].endswith("/browser/close"))
        self.assertTrue(request.call_args_list[3].args[1].endswith("/browser/delete"))
        self.assertEqual(request.call_args_list[3].kwargs["json"]["dirIds"], [42])
        self.assertEqual(fake.connections[0][0], "ws://127.0.0.1:9222/devtools/browser/test")

    @patch("sms_tool.registration_drivers.external_sessions.curl_requests.request")
    def test_roxy_get_methods_use_query_params_and_extract_reference_keys(self, request):
        request.side_effect = [
            _Response({"data": {"dir_id": "42"}}),
            _Response({"data": {"debugAddress": ":9222"}}),
            _Response({"ok": True}),
            _Response({"success": True}),
        ]
        config = {"registration": {"drivers": {"roxy": {
            "api_base": "https://roxy.test",
            "workspace_id": "7",
            "create_method": "GET",
            "open_method": "GET",
            "close_method": "GET",
            "delete_method": "GET",
            "delete_profile_after_run": True,
        }}}}
        session = RoxyBrowserSession(config=config, **self._session_kwargs())
        fake = _Playwright()
        session._start_playwright = lambda: setattr(session, "_playwright", fake)

        self.assertIs(session.__enter__(), session)
        session.close()

        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "GET", "GET", "GET"])
        self.assertEqual(request.call_args_list[1].kwargs["params"], {
            "workspaceId": 7, "dirId": 42, "args": [], "forceOpen": True, "headless": True,
        })
        self.assertNotIn("json", request.call_args_list[1].kwargs)
        self.assertEqual(request.call_args_list[2].kwargs["params"], {"workspaceId": 7, "dirId": 42})
        self.assertEqual(request.call_args_list[3].kwargs["params"], {"workspaceId": 7, "dirIds": [42]})
        self.assertEqual(session.debugger_address, ":9222")
        self.assertEqual(fake.connections[0][0], "http://127.0.0.1:9222")

    @patch("sms_tool.registration_drivers.external_sessions.curl_requests.request")
    def test_roxy_response_code_failure_is_classified_without_raw_message(self, request):
        request.return_value = _Response({"code": 1001, "message": "token=secret"})
        session = RoxyBrowserSession(
            config={"registration": {"drivers": {"roxy": {"workspace_id": "7"}}}},
            **self._session_kwargs(),
        )
        session.api_base = "https://roxy.test"

        with self.assertRaises(BrowserRegistrationError) as raised:
            session._request("POST", "/browser/open", {})
        self.assertEqual(raised.exception.code, "roxy_api_error")
        self.assertEqual(raised.exception.detail, "response_code_invalid")
        self.assertNotIn("secret", str(raised.exception))

    @patch("sms_tool.registration_drivers.external_sessions.time.sleep")
    @patch("sms_tool.registration_drivers.external_sessions.curl_requests.request")
    def test_roxy_transient_api_failure_retries_non_create_request(self, request, sleep):
        request.side_effect = [_Response({}, status_code=503), _Response({"ok": True})]
        session = RoxyBrowserSession(
            config={"registration": {"drivers": {"roxy": {
                "workspace_id": "7", "api_retries": 2, "api_retry_delay_seconds": 0,
            }}}},
            **self._session_kwargs(),
        )
        session.api_base = "https://roxy.test"

        self.assertEqual(session._request("GET", "/browser/open", {}), {"ok": True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0)


    def test_cloak_dependency_failure_is_lazy_and_sanitized(self):
        session = CloakBrowserSession(config={"registration": {}}, **self._session_kwargs())
        with patch.dict(sys.modules, {"cloakbrowser": None}):
            with self.assertRaises(BrowserRegistrationError) as raised:
                session.__enter__()
        self.assertEqual(raised.exception.code, "browser_dependency_missing")
        self.assertIn("cloakbrowser", str(raised.exception))

    @patch("sms_tool.registration_drivers.external_sessions.apply_playwright_stealth", return_value={"playwright_stealth": True})
    def test_cloak_geoip_does_not_get_overridden_by_global_locale_defaults(self, _stealth):
        context = MagicMock()
        page = MagicMock()
        context.pages = []
        context.new_page.return_value = page
        browser = MagicMock()
        browser.new_context.return_value = context
        cloak_module = SimpleNamespace(
            launch=MagicMock(return_value=browser),
            launch_persistent_context=MagicMock(),
        )
        session = CloakBrowserSession(
            config={"registration": {"drivers": {"cloak": {"geoip": True}}}},
            **self._session_kwargs(),
        )

        with patch.dict(sys.modules, {"cloakbrowser": cloak_module}):
            self.assertIs(session.__enter__(), session)

        cloak_module.launch.assert_called_once_with(headless=True, humanize=True, geoip=True)
        browser.new_context.assert_called_once_with()

    def test_invalid_nested_driver_config_is_rejected(self):
        with self.assertRaises(ConfigError) as raised:
            validate_config({
                "chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"},
                "registration": {"drivers": {"roxy": "invalid"}},
            })
        self.assertIn("registration.drivers.roxy must be an object", str(raised.exception))

    def test_manual_challenge_and_session_email_mismatch_are_terminal(self):
        body = MagicMock()
        body.inner_text.return_value = "Please verify you are human"
        page = MagicMock()
        page.locator.return_value = body
        self.assertTrue(_manual_challenge(page))

        browser = MagicMock()
        browser.fetch_json.return_value = {
            "status": 200,
            "body": {"accessToken": "at", "user": {"email": "other@example.com"}},
        }
        with self.assertRaises(BrowserRegistrationError) as raised:
            _session_payload(browser, "https://chatgpt.com", "user@example.com", timeout_seconds=1)
        self.assertEqual(raised.exception.code, "browser_session_email_mismatch")

    def test_session_payload_retries_transient_empty_session(self):
        browser = MagicMock()
        browser.fetch_json.side_effect = [
            {"status": 200, "body": {"user": {"email": "user@example.com"}}},
            {"status": 503, "body": {"error": "temporarily unavailable"}},
            {"status": 200, "body": {"accessToken": "at", "user": {"email": "user@example.com"}}},
        ]
        with patch("sms_tool.registration_drivers.playwright.time.sleep") as sleep:
            result = _session_payload(browser, "https://chatgpt.com", "user@example.com", timeout_seconds=5)
        self.assertEqual(result["access_token"], "at")
        self.assertEqual(browser.fetch_json.call_count, 3)
        self.assertGreaterEqual(sleep.call_count, 1)

    def test_session_payload_supports_legacy_fetch_json_signature(self):
        class LegacyBrowser:
            def __init__(self):
                self.calls = []

            def fetch_json(self, url):
                self.calls.append(url)
                return {"status": 200, "body": {"accessToken": "at", "user": {"email": "user@example.com"}}}

        browser = LegacyBrowser()
        result = _session_payload(browser, "https://chatgpt.com", "user@example.com", timeout_seconds=1)

        self.assertEqual(result["access_token"], "at")
        self.assertEqual(browser.calls, ["https://chatgpt.com/api/auth/session"])

    def test_session_payload_retries_exception_then_success(self):
        browser = MagicMock()
        browser.fetch_json.side_effect = [
            RuntimeError("temporary transport failure"),
            {"status": 200, "body": {"accessToken": "at", "user": {"email": "user@example.com"}}},
        ]
        with patch("sms_tool.registration_drivers.playwright.time.sleep") as sleep:
            result = _session_payload(browser, "https://chatgpt.com", "user@example.com", timeout_seconds=5)

        self.assertEqual(result["access_token"], "at")
        self.assertEqual(browser.fetch_json.call_count, 2)
        self.assertGreaterEqual(sleep.call_count, 1)

    def test_session_payload_retries_transient_context_unavailable(self):
        browser = MagicMock()
        browser.fetch_json.side_effect = [
            {"status": 0, "body": {"error": "chatgpt_context_unavailable"}},
            {"status": 200, "body": {"accessToken": "at", "user": {"email": "user@example.com"}}},
        ]
        with patch("sms_tool.registration_drivers.playwright.time.sleep"):
            result = _session_payload(browser, "https://chatgpt.com", "user@example.com", timeout_seconds=5)
        self.assertEqual(result["access_token"], "at")
        self.assertEqual(browser.fetch_json.call_count, 2)

    def test_session_payload_ignores_stale_user_until_token_exists(self):
        browser = MagicMock()
        browser.fetch_json.side_effect = [
            {"status": 200, "body": {"user": {"email": "stale@example.com"}}},
            {"status": 200, "body": {"accessToken": "at", "user": {"email": "user@example.com"}}},
        ]
        with patch("sms_tool.registration_drivers.playwright.time.sleep"):
            result = _session_payload(browser, "https://chatgpt.com", "user@example.com", timeout_seconds=5)
        self.assertEqual(result["access_token"], "at")

    def test_session_payload_classifies_terminal_nextauth_errors(self):
        cases = (
            (401, {"error": "SessionRequired"}, "browser_session_unauthorized"),
            (403, {"error": "AccessDenied"}, "browser_session_access_denied"),
            (200, {"error": "OAuthAccountNotLinked"}, "browser_session_account_not_linked"),
            (200, {"error": "OAuthCallback"}, "browser_session_oauth_callback_failed"),
            (200, {"name": "OAuthCallbackError"}, "browser_session_oauth_callback_failed"),
            (200, {"message": "OAuthAccountNotLinked"}, "browser_session_account_not_linked"),
            (429, {"message": "Too many requests"}, "browser_session_rate_limited"),
        )
        for status, body, expected in cases:
            with self.subTest(expected=expected):
                browser = MagicMock()
                browser.fetch_json.return_value = {"status": status, "body": body}
                with self.assertRaises(BrowserRegistrationError) as raised:
                    _session_payload(browser, "https://chatgpt.com", "user@example.com", timeout_seconds=1)
                self.assertEqual(raised.exception.code, expected)

    def test_session_payload_retries_closed_target_then_fails_precisely(self):
        browser = MagicMock()
        browser.fetch_json.side_effect = RuntimeError("Target page, context or browser has been closed")
        with patch("sms_tool.registration_drivers.playwright.time.sleep"):
            with self.assertRaises(BrowserRegistrationError) as raised:
                _session_payload(browser, "https://chatgpt.com", "user@example.com", timeout_seconds=5)
        self.assertEqual(raised.exception.code, "browser_session_context_closed")

    def test_playwright_session_switches_callback_page_and_uses_relative_session_path(self):
        session = PlaywrightBrowserSession()
        auth = _Page("https://auth.openai.com/about-you")
        callback = _Page("https://chatgpt.com/")
        session.context = SimpleNamespace(pages=[auth, callback])
        session.page = auth

        payload = session.fetch_json("https://chatgpt.com/api/auth/session")

        self.assertEqual(payload["status"], 200)
        self.assertIs(session.page, callback)
        self.assertEqual(callback.evaluate_calls[0][1]["url"], "/api/auth/session")
        self.assertEqual(auth.goto_calls, [])

    def test_context_request_terminal_response_is_not_hidden_by_page_fallback(self):
        session = PlaywrightBrowserSession()
        page = _Page("https://chatgpt.com/")
        request = MagicMock()
        request.get.return_value = _RequestResponse({"error": "SessionRequired"}, status=401)
        session.context = SimpleNamespace(pages=[page], request=request)
        session.page = page

        payload = session.fetch_json("https://chatgpt.com/api/auth/session")

        self.assertEqual(payload, {"status": 401, "body": {"error": "SessionRequired"}})
        self.assertEqual(page.evaluate_calls, [])

    def test_live_page_selection_prefers_active_auth_step(self):
        session = PlaywrightBrowserSession()
        stale_chatgpt = _Page("https://chatgpt.com/")
        active_otp = _Page("https://auth.openai.com/email-verification")
        session.context = SimpleNamespace(pages=[stale_chatgpt, active_otp])
        session.page = stale_chatgpt

        selected = session.select_live_page()

        self.assertIs(selected, active_otp)
        self.assertIs(session.page, active_otp)

    def test_first_visible_waits_for_async_login_form(self):
        locator = MagicMock()
        locator.first = locator
        locator.is_visible.return_value = True
        page = MagicMock()
        page.locator.return_value = locator

        self.assertIs(_first_visible(page, ("input[type='email']",)), locator)
        locator.wait_for.assert_called_once_with(state="visible", timeout=5_000)

    def test_cookie_consent_is_dismissed_with_localized_structural_selector(self):
        locator = MagicMock()
        locator.first = locator
        locator.is_visible.return_value = True
        page = MagicMock()
        page.locator.return_value = locator

        self.assertTrue(_maybe_accept_cookies(page))
        selector = page.locator.call_args.args[0]
        self.assertIn("Accept all", selector)
        locator.click.assert_called_once_with(no_wait_after=True)

    def test_chatgpt_onboarding_dismissal_is_bounded(self):
        locator = MagicMock()
        locator.first = locator
        locator.is_visible.return_value = True
        page = MagicMock()
        page.url = "https://chatgpt.com/"
        page.locator.return_value = locator

        self.assertEqual(_maybe_dismiss_chatgpt_onboarding(page), 4)
        self.assertEqual(locator.click.call_count, 4)

    @patch("sms_tool.registration_drivers.playwright._first_visible")
    def test_continue_uses_exact_accessible_name(self, first_visible):
        button = MagicMock()
        button.is_visible.return_value = True
        page = MagicMock()
        page.get_by_role.return_value.first = button

        _click_continue(page)

        page.get_by_role.assert_called_once_with("button", name="Continue", exact=True)
        button.click.assert_called_once_with(no_wait_after=True)
        first_visible.assert_not_called()

    @patch("sms_tool.registration_drivers.playwright._first_visible", return_value=None)
    def test_continue_uses_structural_submit_fallback(self, _first_visible):
        button = MagicMock()
        button.is_visible.return_value = False
        page = MagicMock()
        page.get_by_role.return_value.first = button
        page.evaluate.return_value = True

        _click_continue(page)

        script = page.evaluate.call_args.args[0]
        self.assertIn("requestSubmit", script)
        self.assertIn("button[type=submit]", script)

    def test_profile_completion_does_not_treat_unknown_as_complete(self):
        page = MagicMock()
        with patch("sms_tool.registration_drivers.playwright.time.monotonic", side_effect=[0, 0, 2]), patch(
            "sms_tool.registration_drivers.playwright._quick_auth_state", return_value="unknown"
        ):
            self.assertFalse(_wait_for_profile_completion(page, timeout_seconds=1))

    def test_state_wait_uses_recovered_live_page(self):
        stale = MagicMock()
        live = MagicMock()
        browser = MagicMock()
        browser.select_live_page.return_value = live
        with patch("sms_tool.registration_drivers.playwright._quick_auth_state", return_value="otp") as quick:
            state = _wait_for_registration_state(stale, timeout_seconds=1, browser=browser)
        self.assertEqual(state, "otp")
        quick.assert_called_once_with(live)

    def test_prepare_session_page_waits_for_natural_callback(self):
        page = MagicMock()
        callback = MagicMock()
        browser = MagicMock()
        browser.page = callback
        browser.ensure_chatgpt_context.return_value = False

        selected = _prepare_session_page(browser, page, 90)

        self.assertIs(selected, callback)
        browser.ensure_chatgpt_context.assert_called_once_with(auto_jump_wait=15)

    def test_closed_target_class_names_are_recognized(self):
        self.assertTrue(_session_context_closed("NoSuchWindowException"))
        self.assertTrue(_session_context_closed("InvalidSessionIdException"))

    def test_quick_state_distinguishes_existing_login_password(self):
        page = MagicMock()
        page.evaluate.return_value = {
            "url": "https://auth.openai.com/log-in/password",
            "challenge": False,
            "otp": False,
            "profile": False,
            "password": True,
            "passwordAutocomplete": "current-password",
            "email": False,
        }
        self.assertEqual(_quick_auth_state(page), "login_password")

    @patch("sms_tool.registration_drivers.playwright._wait_for_registration_state", return_value="profile")
    def test_post_otp_state_reprobe_allows_profile_only(self, wait_state):
        page = MagicMock()
        state = _post_otp_registration_state(page, timeout_seconds=5)

        self.assertEqual(state, "profile")
        wait_state.assert_called_once_with(
            page, 5, browser=None, wait_for_otp_transition=True, config=None,
        )
        self.assertTrue(_profile_completion_required(state))

    @patch("sms_tool.registration_drivers.playwright._wait_for_registration_state", return_value="otp")
    @patch("sms_tool.registration_drivers.playwright._quick_auth_state", return_value="otp")
    def test_post_otp_state_reprobe_skips_stale_otp_on_authenticated_callback(self, _quick_state, _wait_state):
        page = MagicMock()
        page.url = "https://chatgpt.com/"

        state = _post_otp_registration_state(page, timeout_seconds=5)

        self.assertEqual(state, "authenticated")
        self.assertFalse(_profile_completion_required(state))

    def test_post_otp_state_reprobe_preserves_terminal_auth_states(self):
        expected = {
            "challenge": "manual_challenge_required",
            "identity_provider": "browser_unexpected_identity_provider",
            "login_password": "browser_existing_account",
            "unknown": "browser_registration_state_unknown",
        }
        for state, code in expected.items():
            with self.subTest(state=state), patch(
                "sms_tool.registration_drivers.playwright._wait_for_registration_state",
                return_value=state,
            ):
                with self.assertRaises(BrowserRegistrationError) as raised:
                    _profile_completion_required(
                        _post_otp_registration_state(MagicMock(), timeout_seconds=5)
                    )
                self.assertEqual(raised.exception.code, code)

    def test_browser_failure_classes_preserve_terminal_auth_and_mailbox_states(self):
        self.assertEqual(_browser_failure_class("browser_existing_account"), "account")
        self.assertEqual(_browser_failure_class("browser_email_otp_timeout"), "mailbox")
        self.assertEqual(_browser_failure_class("manual_challenge_required"), "auth_state")
        self.assertEqual(_browser_failure_class("browser_dependency_missing"), "configuration")
        self.assertEqual(_browser_failure_class("browser_session_context_closed"), "network")
        self.assertEqual(_browser_failure_class("browser_session_oauth_callback_failed"), "auth_state")
        self.assertEqual(_browser_failure_class("browser_session_rate_limited"), "rate_limit")
        self.assertEqual(_browser_failure_class("browser_otp_restart_state_unknown"), "auth_state")

    def test_safe_email_submit_uses_structural_filter(self):
        page = MagicMock()
        page.evaluate.return_value = {"ok": True}

        self.assertTrue(_safe_submit_email_form(page, "user@example.com"))

        script = page.evaluate.call_args.args[0]
        self.assertIn("google|apple|microsoft|github|facebook", script)
        self.assertIn("email_value_mismatch", script)
        self.assertIn("bad.test(attrText(form))", script)

    @patch("sms_tool.registration_drivers.playwright._quick_auth_state", return_value="login_password")
    def test_existing_login_password_is_not_overwritten(self, _state):
        page = MagicMock()
        page.url = "https://auth.openai.com/log-in/password"
        page.evaluate.return_value = False
        with self.assertRaises(BrowserRegistrationError) as raised:
            _fill_password_if_present(page, "Password!1")
        self.assertEqual(raised.exception.code, "browser_existing_account")

    @patch("sms_tool.registration_drivers.playwright._wait_for_registration_state", return_value="otp")
    @patch("sms_tool.registration_drivers.playwright._click_passwordless_otp", return_value=True)
    @patch("sms_tool.registration_drivers.playwright._quick_auth_state", return_value="login_password")
    def test_existing_login_password_can_switch_to_passwordless_otp(self, _state, click_otp, wait_state):
        page = MagicMock()
        page.url = "https://auth.openai.com/log-in/password"

        self.assertFalse(_fill_password_if_present(page, "Password!1"))

        click_otp.assert_called_once_with(page)
        wait_state.assert_called_once_with(page, 20, config=None)

    def test_email_poll_propagates_unexpected_identity_provider(self):
        class Field:
            def wait_for(self, **_kwargs):
                return None

            def fill(self, _value):
                return None

            def input_value(self):
                return "user@example.com"

        class Locator:
            first = Field()

        class Page:
            url = "https://accounts.google.com/o/oauth2/auth"

            def locator(self, _selector):
                return Locator()

            def wait_for_timeout(self, _timeout):
                return None

        page = Page()
        with patch("sms_tool.registration_drivers.playwright._safe_submit_email_form", return_value=False), patch(
            "sms_tool.registration_drivers.playwright._click_continue"
        ):
            with self.assertRaises(BrowserRegistrationError) as raised:
                _fill_email(page, "user@example.com")

        self.assertEqual(raised.exception.code, "browser_unexpected_identity_provider")

    def test_passwordless_probe_rejects_truthy_non_boolean_adapter_result(self):
        page = MagicMock()
        page.evaluate.return_value = MagicMock()

        self.assertFalse(_click_passwordless_otp(page))

    def test_passwordless_probe_preserves_identity_provider_state(self):
        page = MagicMock()
        page.url = "https://auth.openai.com/log-in/password"
        with patch("sms_tool.registration_drivers.playwright._quick_auth_state", return_value="password"), patch(
            "sms_tool.registration_drivers.playwright._click_passwordless_otp", return_value=True
        ), patch(
            "sms_tool.registration_drivers.playwright._wait_for_registration_state", return_value="identity_provider"
        ):
            with self.assertRaises(BrowserRegistrationError) as raised:
                _fill_password_if_present(page, "Password!1")

        self.assertEqual(raised.exception.code, "browser_unexpected_identity_provider")

    @patch("sms_tool.registration_drivers.playwright._click_continue")
    def test_profile_requires_birthdate_and_supports_age_widget(self, click_continue):
        page = MagicMock()
        page.evaluate.return_value = {"name": True, "birth": True}

        _complete_profile(page, "Test User", "1990-01-02")

        self.assertIn("ageField", page.evaluate.call_args.args[0])
        self.assertIn("role=spinbutton", page.evaluate.call_args.args[0])
        click_continue.assert_called_once_with(page)

    def test_profile_does_not_submit_when_birthdate_is_missing(self):
        page = MagicMock()
        page.evaluate.return_value = {"name": True, "birth": False}
        with self.assertRaises(BrowserRegistrationError) as raised:
            _complete_profile(page, "Test User", "1990-01-02")
        self.assertEqual(raised.exception.code, "browser_profile_birthdate_missing")

    def test_heartbeat_otp_poll_heartbeats_between_short_polls(self):
        mailbox_service = MagicMock()
        mailbox_service.poll_otp.side_effect = [None, "123456"]
        browser = MagicMock()
        page = MagicMock()
        browser.select_live_page.return_value = page

        otp = _poll_browser_otp(
            mailbox_service,
            SimpleNamespace(email="user@example.com"),
            browser=browser,
            page=page,
            driver_name="camoufox",
            subject_keyword="verification code",
            timeout=40,
            issued_after_unix=1,
            proxy=None,
            excluded_otps=set(),
        )

        self.assertEqual(otp, "123456")
        self.assertEqual(mailbox_service.poll_otp.call_count, 2)
        self.assertGreaterEqual(browser.select_live_page.call_count, 1)
        self.assertGreaterEqual(page.evaluate.call_count, 1)

    @patch("sms_tool.registration_drivers.playwright._browser_heartbeat")
    def test_heartbeat_otp_poll_fails_immediately_when_browser_context_closes(self, heartbeat):
        heartbeat.side_effect = BrowserRegistrationError("browser_session_context_closed")
        mailbox_service = MagicMock()

        with self.assertRaises(BrowserRegistrationError) as raised:
            _poll_browser_otp(
                mailbox_service,
                SimpleNamespace(email="user@example.com"),
                browser=MagicMock(),
                page=MagicMock(),
                driver_name="camoufox",
                subject_keyword="verification code",
                timeout=300,
                issued_after_unix=1,
                proxy=None,
                excluded_otps=set(),
            )

        self.assertEqual(raised.exception.code, "browser_session_context_closed")
        mailbox_service.poll_otp.assert_not_called()

    def test_heartbeat_otp_poll_retries_transient_mailbox_error(self):
        mailbox_service = MagicMock()
        mailbox_service.poll_otp.side_effect = [RuntimeError("temporary mailbox failure"), "123456"]
        browser = MagicMock()
        page = MagicMock()
        browser.select_live_page.return_value = page

        otp = _poll_browser_otp(
            mailbox_service,
            SimpleNamespace(email="user@example.com"),
            browser=browser,
            page=page,
            driver_name="camoufox",
            subject_keyword="verification code",
            timeout=40,
            issued_after_unix=1,
            proxy=None,
            excluded_otps=set(),
        )

        self.assertEqual(otp, "123456")
        self.assertEqual(mailbox_service.poll_otp.call_count, 2)
        self.assertGreaterEqual(browser.select_live_page.call_count, 2)

    @patch("sms_tool.registration_drivers.playwright._wait_for_registration_state", return_value="otp")
    @patch("sms_tool.registration_drivers.playwright._fill_email")
    @patch("sms_tool.registration_drivers.playwright._manual_challenge", return_value=False)
    @patch("sms_tool.registration_drivers.playwright._maybe_accept_cookies")
    def test_cloud_otp_restart_reopens_and_resubmits_email(self, accept_cookies, _challenge, fill_email, _state):
        page = MagicMock()
        browser = MagicMock()
        browser.select_live_page.return_value = page
        events = []
        accept_cookies.side_effect = lambda *_args: events.append("cookies")
        fill_email.side_effect = lambda *_args, **_kwargs: events.append("email")

        restarted, state = _restart_email_otp_flow(
            browser,
            page,
            start_url="https://chatgpt.com/auth/login",
            email="user@example.com",
            password="Password!1",
            timeout_seconds=20,
        )

        self.assertIs(restarted, page)
        self.assertEqual(state, "otp")
        page.goto.assert_called_once()
        fill_email.assert_called_once_with(page, "user@example.com", config=None)
        self.assertEqual(events, ["cookies", "email"])

    def test_session_fetch_prefers_context_request_token(self):
        session = PlaywrightBrowserSession()
        page = _Page("https://auth.openai.com/about-you")
        request = MagicMock()
        request.get.return_value = _RequestResponse({
            "accessToken": "at",
            "user": {"email": "user@example.com"},
        })
        session.context = SimpleNamespace(pages=[page], request=request)
        session.page = page

        payload = session.fetch_json("https://chatgpt.com/api/auth/session")

        self.assertEqual(payload["body"]["accessToken"], "at")
        self.assertEqual(page.evaluate_calls, [])

    @patch("sms_tool.registration_drivers.playwright._click_continue")
    def test_fill_email_retries_after_hydration_reload(self, click_continue):
        initial = MagicMock()
        reloaded = MagicMock()
        page = MagicMock()
        page.url = "https://chatgpt.com/auth/login"
        page.locator.side_effect = [
            MagicMock(first=initial), MagicMock(count=MagicMock(return_value=1)),
            MagicMock(first=reloaded), MagicMock(count=MagicMock(return_value=1)),
        ]

        _fill_email(page, "user@example.com")

        initial.wait_for.assert_called_once_with(state="visible", timeout=30_000)
        initial.fill.assert_called_once_with("user@example.com")
        reloaded.wait_for.assert_called_once_with(state="visible", timeout=30_000)
        reloaded.fill.assert_called_once_with("user@example.com")
        self.assertEqual(click_continue.call_count, 2)

    @patch("sms_tool.registration_drivers.playwright._browser_mailbox_snapshot", return_value={})
    @patch("sms_tool.registration_drivers.playwright._registration_outcome", return_value=(True, "", ""))
    @patch("sms_tool.registration_outcome._probe_registration_access_token", return_value={"ok": True, "status_code": 200})
    @patch("sms_tool.registration_drivers.playwright._session_payload", return_value={
        "body": {"user": {"email": "user@example.com"}}, "access_token": "at", "id_token": "id"
    })
    @patch("sms_tool.registration_drivers.playwright._complete_profile")
    @patch("sms_tool.registration_drivers.playwright._wait_after_otp_submit", side_effect=("invalid", "accepted"))
    @patch("sms_tool.registration_drivers.playwright._click_resend", return_value=True)
    @patch("sms_tool.registration_drivers.playwright._fill_otp")
    @patch("sms_tool.registration_drivers.playwright._wait_for_registration_state", return_value="otp")
    @patch("sms_tool.registration_drivers.playwright._fill_email")
    @patch("sms_tool.registration_drivers.playwright._manual_challenge", return_value=False)
    @patch("sms_tool.mailbox._snapshot_mailbox_message")
    @patch("sms_tool.storage.get_device_context", return_value={})
    @patch("sms_tool.registration_drivers.playwright._ensure_mailbox_account")
    @patch("sms_tool.registration_drivers.playwright.MailboxService.create")
    def test_otp_rejection_resends_and_uses_a_fresh_code(
        self, mailbox_create, ensure_mailbox, _device, _snapshot, _challenge, _fill_email,
        _wait_state, fill_otp, resend, _wait_submit, _profile, _session, _probe, _outcome, _mailbox_snapshot,
    ):
        mailbox = SimpleNamespace(email="user@example.com")
        ensure_mailbox.return_value = mailbox
        mailbox_service = MagicMock()
        excluded_history = []
        otp_codes = iter(("111111", "222222"))
        def poll_otp(*_args, **kwargs):
            excluded_history.append(set(kwargs["excluded_otps"]))
            return next(otp_codes)
        mailbox_service.poll_otp.side_effect = poll_otp
        mailbox_create.return_value = mailbox_service
        flow_session = _FlowSession()
        session_factory = MagicMock(return_value=flow_session)
        attempts = {"count": 0}
        fill_otp.side_effect = lambda *_args: attempts.__setitem__("count", attempts["count"] + 1)

        with patch("sms_tool.registration_drivers.playwright._otp_fields", side_effect=lambda _page: object() if attempts["count"] < 2 else None):
            result = run_browser_registration(
                driver_name="playwright",
                proxy=None,
                password="Password!1",
                mailbox=mailbox,
                config={
                    "chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"},
                    "registration": {"browser_timeout_seconds": 5},
                    "email_registration": {"otp_timeout": 5},
                },
                session_factory=session_factory,
            )

        self.assertTrue(result["success"])
        self.assertEqual(mailbox_service.poll_otp.call_count, 2)
        self.assertEqual(excluded_history, [set(), {"111111"}])
        resend.assert_called_once()
        self.assertEqual(fill_otp.call_count, 2)
        history = [item["state"] for item in result["registration_machine"]["history"]]
        self.assertEqual(history.count("email_otp_validate"), 2)
        self.assertEqual(history.count("email_otp_wait"), 1)

    @patch("sms_tool.registration_drivers.playwright._browser_mailbox_snapshot", return_value={})
    @patch("sms_tool.registration_drivers.playwright._registration_outcome", return_value=(True, "", ""))
    @patch("sms_tool.registration_outcome._probe_registration_access_token", return_value={"ok": True, "status_code": 200})
    @patch("sms_tool.registration_drivers.playwright._session_payload", return_value={
        "body": {"user": {"email": "user@example.com"}}, "access_token": "at", "id_token": "id"
    })
    @patch("sms_tool.registration_drivers.playwright._complete_profile")
    @patch("sms_tool.registration_drivers.playwright._wait_after_otp_submit", return_value="accepted")
    @patch("sms_tool.registration_drivers.playwright._fill_otp")
    @patch("sms_tool.registration_drivers.playwright._wait_for_registration_state", return_value="otp")
    @patch("sms_tool.registration_drivers.playwright._fill_email")
    @patch("sms_tool.registration_drivers.playwright._manual_challenge", return_value=False)
    @patch("sms_tool.mailbox._snapshot_mailbox_message")
    @patch("sms_tool.storage.get_device_context", return_value={})
    @patch("sms_tool.registration_drivers.playwright._ensure_mailbox_account")
    @patch("sms_tool.registration_drivers.playwright.MailboxService.create")
    def test_accepted_otp_does_not_resend_for_stale_mounted_dom(
        self, mailbox_create, ensure_mailbox, _device, _snapshot, _challenge, _fill_email,
        _wait_state, fill_otp, _wait_submit, _profile, _session, _probe, _outcome, _mailbox_snapshot,
    ):
        mailbox = SimpleNamespace(email="user@example.com")
        ensure_mailbox.return_value = mailbox
        mailbox_service = MagicMock()
        mailbox_service.poll_otp.return_value = "111111"
        mailbox_create.return_value = mailbox_service

        with patch("sms_tool.registration_drivers.playwright._otp_fields", return_value=object()), patch(
            "sms_tool.registration_drivers.playwright._click_resend"
        ) as resend:
            result = run_browser_registration(
                driver_name="playwright",
                proxy=None,
                password="Password!1",
                mailbox=mailbox,
                config={
                    "chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"},
                    "registration": {"browser_timeout_seconds": 5},
                    "email_registration": {"otp_timeout": 5},
                },
                session_factory=MagicMock(return_value=_FlowSession()),
            )

        self.assertTrue(result["success"])
        fill_otp.assert_called_once()
        mailbox_service.poll_otp.assert_called_once()
        resend.assert_not_called()

    @patch("sms_tool.browser_fingerprint_pool.detect_proxy_exit_geo",
          return_value={"country": "JP", "timezone": "Asia/Tokyo", "ip": "1.2.3.4"})
    @patch("sms_tool.registration_drivers.playwright._browser_mailbox_snapshot", return_value={})
    @patch("sms_tool.registration_drivers.playwright._registration_outcome", return_value=(True, "", ""))
    @patch("sms_tool.registration_outcome._probe_registration_access_token", return_value={"ok": True, "status_code": 200})
    @patch("sms_tool.registration_drivers.playwright._session_payload", return_value={
        "body": {"user": {"email": "user@example.com"}}, "access_token": "at", "id_token": "id"
    })
    @patch("sms_tool.registration_drivers.playwright._complete_profile")
    @patch("sms_tool.registration_drivers.playwright._wait_after_otp_submit", return_value="accepted")
    @patch("sms_tool.registration_drivers.playwright._fill_otp")
    @patch("sms_tool.registration_drivers.playwright._wait_for_registration_state", return_value="otp")
    @patch("sms_tool.registration_drivers.playwright._fill_email")
    @patch("sms_tool.registration_drivers.playwright._manual_challenge", return_value=False)
    @patch("sms_tool.mailbox._snapshot_mailbox_message")
    @patch("sms_tool.storage.get_device_context", return_value={})
    @patch("sms_tool.registration_drivers.playwright._ensure_mailbox_account")
    @patch("sms_tool.registration_drivers.playwright.MailboxService.create")
    def test_browser_registration_records_geo_aligned_fingerprint(
        self, mailbox_create, ensure_mailbox, _device, _snapshot, _challenge, _fill_email,
        _wait_state, fill_otp, _wait_submit, _profile, _session, _probe, _outcome, _mailbox_snapshot, _geo,
    ):
        mailbox = SimpleNamespace(email="user@example.com")
        ensure_mailbox.return_value = mailbox
        mailbox_service = MagicMock()
        mailbox_service.poll_otp.return_value = "111111"
        mailbox_create.return_value = mailbox_service

        result = run_browser_registration(
            driver_name="playwright",
            proxy="http://user:pass@proxy.example:8080",
            password="Password!1",
            mailbox=mailbox,
            config={
                "chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"},
                "registration": {"browser_timeout_seconds": 5, "browser_geo_alignment": True},
                "email_registration": {"otp_timeout": 5},
            },
            session_factory=MagicMock(return_value=_FlowSession()),
        )

        self.assertTrue(result["success"])
        identity = result["identity_context"]
        # ① Browser profile pool: a profile label was drawn and recorded.
        self.assertTrue(identity.get("browser_fingerprint_profile"))
        # ② Exit-geo alignment: JP egress -> recorded geo country + aligned
        #    proxy_affinity.country so fingerprint geo matches the browser locale.
        self.assertEqual(identity.get("geo_country"), "JP")
        self.assertEqual(identity.get("proxy_affinity", {}).get("country"), "JP")
        # Top-level auth_fingerprint_profile now reflects the browser label.
        self.assertEqual(result["auth_fingerprint_profile"], identity["browser_fingerprint_profile"])

    def test_registration_dismisses_overlays_before_email_and_session_read(self):
        mailbox = SimpleNamespace(email="user@example.com")
        events = []
        session_result = {
            "body": {"user": {"email": mailbox.email}},
            "access_token": "at",
            "id_token": "id",
        }

        with patch("sms_tool.registration_drivers.playwright._ensure_mailbox_account", return_value=mailbox), patch(
            "sms_tool.registration_drivers.playwright.MailboxService.create", return_value=MagicMock()
        ), patch("sms_tool.storage.get_device_context", return_value={}), patch(
            "sms_tool.mailbox._snapshot_mailbox_message"
        ), patch("sms_tool.registration_drivers.playwright._manual_challenge", return_value=False), patch(
            "sms_tool.registration_drivers.playwright._maybe_accept_cookies",
            side_effect=lambda *_args: events.append("cookies"),
        ), patch(
            "sms_tool.registration_drivers.playwright._fill_email",
            side_effect=lambda *_args, **_kwargs: events.append("email"),
        ), patch(
            "sms_tool.registration_drivers.playwright._wait_for_registration_state", return_value="authenticated"
        ), patch("sms_tool.registration_drivers.playwright._otp_fields", return_value=None), patch(
            "sms_tool.registration_drivers.playwright._complete_profile"
        ), patch(
            "sms_tool.registration_drivers.playwright._wait_for_profile_completion", return_value=True
        ), patch(
            "sms_tool.registration_drivers.playwright._maybe_dismiss_chatgpt_onboarding",
            side_effect=lambda *_args, **_kwargs: events.append("onboarding"),
        ), patch(
            "sms_tool.registration_drivers.playwright._session_payload",
            side_effect=lambda *_args, **_kwargs: (events.append("session") or session_result),
        ), patch(
            "sms_tool.registration_outcome._probe_registration_access_token", return_value={"ok": True, "status_code": 200}
        ), patch(
            "sms_tool.registration_drivers.playwright._registration_outcome", return_value=(True, "", "")
        ):
            result = run_browser_registration(
                driver_name="playwright",
                proxy=None,
                password="Password!1",
                mailbox=mailbox,
                config={
                    "chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"},
                    "registration": {"browser_timeout_seconds": 5},
                },
                session_factory=MagicMock(return_value=_FlowSession()),
            )

        self.assertTrue(result["success"])
        self.assertLess(events.index("cookies"), events.index("email"))
        self.assertLess(events.index("onboarding"), events.index("session"))

    def test_authenticated_flow_skips_profile_submission(self):
        mailbox = SimpleNamespace(
            email="user@example.com",
            token="mailbox-token",
            refresh_token="mailbox-refresh-token",
        )
        proxy = "http://user-region-US-sid-BROWSER5678-t-5:proxy-secret@proxy.example:443"
        session_result = {
            "body": {"user": {"email": mailbox.email}},
            "access_token": "at",
            "id_token": "id",
        }

        session_factory = MagicMock(return_value=_FlowSession())
        with patch("sms_tool.registration_drivers.playwright._ensure_mailbox_account", return_value=mailbox), patch(
            "sms_tool.registration_drivers.playwright.MailboxService.create", return_value=MagicMock()
        ), patch("sms_tool.storage.get_device_context", return_value={"device_id": "device-browser"}), patch(
            "sms_tool.mailbox._snapshot_mailbox_message"
        ), patch("sms_tool.registration_drivers.playwright._manual_challenge", return_value=False), patch(
            "sms_tool.registration_drivers.playwright._maybe_accept_cookies"
        ), patch("sms_tool.registration_drivers.playwright._fill_email"), patch(
            "sms_tool.registration_drivers.playwright._wait_for_registration_state", return_value="authenticated"
        ), patch("sms_tool.registration_drivers.playwright._otp_fields", return_value=None), patch(
            "sms_tool.registration_drivers.playwright._quick_auth_state", return_value="authenticated"
        ), patch("sms_tool.registration_drivers.playwright._complete_profile") as complete_profile, patch(
            "sms_tool.registration_drivers.playwright._wait_for_profile_completion"
        ) as wait_profile, patch(
            "sms_tool.registration_drivers.playwright._session_payload", return_value=session_result
        ), patch(
            "sms_tool.registration_outcome._probe_registration_access_token", return_value={"ok": True, "status_code": 200}
        ), patch(
            "sms_tool.registration_drivers.playwright._registration_outcome", return_value=(True, "", "")
        ):
            result = run_browser_registration(
                driver_name="playwright",
                proxy=proxy,
                password="Password!1",
                mailbox=mailbox,
                config={
                    "chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"},
                    "registration": {"browser_timeout_seconds": 5},
                },
                session_factory=session_factory,
                proxy_metadata={"pool_index": 2, "expected_country": "US"},
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["mailbox"]["token"], "mailbox-token")
        self.assertEqual(result["mailbox"]["refresh_token"], "mailbox-refresh-token")
        self.assertEqual(result["identity_context"]["device_id"], "device-browser")
        self.assertEqual(result["identity_context"]["proxy_affinity"]["pool_index"], 2)
        self.assertEqual(result["identity_context"]["proxy_affinity"]["session_id"], "BROWSER5678")
        browser_identity = result["identity_context"]["browser_identity"]
        self.assertEqual(browser_identity["driver"], "playwright")
        self.assertEqual(browser_identity["profile_id"], result["identity_context"]["account_key"])
        session_factory.assert_called_once()
        self.assertEqual(session_factory.call_args.kwargs["browser_identity"], browser_identity)
        self.assertNotIn("proxy-secret", str(result["identity_context"]))
        complete_profile.assert_not_called()
        wait_profile.assert_not_called()

    def test_browser_failure_result_omits_mailbox_credentials(self):
        mailbox = SimpleNamespace(
            email="user@example.com",
            provider="graph",
            source="pool",
            password="mail-password",
            login_password="mail-login-password",
            access_token="mail-access-token",
            refresh_token="mail-refresh-token",
            token="mail-provider-token",
            client_secret="mail-client-secret",
        )

        with patch("sms_tool.registration_drivers.playwright._ensure_mailbox_account", return_value=mailbox), patch(
            "sms_tool.registration_drivers.playwright.MailboxService.create", return_value=MagicMock()
        ), patch("sms_tool.storage.get_device_context", return_value={}):
            result = run_browser_registration(
                driver_name="playwright",
                proxy=None,
                password="Password!1",
                mailbox=mailbox,
                config={
                    "chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"},
                    "registration": {"browser_timeout_seconds": 5},
                },
                session_factory=MagicMock(side_effect=BrowserRegistrationError("browser_launch_failed")),
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["mailbox"], {
            "email": "user@example.com",
            "source": "pool",
            "provider": "graph",
        })
        rendered = json.dumps(result, ensure_ascii=False)
        for secret in (
            "mail-password", "mail-login-password", "mail-access-token",
            "mail-refresh-token", "mail-provider-token", "mail-client-secret",
        ):
            self.assertNotIn(secret, rendered)

    def test_mailbox_setup_failure_is_returned_as_sanitized_browser_result(self):
        mailbox = SimpleNamespace(email="user@example.com", token="provider-secret")
        with patch(
            "sms_tool.registration_drivers.playwright._ensure_mailbox_account",
            return_value=mailbox,
        ), patch(
            "sms_tool.registration_drivers.playwright.MailboxService.create",
            side_effect=RuntimeError("mail provider token=provider-secret"),
        ):
            result = run_browser_registration(
                driver_name="playwright",
                proxy="http://user:secret@proxy.example:8080",
                password="Password!1",
                mailbox=mailbox,
                config={"registration": {"browser_timeout_seconds": 5}},
                session_factory=MagicMock(),
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["failure_class"], "network")
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("provider-secret", rendered)
        self.assertNotIn("user:secret@proxy.example", rendered)


if __name__ == "__main__":
    unittest.main()
