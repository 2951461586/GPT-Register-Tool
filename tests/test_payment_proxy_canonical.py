import unittest

from sms_tool.payment_routing import parse_proxy_pool


class PaymentProxyCanonicalTests(unittest.TestCase):
    def test_pool_normalizes_provider_four_part_form(self):
        self.assertEqual(
            parse_proxy_pool("proxy.example:8080:user:pass"),
            ["http://user:pass@proxy.example:8080"],
        )

    def test_pool_normalizes_ipv6_and_socks_alias(self):
        self.assertEqual(
            parse_proxy_pool("socks://[::1]:1080"),
            ["socks5://[::1]:1080"],
        )

    def test_invalid_entries_are_rejected_before_route_planning(self):
        self.assertEqual(parse_proxy_pool("not-a-proxy,https://valid.example:443"), ["https://valid.example:443"])


if __name__ == "__main__":
    unittest.main()
