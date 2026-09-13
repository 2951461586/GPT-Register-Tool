"""Unit tests for sms_tool.proxy_entry (unified proxy parser + pool loader)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from sms_tool.proxy_entry import (
    ProxyEntry,
    build_proxy_config,
    choose_proxy_entry,
    load_proxy_pool,
    parse_proxy,
    parse_proxy_list,
    proxy_to_url,
    infer_region,
    retarget_region,
    rotate_session,
    resolve_proxy_value,
)


class TestParseProxy(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(parse_proxy(""))
        self.assertIsNone(parse_proxy(None))

    def test_bare_4_segment(self):
        e = parse_proxy("gate.kookeey.info:1000:user:pass-JP")
        self.assertIsNotNone(e)
        self.assertEqual(e.host, "gate.kookeey.info")
        self.assertEqual(e.port, 1000)
        self.assertEqual(e.username, "user")
        self.assertEqual(e.password, "pass-JP")
        self.assertEqual(e.scheme, "http")  # default_scheme
        self.assertEqual(proxy_to_url(e), "http://user:pass-JP@gate.kookeey.info:1000")

    def test_url_with_auth(self):
        e = parse_proxy("http://user:pass@host:8080")
        self.assertEqual(e.host, "host")
        self.assertEqual(e.port, 8080)
        self.assertEqual(e.username, "user")
        self.assertEqual(e.password, "pass")

    def test_socks5_scheme_preserved(self):
        e = parse_proxy("socks5h://u:p@h:1080")
        self.assertEqual(e.scheme, "socks5h")
        self.assertEqual(proxy_to_url(e), "socks5h://u:p@h:1080")

    def test_socks_alias_normalized(self):
        e = parse_proxy("socks://u:p@h:1080")
        self.assertEqual(e.scheme, "socks5")

    def test_no_auth_host_port(self):
        e = parse_proxy("1.2.3.4:9999")
        self.assertEqual(e.host, "1.2.3.4")
        self.assertEqual(e.port, 9999)
        self.assertEqual(e.username, "")
        self.assertEqual(proxy_to_url(e), "http://1.2.3.4:9999")

    def test_socks_default_port(self):
        e = parse_proxy("socks5://u:p@h")
        self.assertEqual(e.port, 1080)

    def test_http_without_port_returns_none(self):
        self.assertIsNone(parse_proxy("http://host"))

    def test_http_without_port_is_rejected_across_all_forms(self):
        # The "http/https needs an explicit port" rule must hold no matter which
        # parse branch handles the input, not only the bare host branch.
        self.assertIsNone(parse_proxy("http://user:pass@host"))      # userinfo branch
        self.assertIsNone(parse_proxy("https://user:pass@host"))
        self.assertIsNone(parse_proxy("http://[::1]"))               # ipv6 branch
        self.assertIsNone(parse_proxy("user:pass@host"))             # implied http scheme

    def test_socks_defaults_port_across_all_forms(self):
        self.assertEqual(parse_proxy("socks5://u:p@h").port, 1080)   # userinfo branch
        self.assertEqual(parse_proxy("socks5://h").port, 1080)       # bare host branch
        self.assertEqual(parse_proxy("socks5://[::1]").port, 1080)   # ipv6 branch

    def test_port_zero_is_invalid(self):
        # Port 0 is never a real proxy port; a missing/zero http port stays
        # invalid and a zero socks port falls back to the socks default.
        self.assertIsNone(parse_proxy("http://host:0"))
        self.assertEqual(parse_proxy("socks5://host:0").port, 1080)

    def test_userinfo_scheme_implied(self):
        e = parse_proxy("user:pass@host:3128")
        self.assertEqual(e.scheme, "http")
        self.assertEqual(e.host, "host")
        self.assertEqual(e.port, 3128)

    def test_ipv6_bracketed_bare(self):
        e = parse_proxy("[::1]:8080:u:p")
        self.assertEqual(e.host, "::1")
        self.assertEqual(e.port, 8080)
        self.assertEqual(e.username, "u")
        self.assertEqual(e.password, "p")

    def test_ipv6_url(self):
        e = parse_proxy("http://[::1]:8080")
        self.assertEqual(e.host, "::1")
        self.assertEqual(e.port, 8080)

    def test_ipv6_url_with_auth(self):
        e = parse_proxy("socks5://u:p@[::1]:1080")
        self.assertEqual(e.host, "::1")
        self.assertEqual(e.port, 1080)
        self.assertEqual(e.username, "u")

    def test_unknown_scheme_returns_none(self):
        self.assertIsNone(parse_proxy("weird://host:1234"))

    def test_masked_does_not_leak_credentials(self):
        e = parse_proxy("http://user:secret@host:8080")
        self.assertNotIn("secret", e.masked)
        self.assertNotIn("user", e.masked)

    def test_repr_no_credentials(self):
        e = parse_proxy("http://user:secret@host:8080")
        self.assertNotIn("secret", repr(e))
        self.assertNotIn("user", repr(e))


class TestPoolAndChooser(unittest.TestCase):
    def test_parse_proxy_list_dedup(self):
        entries = parse_proxy_list(["h1:1:u:p", "h1:1:u:p", "h2:2:u2:p2"])
        self.assertEqual([(e.host, e.port) for e in entries], [("h1", 1), ("h2", 2)])

    @patch.dict(os.environ, {"PROXY_POOL": "h1:1:u:p,h2:2:u2:p2"}, clear=False)
    def test_load_proxy_pool_from_env(self):
        pool = load_proxy_pool({}, env_prefix="PROXY")
        hosts = [(e.host, e.port) for e in pool]
        self.assertIn(("h1", 1), hosts)
        self.assertIn(("h2", 2), hosts)

    @patch.dict(os.environ, {}, clear=False)
    def test_load_proxy_pool_from_config(self):
        cfg = {"paypal": {"proxy_pool": ["h1:1:u:p", "h2:2:u2:p2"]}}
        pool = load_proxy_pool(cfg)
        self.assertEqual([e.host for e in pool], ["h1", "h2"])

    def test_load_proxy_pool_skips_invalid(self):
        cfg = {"proxy_pool": ["", "bad", "h1:1:u:p"]}
        pool = load_proxy_pool(cfg)
        self.assertEqual([e.host for e in pool], ["h1"])

    def test_choose_proxy_entry_empty(self):
        self.assertIsNone(choose_proxy_entry([]))

    def test_choose_proxy_entry_by_index(self):
        pool = [ProxyEntry.parse("h1:1:u:p"), ProxyEntry.parse("h2:2:u:p")]
        self.assertEqual(choose_proxy_entry(pool, index=0).host, "h1")
        self.assertEqual(choose_proxy_entry(pool, index=1).host, "h2")
        # clamped / wrapped
        self.assertEqual(choose_proxy_entry(pool, index=5).host, "h2")

    @patch.dict(os.environ, {"PROXY_ENABLED": "1", "PROXY_POOL": "h1:1:u:p"}, clear=False)
    def test_build_proxy_config(self):
        cfg = build_proxy_config(config={}, env_prefix="PROXY")
        self.assertTrue(cfg["enabled"])
        self.assertIsNotNone(cfg["entry"])
        self.assertTrue(cfg["proxy_url"].startswith("http://"))

    @patch.dict(os.environ, {}, clear=False)
    def test_build_proxy_config_disabled(self):
        cfg = build_proxy_config(False, config={}, env_prefix="PROXY")
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["proxy_url"], "")


class TestProxyEntryClass(unittest.TestCase):
    def test_parse_classmethod(self):
        e = ProxyEntry.parse("socks5://1.2.3.4:1080")
        self.assertEqual(e.host, "1.2.3.4")
        self.assertEqual(e.port, 1080)
        self.assertEqual(e.scheme, "socks5")

    def test_is_socks(self):
        self.assertTrue(ProxyEntry.parse("socks5://h:1080").is_socks)
        self.assertFalse(ProxyEntry.parse("http://h:80").is_socks)

    def test_url_property(self):
        e = ProxyEntry.parse("host:8080:user:pass")
        self.assertEqual(e.url, "http://user:pass@host:8080")

    def test_to_dict(self):
        e = ProxyEntry.parse("host:8080:user:pass")
        d = e.to_dict()
        self.assertEqual(d["host"], "host")
        self.assertEqual(d["port"], 8080)
        self.assertIn("label", d)


class TestIpwoCountryTemplate(unittest.TestCase):
    def test_infers_custom_zone_country(self):
        proxy = "http://account_custom_zone_US:password@us.ipwo.net:7878"
        self.assertEqual(infer_region(proxy), "US")

    def test_retargets_custom_zone_country(self):
        proxy = "http://account_custom_zone_US:password@us.ipwo.net:7878"
        retargeted = retarget_region(proxy, "JP")
        self.assertIn("custom_zone_JP", retargeted)
        self.assertEqual(infer_region(retargeted), "JP")

    def test_rotation_retargets_country_without_requiring_session_token(self):
        proxy = "http://account_custom_zone_US:password@us.ipwo.net:7878"
        rotated = rotate_session(proxy, "GB")
        self.assertIn("custom_zone_GB", rotated)
        self.assertEqual(infer_region(rotated), "GB")


class TestNineHttpGeoCountryTemplate(unittest.TestCase):
    """9http / 9proxy use ``geo-XX`` where Cliproxy uses ``region-XX``.

    The region tag must be matched *and* preserved on rewrite: a naive
    ``region-`` only pattern leaves the credential unrecognised, while a
    capturing tag group makes ``infer_region`` return ``"GEO"`` instead of the
    country code (``match.group(1)`` is the country).
    """

    PROXY = "http://VSBFTHZC-geo-VN-sid-mwT3-ttl-5:A90IhxdPeq@global.9http.com:9091"

    def test_infers_geo_country_not_the_tag(self):
        self.assertEqual(infer_region(self.PROXY), "VN")

    def test_retarget_preserves_the_geo_tag(self):
        retargeted = retarget_region(self.PROXY, "US")
        self.assertIn("geo-US", retargeted)
        self.assertNotIn("region-US", retargeted)
        self.assertEqual(infer_region(retargeted), "US")

    def test_rotation_keeps_the_region(self):
        rotated = rotate_session(self.PROXY)
        self.assertEqual(infer_region(rotated), "VN")
        self.assertIn("geo-VN", rotated)

    def test_region_tag_still_supported(self):
        cliproxy = "http://user-region-JP-sid-abc:t-5@host:8080"
        self.assertEqual(infer_region(cliproxy), "JP")
        self.assertIn("region-GB", retarget_region(cliproxy, "GB"))


class TestRotationWithCountryPreservesTheTag(unittest.TestCase):
    """``rotate_session`` with a country must write the *original* tag back.

    Regression: it hardcoded ``region-`` while ``retarget_region`` preserved the
    tag, so rotating a 9http ``geo-VN`` credential to a country produced
    ``region-US`` -- which the provider rejects.  The old tests missed it because
    ``test_rotation_keeps_the_region`` calls ``rotate_session(PROXY)`` with **no**
    country, skipping the region branch entirely.

    Reachable in production: ``paypal_proxy.rotate_proxy_session`` passes a real
    country code.
    """

    PROXY = "http://VSBFTHZC-geo-VN-sid-mwT3-ttl-5:A90IhxdPeq@global.9http.com:9091"

    def test_rotation_with_country_keeps_the_geo_tag(self):
        rotated = rotate_session(self.PROXY, "US")
        self.assertIn("geo-US", rotated)
        self.assertNotIn("region-US", rotated)
        self.assertEqual(infer_region(rotated), "US")

    def test_rotation_with_country_also_refreshes_the_session_id(self):
        rotated = rotate_session(self.PROXY, "US")
        self.assertNotEqual(rotated, self.PROXY)
        self.assertNotIn("sid-mwT3-", rotated)


class TestRolaCountryTemplate(unittest.TestCase):
    """rola spells the region tag ``country``: ``BASE_<sid>-country-XX``.

    Probed against the live gateway on 2026-09-12: the provider accepts an
    arbitrary token in the sid slot (a random 4-char id returned a fresh VN
    egress IP) and is case-insensitive on the country code, so rotating the sid
    and rewriting the country are both safe.  Before this, ``rotate_session`` and
    ``retarget_region`` were silent no-ops on all 10 rola pool entries.
    """

    PROXY = "http://SYS433954tbr_1-country-vn:tWL%5E%23B@gate.rola.vip:2000"

    def test_infers_country_from_the_tag_not_the_tail(self):
        # The tag *name* must not leak into the result -- the 9http lesson: a
        # capturing tag group would make this return "COUNTRY".
        self.assertEqual(infer_region(self.PROXY), "VN")

    def test_retarget_preserves_the_country_tag(self):
        retargeted = retarget_region(self.PROXY, "US")
        self.assertIn("country-US", retargeted)
        self.assertNotIn("region-US", retargeted)
        self.assertNotIn("geo-US", retargeted)
        self.assertEqual(infer_region(retargeted), "US")

    def test_rotation_refreshes_the_sid_and_keeps_the_region(self):
        rotated = rotate_session(self.PROXY)
        self.assertNotEqual(rotated, self.PROXY)
        self.assertIn("country-vn", rotated)
        self.assertEqual(infer_region(rotated), "VN")

    def test_rotation_with_country_changes_both(self):
        rotated = rotate_session(self.PROXY, "US")
        self.assertIn("country-US", rotated)
        self.assertEqual(infer_region(rotated), "US")

    def test_credentials_survive_the_rotation(self):
        # The password contains ``^#``; hand-built URLs lose it to fragment
        # parsing, so this pins the ``ProxyEntry.url`` round trip.
        entry = parse_proxy(rotate_session(self.PROXY))
        self.assertIsNotNone(entry)
        self.assertEqual(entry.password, "tWL^#B")
        self.assertEqual(entry.host, "gate.rola.vip")
        self.assertEqual(entry.port, 2000)


class TestUnknownTemplateIsLeftAlone(unittest.TestCase):
    """Negative guard: an unrecognised credential comes back byte-identical.

    Without this, "make rola work" could be satisfied by loosening the patterns
    until they match anything -- which would rewrite credentials the provider
    never agreed to.
    """

    UNKNOWN = (
        "http://plainuser:plainpass@host:8080",
        "http://user-country-vietnam:pw@host:8080",  # tag present, not ISO-2
        "http://user-cc-vn:pw@host:8080",            # tag name not in the set
        "http://SYS433954tbr_1:pw@host:2000",        # rola shape minus the region
    )

    def test_retarget_is_a_noop(self):
        for proxy in self.UNKNOWN:
            with self.subTest(proxy=proxy):
                self.assertEqual(retarget_region(proxy, "US"), proxy)

    def test_rotation_is_a_noop(self):
        for proxy in self.UNKNOWN:
            with self.subTest(proxy=proxy):
                self.assertEqual(rotate_session(proxy, "US"), proxy)


class TestShortSessionIdNeverRotatesToItself(unittest.TestCase):
    """A 1-char sid must not rotate back to the same value.

    Measured on rola's ``_1``..``_10`` ids: one of ten rotations was a silent
    no-op, which keeps the same sticky session on a retry whose entire purpose is
    to change it.
    """

    def test_one_char_rola_sid_always_changes(self):
        for _ in range(200):
            rotated = rotate_session("http://SYS433954tbr_1-country-vn:pw@gate.rola.vip:2000")
            self.assertNotIn("tbr_1-", rotated)

    def test_two_char_rola_sid_always_changes(self):
        # `_10` is digits-only and two characters -- a re-roll-free
        # implementation collides 1 time in 100, too often to ignore.
        for _ in range(300):
            rotated = rotate_session("http://SYS433954tbr_10-country-vn:pw@gate.rola.vip:2000")
            self.assertNotIn("tbr_10-", rotated)

    def test_one_char_rola_sid_stays_numeric(self):
        # The original slot is a digit; keep the alphabet so the credential keeps
        # looking like the provider's own format.
        for _ in range(50):
            rotated = rotate_session("http://SYS433954tbr_1-country-vn:pw@gate.rola.vip:2000")
            sid = rotated.split("://", 1)[1].split("@", 1)[0].split("_", 1)[1].split("-", 1)[0]
            self.assertTrue(sid.isdigit(), sid)


class TestResolveProxyValue(unittest.TestCase):
    """--proxy single-value resolution (pool / bare credential / URL)."""

    def test_empty_returns_empty(self):
        self.assertEqual(resolve_proxy_value(""), "")
        self.assertEqual(resolve_proxy_value(None), "")

    def test_single_url_passthrough(self):
        self.assertEqual(
            resolve_proxy_value("http://u:p@host:8080"),
            "http://u:p@host:8080",
        )

    def test_bare_credential_normalized(self):
        self.assertEqual(
            resolve_proxy_value("gate.kookeey.info:1000:user:pass-JP"),
            "http://user:pass-JP@gate.kookeey.info:1000",
        )

    def test_bare_credential_accepts_full_width_colons(self):
        entry = parse_proxy("gate.example:8080：user：pass")
        self.assertEqual(entry.url, "http://user:pass@gate.example:8080")

    def test_pool_picks_first_usable(self):
        self.assertEqual(
            resolve_proxy_value("bad,h1:1:u:p,socks5://h2:1080"),
            "http://u:p@h1:1",
        )

    def test_pool_newline_separated(self):
        self.assertEqual(
            resolve_proxy_value("h1:1:u:p\nh2:2:u2:p2"),
            "http://u:p@h1:1",
        )

    def test_socks_pool_kept_scheme(self):
        self.assertEqual(
            resolve_proxy_value("socks5h://u:p@h:1080"),
            "socks5h://u:p@h:1080",
        )

    def test_all_invalid_returns_empty(self):
        self.assertEqual(resolve_proxy_value("bad,weird://h:1"), "")


def test_csharp_normalized_pool_entries_round_trip_through_proxy_entry():
    """C# 归一器写出的代理值必须能被唯一权威 parse_proxy 无损解析。

    两侧消费同一份 tests/fixtures/proxy_input_cases.json；C# 侧断言
    Normalize(operator_input) == csharp_normalized（ProxyInputNormalizerTests），
    本侧断言 csharp_normalized 解析后语义字段无损。
    """
    import json
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "proxy_input_cases.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    assert cases
    for case in cases:
        entry = parse_proxy(case["csharp_normalized"], default_scheme="socks5")
        assert entry is not None, case["name"]
        assert entry.scheme == case["scheme"], case["name"]
        assert entry.host == case["host"], case["name"]
        assert entry.port == case["port"], case["name"]
        assert entry.username == case["username"], case["name"]
        assert entry.password == case["password"], case["name"]

if __name__ == "__main__":
    unittest.main()
