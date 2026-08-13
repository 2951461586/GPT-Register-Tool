"""Proxy scheme resolution must not silently downgrade a healthy socks5 proxy.

A mislabeled provider proxy (``socks5h://`` that is really HTTP CONNECT) may
still be corrected, but the correction is now gated on the socks5 endpoint
actually failing a reachability check against the real auth edge, and the
``registration.proxy_scheme_fallback=off`` switch pins the declared scheme.
"""

import unittest
from unittest.mock import patch

from sms_tool import registration_preflight as preflight


class ResolveProxySchemeTests(unittest.TestCase):
    def test_reachable_socks5_scheme_is_kept(self):
        seen = []

        def fake_reachable(candidate, url):
            seen.append(candidate)
            return candidate.startswith("socks5h://")

        with patch.object(preflight, "_proxy_scheme_reachable", side_effect=fake_reachable):
            resolved = preflight._resolve_proxy_scheme("socks5h://u:p@host:1080")

        self.assertEqual(resolved, "socks5h://u:p@host:1080")
        # A working socks5 endpoint must never be probed as http.
        self.assertTrue(all(c.startswith("socks5h://") for c in seen))

    def test_mislabeled_socks5_downgrades_to_http_when_socks_unreachable(self):
        def fake_reachable(candidate, url):
            return candidate.startswith("http://")

        with patch.object(preflight, "_proxy_scheme_reachable", side_effect=fake_reachable):
            resolved = preflight._resolve_proxy_scheme("socks5h://u:p@host:1080")

        self.assertEqual(resolved, "http://u:p@host:1080")

    def test_fallback_off_pins_declared_scheme_even_when_socks_unreachable(self):
        attempted = []

        def fake_reachable(candidate, url):
            attempted.append(candidate)
            return False

        cfg = {"registration": {"proxy_scheme_fallback": "off"}}
        with patch.object(preflight, "_proxy_scheme_reachable", side_effect=fake_reachable):
            resolved = preflight._resolve_proxy_scheme("socks5h://u:p@host:1080", cfg=cfg)

        self.assertEqual(resolved, "socks5h://u:p@host:1080")
        # With fallback off, only the declared socks scheme is tested; the http
        # transport is never attempted.
        self.assertTrue(all(c.startswith("socks5h://") for c in attempted))

    def test_non_socks_proxy_is_returned_unchanged_without_probing(self):
        with patch.object(preflight, "_proxy_scheme_reachable", side_effect=AssertionError("must not probe")):
            self.assertEqual(
                preflight._resolve_proxy_scheme("http://u:p@host:8080"),
                "http://u:p@host:8080",
            )

    def test_both_schemes_unreachable_keeps_original(self):
        with patch.object(preflight, "_proxy_scheme_reachable", return_value=False):
            resolved = preflight._resolve_proxy_scheme("socks5://u:p@host:1080")
        self.assertEqual(resolved, "socks5://u:p@host:1080")


if __name__ == "__main__":
    unittest.main()
