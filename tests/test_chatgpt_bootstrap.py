"""Unit tests for the ChatGPT first-screen warm-up (bootstrap).

Mirrors turb-gpt-free-register's ``core/chatgpt_bootstrap.py`` contract: the
warm-up is decoration, never a gate — every failure is counted, never raised.
The config-gated entry points must additionally be strict no-ops while the
feature is disabled (the shipping default).
"""

import unittest

from sms_tool.chatgpt_bootstrap import (
    ANON_BASE,
    API_BASE,
    anonymous_bootstrap,
    authenticated_bootstrap,
    bootstrap_config,
    run_anonymous_bootstrap,
    run_authenticated_bootstrap,
)


class _FakePage:
    """Page double recording every ``evaluate`` call; can be told to fail."""

    def __init__(self, *, status: int = 200, tz: int = 480, raise_on_evaluate: bool = False):
        self.status = status
        self.tz = tz
        self.raise_on_evaluate = raise_on_evaluate
        self.urls: list[str] = []
        self.headers: list[dict] = []
        self.scripts: list[str] = []

    def evaluate(self, script, arg=None):
        self.scripts.append(script)
        if self.raise_on_evaluate:
            raise RuntimeError("page crashed")
        if "getTimezoneOffset" in script:
            return self.tz
        url = ""
        headers: dict = {}
        if isinstance(arg, list) and arg:
            url = str(arg[0])
            if len(arg) > 1 and isinstance(arg[1], dict):
                headers = arg[1]
        self.urls.append(url)
        self.headers.append(headers)
        return {"status": self.status, "ok": 200 <= self.status < 400, "body": ""}


class BootstrapConfigTests(unittest.TestCase):
    def test_defaults_disabled(self):
        for cfg in (None, {}, {"registration": {}}):
            self.assertFalse(bootstrap_config(cfg)["enabled"])

    def test_reads_registration_section(self):
        cfg = bootstrap_config({"registration": {"chatgpt_bootstrap": {
            "enabled": True, "anonymous": False,
        }}})
        self.assertTrue(cfg["enabled"])
        self.assertFalse(cfg["anonymous"])
        self.assertTrue(cfg["authenticated"])  # default preserved

    def test_coerces_truthy_values(self):
        cfg = bootstrap_config({"registration": {"chatgpt_bootstrap": {"enabled": "yes"}}})
        self.assertIs(cfg["enabled"], True)


class AnonymousBootstrapTests(unittest.TestCase):
    def test_hits_anonymous_endpoints_with_timezone(self):
        page = _FakePage(tz=480)
        result = anonymous_bootstrap(page)
        self.assertEqual(result["stats"]["attempted"], 6)
        self.assertEqual(result["stats"]["ok"], 6)
        self.assertTrue(all(url.startswith(ANON_BASE) for url in page.urls))
        self.assertIn(
            f"{ANON_BASE}/accounts/check/v4-2023-04-27?timezone_offset_min=480",
            page.urls,
        )

    def test_covers_all_system_hints_modes(self):
        page = _FakePage()
        anonymous_bootstrap(page)
        joined = "\n".join(page.urls)
        for mode in ("custom_agents", "connectors", "basic"):
            self.assertIn(f"system_hints?mode={mode}", joined)

    def test_no_page_returns_skipped(self):
        result = anonymous_bootstrap(None)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "no_page")

    def test_page_crash_is_counted_not_raised(self):
        page = _FakePage(raise_on_evaluate=True)
        result = anonymous_bootstrap(page)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stats"]["failed"], 6)
        self.assertEqual(result["stats"]["ok"], 0)

    def test_http_error_is_counted_not_raised(self):
        page = _FakePage(status=500)
        result = anonymous_bootstrap(page)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stats"]["failed"], 6)

    def test_strict_mode_raises(self):
        page = _FakePage(status=403)
        with self.assertRaises(RuntimeError):
            anonymous_bootstrap(page, strict=True)


class AuthenticatedBootstrapTests(unittest.TestCase):
    def test_sends_bearer_and_device_headers(self):
        page = _FakePage()
        result = authenticated_bootstrap(page, "tok-123", device_id="dev-9")
        self.assertEqual(result["stats"]["attempted"], 7)
        self.assertTrue(all(url.startswith(API_BASE) for url in page.urls))
        for headers in page.headers:
            self.assertEqual(headers["Authorization"], "Bearer tok-123")
            self.assertEqual(headers["oai-device-id"], "dev-9")

    def test_bearer_prefix_not_doubled(self):
        page = _FakePage()
        authenticated_bootstrap(page, "Bearer tok-123")
        self.assertEqual(page.headers[0]["Authorization"], "Bearer tok-123")

    def test_omits_authorization_without_token(self):
        page = _FakePage()
        authenticated_bootstrap(page, "")
        self.assertNotIn("Authorization", page.headers[0])

    def test_no_page_returns_skipped(self):
        self.assertTrue(authenticated_bootstrap(None)["skipped"])


class ConfigGatedEntryPointTests(unittest.TestCase):
    """The wiring used by the registration flow must be a strict no-op by default."""

    def test_disabled_makes_no_page_calls(self):
        page = _FakePage()
        result = run_anonymous_bootstrap(page, {})
        self.assertTrue(result["skipped"])
        self.assertEqual(page.urls, [])
        self.assertEqual(page.scripts, [])

    def test_enabled_runs_warm_up(self):
        page = _FakePage()
        result = run_anonymous_bootstrap(
            page, {"registration": {"chatgpt_bootstrap": {"enabled": True}}}
        )
        self.assertFalse(result.get("skipped"))
        self.assertEqual(len(page.urls), 6)

    def test_authenticated_disabled_makes_no_calls(self):
        page = _FakePage()
        result = run_authenticated_bootstrap(page, "tok", config={})
        self.assertTrue(result["skipped"])
        self.assertEqual(page.urls, [])

    def test_anonymous_flag_can_disable_single_phase(self):
        page = _FakePage()
        cfg = {"registration": {"chatgpt_bootstrap": {"enabled": True, "anonymous": False}}}
        self.assertTrue(run_anonymous_bootstrap(page, cfg)["skipped"])
        self.assertEqual(page.urls, [])

    def test_crash_never_propagates(self):
        page = _FakePage(raise_on_evaluate=True)
        cfg = {"registration": {"chatgpt_bootstrap": {"enabled": True}}}
        # Must not raise even though every single request fails.
        self.assertFalse(run_anonymous_bootstrap(page, cfg)["ok"])


if __name__ == "__main__":
    unittest.main()
