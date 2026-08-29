"""Unit tests for the browser fingerprint pool (headless-registration path).

Mirrors turb-gpt-free-register's BROWSER_PROFILE_POOL + _detect_exit_geo:
profile rotation, exit-geo locale/timezone alignment, and graceful fallback.
"""

import unittest
from unittest.mock import patch

from sms_tool.browser_fingerprint_pool import (
    BROWSER_LOCALE_PROFILES,
    BROWSER_PROFILE_POOL,
    build_browser_environment,
    classify_proxy_org,
    detect_proxy_exit_geo,
    is_cloud_proxy,
    locale_profile_key_from_geo,
    select_browser_profile,
    shared_browser_profile_pool,
    validate_browser_profile,
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


class BrowserProfileValidationTests(unittest.TestCase):
    """Profile consistency self-check (mirrors turb's validate_browser_profile).

    The contract is diagnostic-only: contradictions are reported, never raised,
    so a bad profile can never take registration down.
    """

    def test_pool_and_locale_matrix_has_no_contradictions(self):
        # Regression guard: every hardware profile x every locale profile must
        # be internally consistent.  Catches anyone appending to
        # BROWSER_PROFILE_POOL or BROWSER_LOCALE_PROFILES with mismatched values.
        for base in BROWSER_PROFILE_POOL:
            for geo in [None] + [{"country": code} for code in BROWSER_LOCALE_PROFILES]:
                env = build_browser_environment(geo, base)
                self.assertEqual(
                    validate_browser_profile(env),
                    [],
                    msg=f"contradiction in profile={base} geo={geo}",
                )

    def test_validate_flags_language_missing_from_languages(self):
        issues = validate_browser_profile({
            "navigator_language": "ja-JP",
            "navigator_languages": ["en-US"],
            "accept_language": "ja-JP,ja;q=0.9",
            "timezone_iana": "Asia/Tokyo",
        })
        self.assertTrue(any("navigator_language" in item for item in issues))

    def test_validate_flags_accept_language_mismatch(self):
        issues = validate_browser_profile({
            "navigator_language": "ja-JP",
            "navigator_languages": ["ja-JP"],
            "accept_language": "en-US,en;q=0.9",
            "timezone_iana": "Asia/Tokyo",
        })
        self.assertTrue(any("accept_language" in item for item in issues))

    def test_validate_flags_blank_and_non_iana_timezone(self):
        blank = validate_browser_profile({
            "navigator_language": "en-US", "navigator_languages": ["en-US"],
            "accept_language": "en-US,en;q=0.9", "timezone_iana": "",
        })
        self.assertTrue(any("timezone_iana" in item for item in blank))
        bogus = validate_browser_profile({
            "navigator_language": "en-US", "navigator_languages": ["en-US"],
            "accept_language": "en-US,en;q=0.9", "timezone_iana": "Tokyo",
        })
        self.assertTrue(any("IANA" in item for item in bogus))

    def test_validate_flags_out_of_range_hardware(self):
        issues = validate_browser_profile({
            "navigator_language": "en-US", "navigator_languages": ["en-US"],
            "accept_language": "en-US,en;q=0.9", "timezone_iana": "America/New_York",
            "screen_width": 10, "hardware_concurrency": 9999,
        })
        self.assertTrue(any("screen_width" in item for item in issues))
        self.assertTrue(any("hardware_concurrency" in item for item in issues))

    def test_validate_ignores_absent_provider_owned_fields(self):
        # Roxy/Cloak/Camoufox own the screen size; a profile without it is
        # normal here, not a contradiction.
        self.assertEqual(validate_browser_profile({
            "navigator_language": "en-US", "navigator_languages": ["en-US"],
            "accept_language": "en-US,en;q=0.9", "timezone_iana": "America/New_York",
        }), [])

    def test_validate_rejects_non_mapping(self):
        issues = validate_browser_profile("not-a-dict")
        self.assertEqual(len(issues), 1)
        self.assertIn("must be a mapping", issues[0])

    def test_select_browser_profile_survives_contradictions(self):
        # Validation must never raise out of the selection path.
        with patch.dict(BROWSER_LOCALE_PROFILES, {"us": {
            "navigator_language": "en-US",
            "navigator_languages": ["fr-FR"],  # deliberately contradictory
            "accept_language": "en-US,en;q=0.9",
            "timezone_iana": "America/Los_Angeles",
            "timezone_offset_minutes": -420,
            "timezone_name": "Pacific Daylight Time",
        }}):
            env = select_browser_profile(None, seed="device-x")
        self.assertEqual(env["navigator_language"], "en-US")
        self.assertTrue(validate_browser_profile(env))


class CloudProxyClassificationTests(unittest.TestCase):
    """P4: cloud/datacenter ASN detection (mirrors turb's REJECT_CLOUD_PROXY).

    Diagnostic only — even with rejection opted in, selection must still return
    a usable profile.  A user may intentionally pin a fixed cloud egress.
    """

    def test_classify_detects_cloud_orgs(self):
        for org in (
            "AS14061 DigitalOcean, LLC",
            "Amazon.com, Inc.",
            "Google Cloud",
            "Tencent Cloud Computing",
            "Hetzner Online GmbH",
        ):
            self.assertEqual(classify_proxy_org(org), "cloud", msg=org)

    def test_classify_residential_org(self):
        self.assertEqual(classify_proxy_org("Comcast Cable Communications"), "residential")

    def test_classify_unknown_on_missing_org(self):
        self.assertEqual(classify_proxy_org(""), "unknown")
        self.assertEqual(classify_proxy_org(None), "unknown")
        self.assertEqual(classify_proxy_org({}), "unknown")

    def test_classify_accepts_geo_mapping(self):
        self.assertEqual(classify_proxy_org({"org": "Amazon.com, Inc."}), "cloud")

    def test_is_cloud_proxy_disabled_by_default(self):
        # Never blocks while detection is off — the shipping default.
        self.assertFalse(is_cloud_proxy({"org": "Amazon.com, Inc."}))
        self.assertFalse(is_cloud_proxy({"org": "Amazon.com, Inc."}, enabled=False))

    def test_is_cloud_proxy_when_enabled(self):
        self.assertTrue(is_cloud_proxy({"org": "Amazon.com, Inc."}, enabled=True))
        self.assertFalse(is_cloud_proxy({"org": "Comcast Cable"}, enabled=True))
        # An unknown org must never be treated as cloud.
        self.assertFalse(is_cloud_proxy({}, enabled=True))

    def test_environment_carries_org_class(self):
        env = build_browser_environment({"country": "US", "org": "Amazon.com, Inc."})
        self.assertEqual(env["proxy_org"], "Amazon.com, Inc.")
        self.assertEqual(env["proxy_org_class"], "cloud")

    def test_select_profile_never_blocks_on_cloud_egress(self):
        cfg = {"registration": {"reject_cloud_proxy": True}}
        geo = {"country": "US", "org": "Amazon.com, Inc."}
        env = select_browser_profile(geo, seed="device-cloud", config=cfg)
        self.assertEqual(env["proxy_org_class"], "cloud")
        self.assertTrue(env["browser_fingerprint_profile"])


if __name__ == "__main__":
    unittest.main()
