"""P1-4: offsets must be computed from the IANA zone, never written down.

The bug this pins: ``BROWSER_LOCALE_PROFILES`` hardcoded DST offsets (``us`` =
``-7 * 60`` "Pacific Daylight Time").  Those are right for part of the year and
an hour wrong for the rest, and nothing recomputed them.  Worse, on Windows
``zoneinfo`` has no database at all unless ``tzdata`` is installed, so even the
code that *tried* to recompute silently degraded to the hardcoded value.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from sms_tool import geo
from sms_tool.geo.clock import (
    formatted_offset,
    now_in_timezone,
    offset_minutes_for_timezone,
    timezone_abbreviation,
    tzdata_available,
)

# Skip the DST-boundary assertions on a host without the tz database -- they
# assert real IANA behaviour, not our logic.
_REAL_TZDB = tzdata_available()


class OffsetTests(unittest.TestCase):
    def test_blank_name_returns_the_default(self):
        self.assertEqual(offset_minutes_for_timezone("", 42), 42)
        self.assertEqual(offset_minutes_for_timezone(None, -300), -300)

    @unittest.skipUnless(_REAL_TZDB, "requires the IANA tz database (tzdata)")
    def test_new_york_moves_with_dst(self):
        # The whole point: January and July must not give the same answer.
        january = datetime(2026, 1, 15, 12, 0)
        july = datetime(2026, 7, 15, 12, 0)
        self.assertEqual(offset_minutes_for_timezone("America/New_York", at=january), -300)
        self.assertEqual(offset_minutes_for_timezone("America/New_York", at=july), -240)

    @unittest.skipUnless(_REAL_TZDB, "requires the IANA tz database (tzdata)")
    def test_london_moves_with_dst(self):
        self.assertEqual(
            offset_minutes_for_timezone("Europe/London", at=datetime(2026, 1, 15)), 0
        )
        self.assertEqual(
            offset_minutes_for_timezone("Europe/London", at=datetime(2026, 7, 15)), 60
        )

    @unittest.skipUnless(_REAL_TZDB, "requires the IANA tz database (tzdata)")
    def test_zones_without_dst_are_stable(self):
        for moment in (datetime(2026, 1, 15), datetime(2026, 7, 15)):
            self.assertEqual(
                offset_minutes_for_timezone("Asia/Tokyo", at=moment), 540
            )

    @unittest.skipUnless(_REAL_TZDB, "requires the IANA tz database (tzdata)")
    def test_unknown_zone_falls_back_to_the_default(self):
        self.assertEqual(offset_minutes_for_timezone("Mars/Olympus", 99), 99)

    def test_missing_tzdata_falls_back_without_raising(self):
        with patch.object(geo.clock, "_zoneinfo", lambda: None):
            self.assertEqual(offset_minutes_for_timezone("Asia/Tokyo", 33), 33)

    def test_raising_zoneinfo_falls_back_without_raising(self):
        class _Boom:
            def __call__(self, key):
                raise RuntimeError("no tz database")

        with patch.object(geo.clock, "_zoneinfo", lambda: _Boom()):
            self.assertEqual(offset_minutes_for_timezone("Asia/Tokyo", 33), 33)


class FormattedOffsetTests(unittest.TestCase):
    def test_negative_offsets(self):
        self.assertEqual(formatted_offset(-240), "-0400")
        self.assertEqual(formatted_offset(-300), "-0500")

    def test_positive_offsets(self):
        self.assertEqual(formatted_offset(540), "+0900")
        self.assertEqual(formatted_offset(330), "+0530")

    def test_zero_and_junk(self):
        self.assertEqual(formatted_offset(0), "+0000")
        self.assertEqual(formatted_offset(None), "+0000")


class TimezoneNameTests(unittest.TestCase):
    @unittest.skipUnless(_REAL_TZDB, "requires the IANA tz database (tzdata)")
    def test_abbreviation_matches_the_current_season(self):
        # EDT in summer, EST in winter -- a stale abbreviation is the visible
        # half of the same bug as a stale offset.
        expected = "EDT" if offset_minutes_for_timezone("America/New_York") == -240 else "EST"
        self.assertEqual(timezone_abbreviation("America/New_York"), expected)

    def test_abbreviation_is_blank_when_unknown(self):
        self.assertEqual(timezone_abbreviation(""), "")
        with patch.object(geo.clock, "_zoneinfo", lambda: None):
            self.assertEqual(timezone_abbreviation("Asia/Tokyo"), "")

    @unittest.skipUnless(_REAL_TZDB, "requires the IANA tz database (tzdata)")
    def test_now_in_timezone_agrees_with_the_offset(self):
        # The requirements token and the runner config must describe the same
        # instant; a mismatch there is a detectable contradiction.
        tz = "America/New_York"
        now = now_in_timezone(tz)
        offset = offset_minutes_for_timezone(tz)
        self.assertEqual(now.utcoffset().total_seconds() // 60, offset)


class BrowserEnvironmentOffsetTests(unittest.TestCase):
    """``build_browser_environment`` must never emit the table's stale value."""

    def test_offset_is_recomputed_even_without_geo(self):
        from sms_tool.browser_fingerprint_pool import (
            BROWSER_LOCALE_PROFILES,
            build_browser_environment,
        )

        # Force a zone whose table value is deliberately wrong, then prove the
        # output comes from the zone, not the table.
        table_value = BROWSER_LOCALE_PROFILES["us"]["timezone_offset_minutes"]
        bogus = -1234
        with patch.dict(
            BROWSER_LOCALE_PROFILES["us"], {"timezone_offset_minutes": bogus}
        ):
            env = build_browser_environment(None)
        self.assertNotEqual(env["timezone_offset_minutes"], bogus)
        self.assertEqual(
            env["timezone_offset_minutes"],
            offset_minutes_for_timezone(env["timezone_iana"]),
        )
        # Restored by patch.dict, so the real table value is untouched.
        self.assertEqual(BROWSER_LOCALE_PROFILES["us"]["timezone_offset_minutes"], table_value)

    @unittest.skipUnless(_REAL_TZDB, "requires the IANA tz database (tzdata)")
    def test_geo_timezone_drives_the_offset(self):
        from sms_tool.browser_fingerprint_pool import build_browser_environment

        env = build_browser_environment({"country": "JP", "timezone": "Asia/Tokyo"})
        self.assertEqual(env["timezone_iana"], "Asia/Tokyo")
        self.assertEqual(env["timezone_offset_minutes"], 540)


class SentinelProfileOffsetTests(unittest.TestCase):
    """The protocol path used to send ``timezoneOffsetMinutes: 0`` always."""

    def test_profile_carries_an_offset(self):
        from sms_tool.auth_headers import sentinel_fingerprint

        profile = sentinel_fingerprint()
        self.assertIn("timezone_offset_minutes", profile)
        self.assertEqual(
            profile["timezone_offset_minutes"],
            offset_minutes_for_timezone(str(profile.get("timezone") or "")),
        )

    @unittest.skipUnless(_REAL_TZDB, "requires the IANA tz database (tzdata)")
    def test_offset_is_not_zero_for_a_real_zone(self):
        from sms_tool import auth_headers

        auth_headers.set_fingerprint_geo("JP")
        try:
            profile = auth_headers.sentinel_fingerprint()
            self.assertEqual(str(profile["timezone"]), "Asia/Tokyo")
            self.assertEqual(profile["timezone_offset_minutes"], 540)
        finally:
            auth_headers.set_fingerprint_geo("")


if __name__ == "__main__":
    unittest.main()
