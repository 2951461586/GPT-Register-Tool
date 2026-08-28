"""Unit tests for the browser fingerprint pool (headless-registration path).

Mirrors turb-gpt-free-register's BROWSER_PROFILE_POOL + _detect_exit_geo:
profile rotation, exit-geo locale/timezone alignment, and graceful fallback.
"""

import unittest
from unittest.mock import patch

from sms_tool.browser_fingerprint_pool import (
    BROWSER_PROFILE_POOL,
    detect_proxy_exit_geo,
    locale_profile_key_from_geo,
    select_browser_profile,
    shared_browser_profile_pool,
)
from sms_tool.registration_drivers.browser_session import PlaywrightBrowserSession
from sms_tool.registration_drivers.external_sessions import create_browser_session


class BrowserFingerprintPoolTests(unittest.TestCase):
    def test_select_browser_profile_deterministic_by_seed(self):
        a = select_browser_profile(None, seed="device-abc")
        b = select_browser_profile(None, seed="device-abc")
        self.assertEqual(a["browser_profile_index"], b["browser_profile_index"])
        self.assertEqual(a["browser_fingerprint_profile"], b["browser_fingerprint_profile"])
        # Different seeds should usually diverge (not guaranteed, but stable).
        self.assertEqual(
            select_browser_profile(None, seed="device-abc")["browser_profile_index"],
            a["browser_profile_index"],
        )

    def test_browser_profile_pool_round_robin_covers_all(self):
        pool = shared_browser_profile_pool()
        seen = {pool.next()["browser_profile_index"] for _ in range(pool.size())}
        self.assertEqual(len(seen), len(BROWSER_PROFILE_POOL))
        self.assertEqual(seen, set(range(len(BROWSER_PROFILE_POOL))))

    def test_locale_profile_key_from_geo_map_and_fallback(self):
        self.assertEqual(locale_profile_key_from_geo({"country": "JP"}), "jp")
        self.assertEqual(locale_profile_key_from_geo({"country": "DE"}), "de")
        self.assertEqual(locale_profile_key_from_geo({"country": "HK"}), "hk")
        # Unknown / missing country falls back to the default (us) profile.
        self.assertEqual(locale_profile_key_from_geo({"country": "ZZ"}), "us")
        self.assertEqual(locale_profile_key_from_geo(None), "us")

    def test_build_browser_environment_aligns_tz_to_geo(self):
        env = select_browser_profile(
            {"country": "JP", "timezone": "Asia/Tokyo", "ip": "1.2.3.4"},
            seed="device-jp",
        )
        self.assertEqual(env["navigator_language"], "ja-JP")
        self.assertEqual(env["timezone_iana"], "Asia/Tokyo")
        self.assertEqual(env["geo"].get("country"), "JP")
        self.assertTrue(env["browser_fingerprint_profile"])

    def test_detect_proxy_exit_geo_direct_or_disabled_returns_empty(self):
        # No proxy -> no probe, returns empty so caller keeps configured locale.
        self.assertEqual(detect_proxy_exit_geo(None), {})
        self.assertEqual(detect_proxy_exit_geo("", enabled=False), {})

    def test_detect_proxy_exit_geo_graceful_on_failure(self):
        # Any network failure must degrade to {} without raising.
        with patch(
            "sms_tool.browser_fingerprint_pool._query_geo_endpoints",
            side_effect=RuntimeError("network down"),
        ):
            self.assertEqual(detect_proxy_exit_geo("http://proxy.example:8080"), {})

    def test_playwright_session_applies_viewport_from_pool(self):
        # Default viewport unchanged when no pool drawn.
        default = PlaywrightBrowserSession()
        self.assertEqual(default.viewport, {"width": 1440, "height": 900})
        # Rotated screen profile is applied (no browser launch in __init__).
        rotated = PlaywrightBrowserSession(viewport=(1512, 982))
        self.assertEqual(rotated.viewport, {"width": 1512, "height": 982})

    def test_create_browser_session_forwards_viewport_to_playwright(self):
        session = create_browser_session(
            "playwright",
            config={"registration": {}},
            proxy=None,
            headless=True,
            timeout_ms=10_000,
            locale="en-US",
            timezone_id="America/New_York",
            viewport=(1728, 1117),
        )
        self.assertIsInstance(session, PlaywrightBrowserSession)
        self.assertEqual(session.viewport, {"width": 1728, "height": 1117})

    def test_create_browser_session_playwright_ignores_viewport_for_roxy(self):
        # Roxy path must accept the kwarg without applying a playwright viewport.
        session = create_browser_session(
            "roxy",
            config={"registration": {"drivers": {"roxy": {}}}},
            proxy=None,
            headless=True,
            timeout_ms=10_000,
            locale="en-US",
            timezone_id="America/New_York",
            viewport=(1728, 1117),
        )
        self.assertIsNotNone(session)


if __name__ == "__main__":
    unittest.main()
