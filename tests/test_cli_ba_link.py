import unittest
from unittest.mock import patch

from sms_tool import cli


class GenerateBaLinkCliProxyTests(unittest.TestCase):
    def test_generate_ba_link_prefers_stage_proxies_when_proxy_not_explicit(self):
        seen = {}
        cfg = {
            "proxy": {"default": "socks5h://default-proxy"},
            "paypal": {
                "stage_proxies": {
                    "checkout": "socks5h://checkout-proxy",
                    "provider": "http://provider-proxy:11001",
                    "approve": "http://approve-proxy:11002",
                }
            },
            "output": {"directory": "sessions"},
        }

        def fake_generate_pp_link(**kwargs):
            seen.update(kwargs)
            return {
                "ok": True,
                "url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
                "ba_token": "BA-test",
            }

        argv = [
            "chatgpt_phone_reg.py",
            "--generate-ba-link",
            "--at",
            "at-test",
            "--target-country",
            "GB",
            "--require-ba-token",
        ]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=fake_generate_pp_link):
                    cli.main()

        self.assertIsNone(seen["proxy"])
        self.assertEqual(seen["checkout_proxy"], "socks5h://checkout-proxy")
        self.assertEqual(seen["provider_proxy"], "http://provider-proxy:11001")
        self.assertEqual(seen["approve_proxy"], "http://approve-proxy:11002")

    def test_generate_ba_link_keeps_explicit_single_proxy(self):
        seen = {}
        cfg = {
            "proxy": {"default": "socks5h://default-proxy"},
            "paypal": {
                "stage_proxies": {
                    "checkout": "socks5h://checkout-proxy",
                    "provider": "http://provider-proxy:11001",
                    "approve": "http://approve-proxy:11002",
                }
            },
            "output": {"directory": "sessions"},
        }

        def fake_generate_pp_link(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "url": "https://www.paypal.com/agreements/approve?ba_token=BA-test"}

        argv = [
            "chatgpt_phone_reg.py",
            "--generate-ba-link",
            "--at",
            "at-test",
            "--proxy",
            "http://explicit-proxy:8080",
        ]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=fake_generate_pp_link):
                    cli.main()

        self.assertEqual(seen["proxy"], "http://explicit-proxy:8080")
        self.assertIsNone(seen["checkout_proxy"])
        self.assertIsNone(seen["provider_proxy"])
        self.assertIsNone(seen["approve_proxy"])




    def test_generate_chatgpt_checkout_link_uses_checkout_country(self):
        seen = {}
        cfg = {"paypal": {"target_country": "US", "billing_regions": ["JP"]}, "output": {"directory": "sessions"}}

        def fake_generate_pp_link(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "url": "https://chatgpt.com/checkout/openai_llc/cs_live_TEST"}

        argv = [
            "chatgpt_phone_reg.py",
            "--generate-ba-link",
            "--at",
            "at-test",
            "--paypal-generation-type",
            "chatgpt_checkout_link",
            "--target-country",
            "US",
            "--checkout-country",
            "JP",
        ]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=fake_generate_pp_link):
                    cli.main()

        self.assertEqual(seen["paypal_generation_type"], "chatgpt_checkout_link")
        self.assertEqual(seen["target_country"], "US")
        self.assertEqual(seen["checkout_country"], "JP")

    def test_generate_hosted_long_url_uses_checkout_country_and_custom_proxy(self):
        seen = {}
        cfg = {
            "proxy": {"default": "socks5h://default-proxy"},
            "paypal": {
                "link_generation_type": "paypal_direct",
                "billing_regions": ["JP"],
                "stage_proxies": {
                    "checkout": "socks5h://checkout-proxy",
                    "provider": "http://provider-proxy:11001",
                    "approve": "http://approve-proxy:11002",
                }
            },
            "output": {"directory": "sessions"},
        }

        def fake_generate_pp_link(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "url": "https://pay.openai.com/c/pay/cs_live_TEST#fid", "short_url": "https://pay.openai.com/c/pay/cs_live_TEST"}

        argv = [
            "chatgpt_phone_reg.py",
            "--generate-ba-link",
            "--at",
            "at-test",
            "--paypal-generation-type",
            "hosted_long_url",
            "--target-country",
            "GB",
            "--checkout-country",
            "US",
            "--proxy",
            "socks5h://127.0.0.1:7897",
        ]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=fake_generate_pp_link):
                    cli.main()

        self.assertEqual(seen["proxy"], "socks5h://127.0.0.1:7897")
        self.assertIsNone(seen["checkout_proxy"])
        self.assertIsNone(seen["provider_proxy"])
        self.assertEqual(seen["target_country"], "GB")
        self.assertEqual(seen["checkout_country"], "US")
        self.assertEqual(seen["paypal_generation_type"], "hosted_long_url")
        self.assertFalse(seen["require_ba_token"])

    def test_generate_upi_qr_prefers_upi_stage_proxies_when_proxy_not_explicit(self):
        seen = {}
        cfg = {
            "proxy": {"default": "socks5h://default-proxy"},
            "paypal": {
                "stage_proxies": {
                    "checkout": "socks5h://paypal-checkout",
                    "provider": "http://paypal-provider:11001",
                    "approve": "http://paypal-approve:11002",
                }
            },
            "upi": {
                "stage_proxies": {
                    "checkout": "socks5h://jp-checkout",
                    "provider": "http://in-provider:11001",
                    "approve": "http://in-approve:11002",
                },
                "checkout_country": "JP",
                "payment_country": "IN",
            },
            "output": {"directory": "sessions"},
        }

        def fake_generate_upi_qr_link(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "url": "https://pay.openai.com/c/pay/cs_live_UPI", "qr_path": "runtime/upi_qr/test.png"}

        argv = ["chatgpt_phone_reg.py", "--generate-upi-qr", "--at", "at-test"]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.gen_pp_link.generate_upi_qr_link", side_effect=fake_generate_upi_qr_link):
                    cli.main()

        self.assertIsNone(seen["proxy"])
        self.assertEqual(seen["checkout_proxy"], "socks5h://jp-checkout")
        self.assertEqual(seen["provider_proxy"], "http://in-provider:11001")
        self.assertEqual(seen["approve_proxy"], "http://in-approve:11002")
        self.assertEqual(seen["target_country"], "JP")
        self.assertEqual(seen["checkout_country"], "JP")
        self.assertEqual(seen["payment_country"], "IN")

    def test_generate_upi_qr_falls_back_to_paypal_checkout_and_india_provider(self):
        seen = {}
        cfg = {
            "proxy": {"default": "socks5h://default-proxy"},
            "paypal": {"stage_proxies": {"checkout": "socks5h://jp-checkout"}},
            "upi": {},
            "output": {"directory": "sessions"},
        }

        def fake_generate_upi_qr_link(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "url": "https://pay.openai.com/c/pay/cs_live_UPI"}

        argv = ["chatgpt_phone_reg.py", "--generate-upi-qr", "--at", "at-test"]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.gen_pp_link.generate_upi_qr_link", side_effect=fake_generate_upi_qr_link):
                    cli.main()

        self.assertEqual(seen["checkout_proxy"], "socks5h://jp-checkout")
        self.assertEqual(seen["provider_proxy"], "http://107.150.109.49:11001")
        self.assertEqual(seen["approve_proxy"], "http://107.150.109.49:11001")


    def test_generate_upi_qr_cli_country_overrides_are_split(self):
        seen = {}
        cfg = {"upi": {"checkout_country": "IN", "payment_country": "IN"}, "output": {"directory": "sessions"}}

        def fake_generate_upi_qr_link(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "url": "https://pay.openai.com/c/pay/cs_live_UPI"}

        argv = [
            "chatgpt_phone_reg.py",
            "--generate-upi-qr",
            "--at",
            "at-test",
            "--checkout-country",
            "JP",
            "--payment-country",
            "IN",
        ]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.gen_pp_link.generate_upi_qr_link", side_effect=fake_generate_upi_qr_link):
                    cli.main()

        self.assertEqual(seen["target_country"], "JP")
        self.assertEqual(seen["checkout_country"], "JP")
        self.assertEqual(seen["payment_country"], "IN")


if __name__ == "__main__":
    unittest.main()
