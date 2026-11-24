"""Regression tests for Batch 2 upgrades: DST clock, 9-vendor proxy rotation,
health tracker key stability, Sentinel full headers, reference_lane cleanup,
and bridge IPv6/failover/HTTPS handling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest

from sms_tool.geo import clock
from sms_tool.proxy import bridge as proxy_bridge
from sms_tool.proxy import entry as proxy_entry
from sms_tool.proxy.health import ProxyHealthTracker
from sms_tool.registration import reference_lane
from sms_tool.sentinel import client as sentinel_client


def test_clock_offset_handles_aware_datetimes_across_dst_boundaries():
    # 2026-03-08 06:30 UTC is 01:30 EST (-300 min); in Shanghai it is 14:30.
    spring_utc = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)
    spring_sh = spring_utc.astimezone(ZoneInfo("Asia/Shanghai"))
    assert clock.offset_minutes_for_timezone("America/New_York", at=spring_utc) == -300
    assert clock.offset_minutes_for_timezone("America/New_York", at=spring_sh) == -300

    # 2026-11-01 05:30 UTC is 01:30 EDT (-240 min); 06:30 UTC is 01:30 EST (-300 min).
    fall_edt = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    fall_est = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
    assert clock.offset_minutes_for_timezone("America/New_York", at=fall_edt) == -240
    assert clock.offset_minutes_for_timezone("America/New_York", at=fall_est) == -300
    assert (
        clock.offset_minutes_for_timezone("America/New_York", at=fall_edt.astimezone(ZoneInfo("Asia/Shanghai"))) == -240
    )


@pytest.mark.parametrize(
    ("label", "url"),
    [
        ("iproyal", "http://user:pass_country-us_session-abc123_lifetime-10m@geo.iproyal.example:12321"),
        ("9http_dash", "http://user-region-US-sid-abc123:pass@proxy.9http.example:8000"),
        ("9http_under", "http://user_region_US_sid_abc123:pass@proxy.9http.example:8000"),
        ("kookeey_ttl", "http://user:base-US-123456-10m@gate.kookeey.example:1000"),
        ("kookeey_sid", "http://user:pass-region-US-sid-abc123@gate.kookeey.example:1000"),
        ("ipwo_sessid", "http://user_sessid_abc123:pass@proxy.ipwo.example:8000"),
        ("ipwo_session", "http://user_session_abc123:pass@proxy.ipwo.example:8000"),
        ("proxylane", "http://user-country-US-session-abc123:pass@proxy.lane.example:8000"),
        ("decodo", "http://user-session-abc123-sessionduration-10:pass@gate.decodo.example:10001"),
        ("rola_dash", "http://user-zone-custom-region-US-sessid-abc123:pass@proxy.rola.example:1000"),
        ("rola_under", "http://acct_1234-country-us:pass@proxy.rola.example:1000"),
    ],
)
def test_rotate_session_and_strip_session_id_cover_all_major_vendors(label: str, url: str):
    rotated = proxy_entry.rotate_session(url, "DE")
    assert rotated != url, f"{label} did not rotate"
    assert "abc123" not in rotated, f"{label} kept old session id: {rotated}"
    # Rotated and original must normalize to the same template key once target region matches.
    retargeted = proxy_entry.retarget_region(url, "DE")
    assert proxy_entry.strip_session_id(retargeted) == proxy_entry.strip_session_id(rotated), label


def test_health_tracker_collapses_rotated_sids_and_isolates_kookeey_passwords(tmp_path: Path):
    tracker = ProxyHealthTracker({}, path=tmp_path / "health.json")
    s1 = "http://user-region-US-sid-AAA111:pw@gw.example:8000"
    s2 = "http://user-region-US-sid-BBB222:pw@gw.example:8000"
    assert tracker.template_key(s1) == tracker.template_key(s2)
    assert tracker.key(s1) != tracker.key(s2)

    k1 = "http://shared:acctA-US-1111-10m@gate.kookeey.example:1000"
    k2 = "http://shared:acctB-US-2222-10m@gate.kookeey.example:1000"
    assert tracker.template_key(k1) != tracker.template_key(k2)

    # Live failure on rotated session s2 must affect ranking of base template s1.
    for _ in range(3):
        tracker.record(s2, ok=False, error="403")
    clean = "http://other:pw@clean.example:8000"
    assert tracker.rank([s1, clean])[0] == clean


def test_sentinel_wire_headers_include_browser_family_metadata():
    chrome_profile = {
        "impersonate": "chrome152",
        "user_agent": "Mozilla/5.0 Chrome/152.0.0.0",
        "lang_full": "de-DE,de;q=0.9,en;q=0.8",
        "sec_ch_ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    }
    headers = sentinel_client._sentinel_wire_headers(
        chrome_profile,
        referer="https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=test",
        origin="https://sentinel.openai.com",
        accept="*/*",
        content_type="text/plain;charset=UTF-8",
        dest="empty",
        mode="cors",
        site="same-origin",
        priority="u=1, i",
    )
    assert headers["Accept-Language"] == "de-DE,de;q=0.9,en;q=0.8"
    assert headers["Accept-Encoding"] == "gzip, deflate, br, zstd"
    assert headers["Sec-Fetch-Mode"] == "cors"
    assert headers["Priority"] == "u=1, i"
    assert headers["sec-ch-ua"] == chrome_profile["sec_ch_ua"]

    firefox_headers = sentinel_client._sentinel_wire_headers(
        {"impersonate": "firefox144", "user_agent": "Mozilla/5.0 Firefox/144.0", "lang_full": "ja-JP,ja;q=0.9"},
        referer="https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=test",
        accept="*/*",
        dest="empty",
        mode="cors",
        site="same-origin",
        priority="u=1, i",
    )
    assert "sec-ch-ua" not in firefox_headers
    assert firefox_headers["Accept-Language"] == "ja-JP,ja;q=0.9"


def test_reference_lane_excludes_rejected_signin_flags():
    assert "signin_screen_hint_login_or_signup" not in reference_lane.LANE_FLAGS
    assert "signin_prompt_login" not in reference_lane.LANE_FLAGS
    effective = reference_lane.apply_reference_lane({"registration": {"reference_lane": True}})
    assert effective["registration"].get("signin_screen_hint_login_or_signup") is not True
    assert effective["registration"].get("signin_prompt_login") is not True


def test_bridge_formats_ipv6_and_fails_over_dead_a_records():
    assert proxy_bridge._format_host_port("::1", 1080) == "[::1]:1080"
    assert proxy_bridge._format_host_port("127.0.0.1", 1080) == "127.0.0.1:1080"

    bridge = proxy_bridge.LocalProxyBridge(
        upstream=proxy_entry.parse_proxy("http://gw.example:8080"),
        connect_timeout=2.0,
    )
    attempted: list[str] = []

    async def fake_open(host, port, **kwargs):
        attempted.append(host)
        if host == "10.0.0.1":
            raise OSError("dead gateway ip")
        return Mock(), Mock()

    async def fake_getaddrinfo(host, port, type=0):
        return [
            (2, 1, 6, "", ("10.0.0.1", port)),
            (2, 1, 6, "", ("10.0.0.2", port)),
        ]

    async def run():
        loop = asyncio.get_running_loop()
        with (
            patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo),
            patch("sms_tool.proxy.bridge.asyncio.open_connection", side_effect=fake_open),
        ):
            await bridge._open_tcp_with_failover("gw.example", 8080, use_tls=False)
            # Second dial should try healthy 10.0.0.2 first.
            attempted.clear()
            await bridge._open_tcp_with_failover("gw.example", 8080, use_tls=False)

    asyncio.run(run())
    assert attempted == ["10.0.0.2"]
