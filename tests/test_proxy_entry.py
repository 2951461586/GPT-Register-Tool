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


if __name__ == "__main__":
    unittest.main()
