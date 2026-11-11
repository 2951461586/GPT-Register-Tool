"""Cross-layer consistency of the protocol fingerprint (platform-blind device data).

Before 2026-11-11 every protocol profile — iOS Safari and macOS Safari included —
drew its screen / DPR / CPU / memory from one Windows-desktop table, so an
iPhone UA reported ``screen=2560x1080, dpr=1.25, maxTouchPoints=5``.  These
tests pin the per-platform tables and the validator that would have caught it.
"""

from __future__ import annotations

import pytest

from sms_tool.auth_headers import (
    AUTH_FINGERPRINT_PROFILES,
    _platform_class,
    _timezone_display_name,
    sentinel_fingerprint,
    set_auth_fingerprint,
    set_fingerprint_device,
    set_fingerprint_geo,
    validate_protocol_fingerprint,
)

PLATFORM_BY_CLASS = {"windows": "Win32", "macos": "MacIntel", "ios": "iPhone"}


@pytest.fixture(autouse=True)
def _reset_thread_state():
    set_auth_fingerprint("firefox144")
    set_fingerprint_geo("US")
    set_fingerprint_device("acct-reset")
    yield
    set_auth_fingerprint("firefox144")
    set_fingerprint_geo("US")
    set_fingerprint_device("acct-reset")


def _fingerprint(profile: str, *, country: str = "US", device: str = "acct-1") -> dict:
    set_auth_fingerprint(profile)
    set_fingerprint_geo(country)
    set_fingerprint_device(device)
    return sentinel_fingerprint()


class TestPlatformTables:
    def test_every_profile_is_internally_consistent(self):
        for name in AUTH_FINGERPRINT_PROFILES:
            for country in ("US", "IN", "JP"):
                fingerprint = _fingerprint(name, country=country)
                assert validate_protocol_fingerprint(fingerprint) == [], (name, country)

    def test_ios_reports_a_phone_viewport(self):
        for name in AUTH_FINGERPRINT_PROFILES:
            if _platform_class(name) != "ios":
                continue
            fingerprint = _fingerprint(name)
            width = int(str(fingerprint["screen"]).split("x")[0])
            assert fingerprint["navigator_platform"] == "iPhone"
            assert fingerprint["max_touch_points"] == 5
            assert 320 <= width <= 500, (name, fingerprint["screen"])
            assert float(fingerprint["device_pixel_ratio"]) >= 2.0

    def test_macos_reports_a_retina_desktop(self):
        for name in AUTH_FINGERPRINT_PROFILES:
            if _platform_class(name) != "macos":
                continue
            fingerprint = _fingerprint(name)
            assert fingerprint["navigator_platform"] == "MacIntel"
            assert fingerprint["max_touch_points"] == 0
            assert float(fingerprint["device_pixel_ratio"]) in {1.0, 2.0}

    def test_windows_reports_no_touch(self):
        for name in AUTH_FINGERPRINT_PROFILES:
            if _platform_class(name) != "windows":
                continue
            fingerprint = _fingerprint(name)
            assert fingerprint["navigator_platform"] == "Win32"
            assert fingerprint["max_touch_points"] == 0
            assert float(fingerprint["device_pixel_ratio"]) <= 1.5

    def test_device_profile_is_stable_per_account(self):
        first = _fingerprint("chrome146", device="acct-1")
        second = _fingerprint("chrome146", device="acct-1")
        assert first["screen"] == second["screen"]
        assert first["hardware_concurrency"] == second["hardware_concurrency"]

    def test_switching_platform_class_drops_the_cached_measurements(self):
        # The cache is keyed by (seed, platform class): reusing a Windows screen
        # for an iOS profile drawn from the same account is exactly the defect.
        set_fingerprint_geo("US")
        set_fingerprint_device("acct-same")
        set_auth_fingerprint("chrome146")
        desktop = sentinel_fingerprint()
        set_auth_fingerprint("safari18_0_ios")
        phone = sentinel_fingerprint()
        assert desktop["screen"] != phone["screen"]
        assert phone["navigator_platform"] == "iPhone"


class TestValidator:
    def test_it_rejects_a_windows_screen_on_an_ios_profile(self):
        fingerprint = _fingerprint("safari18_0_ios")
        fingerprint["screen"] = "2560x1080"
        fingerprint["device_pixel_ratio"] = 1.25
        issues = validate_protocol_fingerprint(fingerprint)
        assert any("implausible" in issue for issue in issues)
        assert any("device_pixel_ratio" in issue for issue in issues)

    def test_it_rejects_touch_points_on_a_desktop_profile(self):
        fingerprint = _fingerprint("chrome146")
        fingerprint["max_touch_points"] = 5
        assert any("max_touch_points" in issue for issue in validate_protocol_fingerprint(fingerprint))

    def test_it_rejects_a_platform_that_disagrees_with_the_ua(self):
        fingerprint = _fingerprint("safari18_0")
        fingerprint["navigator_platform"] = "Win32"
        assert any("navigator_platform" in issue for issue in validate_protocol_fingerprint(fingerprint))

    def test_it_rejects_a_language_that_does_not_lead_accept_language(self):
        fingerprint = _fingerprint("firefox144")
        fingerprint["lang"] = "ja-JP"
        assert any("does not start with lang" in issue for issue in validate_protocol_fingerprint(fingerprint))

    def test_it_rejects_a_utc_offset_on_a_non_utc_clock(self):
        fingerprint = _fingerprint("firefox144", country="JP")
        fingerprint["timezone_offset_minutes"] = 0
        assert validate_protocol_fingerprint(fingerprint) == []  # 0 is in range
        fingerprint["timezone_offset_minutes"] = -9000
        assert any("out of range" in issue for issue in validate_protocol_fingerprint(fingerprint))


class TestTimezoneName:
    def test_the_protocol_profile_carries_an_os_style_label(self):
        fingerprint = _fingerprint("chrome146", country="JP")
        assert fingerprint["timezone"] == "Asia/Tokyo"
        assert fingerprint["timezone_name"] == "Japan Standard Time"

    def test_india_is_covered_because_the_registration_pool_moved_there(self):
        fingerprint = _fingerprint("chrome146", country="IN")
        assert fingerprint["timezone"] == "Asia/Kolkata"
        assert fingerprint["timezone_name"] == "India Standard Time"

    def test_the_label_matches_the_browser_lane_table(self):
        from sms_tool.geo.profiles import WINDOWS_TIMEZONE_BY_IANA

        for zone in ("Asia/Tokyo", "America/New_York", "Europe/London", "Asia/Kolkata"):
            assert _timezone_display_name(zone) == WINDOWS_TIMEZONE_BY_IANA[zone]

    def test_unknown_zones_degrade_to_the_iana_id(self):
        assert _timezone_display_name("Antarctica/Troll") == "Antarctica/Troll"
        assert _timezone_display_name("") == ""

    def test_requirements_token_uses_the_label_not_the_abbreviation(self):
        import base64
        import json

        from sms_tool.sentinel.client import _requirements_token

        fingerprint = _fingerprint("chrome146", country="JP")
        token = _requirements_token("11111111-1111-4111-8111-111111111111", fingerprint)
        # ``p`` is ``gAAAAAC`` + base64(JSON) + ``~S``.
        assert token.startswith("gAAAAAC") and token.endswith("~S")
        payload = json.loads(base64.b64decode(token[len("gAAAAAC") : -len("~S")]))
        date_string = str(payload[1])
        assert date_string.endswith("(Japan Standard Time)"), date_string
        assert "JST" not in date_string
