import unittest
import tempfile
import io
import json
from pathlib import Path
from unittest.mock import patch

from sms_tool import cli


class GenerateBaLinkCliProxyTests(unittest.TestCase):
    def test_extract_payment_link_uses_payment_pool_fallback_for_momo(self):
        seen = {}
        cfg = {
            "protocol_payments": {
                "proxy_pool": [
                    "http://first-region-JP-sid-Ab12Cd34-t-5:secret@sg.cliproxy.io:443",
                    "http://second-region-JP-sid-Ef56Gh78-t-10:secret@as.zooproxy.com:443",
                ]
            },
            "output": {"directory": "sessions"},
        }

        def fake_generate_payment_link(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "url": "https://payment.momo.vn/test"}

        def fake_probe(proxy, expected_country="", stage="proxy", timeout=12):
            from sms_tool.paypal_proxy import ProxyProbeResult
            ok = "as.zooproxy.com" in proxy
            return ProxyProbeResult(ok, stage, expected_country, country_code=expected_country if ok else "", error="timeout" if not ok else "")

        argv = [
            "chatgpt_phone_reg.py",
            "--extract-payment-link",
            "--at",
            "at-test",
            "--payment-method",
            "momo",
        ]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.paypal_proxy.probe_proxy", side_effect=fake_probe):
                    with patch("sms_tool.payment_link_manager.generate_payment_link", side_effect=fake_generate_payment_link):
                        cli.main()

        self.assertIn("as.zooproxy.com", seen["proxy"])
        self.assertIn("region-VN", seen["proxy"])
        self.assertEqual(seen["target_country"], "VN")

    def test_extract_payment_link_uses_selected_account_access_token(self):
        seen = {}
        cfg = {"paypal": {}, "output": {"directory": "sessions"}}

        def fake_generate_payment_link(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "url": "https://example.test/pay"}

        argv = [
            "chatgpt_phone_reg.py",
            "--extract-payment-link",
            "--email",
            "selected@example.com",
            "--payment-method",
            "paypal",
        ]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.session_refresh._load_seed_session", return_value=({
                    "email": "selected@example.com",
                    "access_token": "selected-at",
                    "cookie_header": "session=cookie",
                }, "session.json")):
                    with patch("sms_tool.payment_link_manager.generate_payment_link", side_effect=fake_generate_payment_link):
                        cli.main()

        self.assertEqual(seen["access_token"], "selected-at")
        self.assertEqual(seen["auth_context"]["email"], "selected@example.com")

    def test_extract_payment_link_rotates_configured_stage_proxies_to_selected_countries(self):
        seen = {}
        cfg = {
            "paypal": {
                "stage_proxies": {
                    "checkout": "http://user:base-US-12345678-5m@gate.example:1000",
                    "provider": "http://user:base-GB-12345678-5m@gate.example:1000",
                    "approve": "http://user:base-TR-12345678-5m@gate.example:1000",
                    "promotion": "http://user:base-TR-12345678-5m@gate.example:1000",
                }
            },
            "output": {"directory": "sessions"},
        }

        def fake_generate_payment_link(**kwargs):
            seen.update(kwargs)
            return {"ok": True, "url": "https://example.test/pay"}

        argv = [
            "chatgpt_phone_reg.py",
            "--extract-payment-link",
            "--at",
            "at-test",
            "--checkout-proxy-country",
            "JP",
            "--approve-proxy-country",
            "DE",
            "--update-proxy-country",
            "BR",
        ]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.payment_link_manager.generate_payment_link", side_effect=fake_generate_payment_link):
                    cli.main()

        self.assertIn("base-JP-", seen["checkout_proxy"])
        self.assertIn("base-DE-", seen["approve_proxy"])
        self.assertIn("base-BR-", seen["promotion_proxy"])
        self.assertEqual(seen["stage_proxy_countries"], {"checkout": "JP", "approve": "DE", "promotion": "BR"})

    def test_payment_proxy_probe_reports_three_operator_stages(self):
        cfg = {
            "paypal": {
                "stage_proxies": {
                    "checkout": "http://checkout.example:1000",
                    "approve": "http://approve.example:1000",
                    "promotion": "http://update.example:1000",
                }
            },
            "output": {"directory": "sessions"},
        }

        def fake_probe(proxy, expected_country="", stage="proxy", timeout=12):
            from sms_tool.paypal_proxy import ProxyProbeResult
            return ProxyProbeResult(True, stage, expected_country, "203.0.113.10", expected_country, "Test")

        stdout = io.StringIO()
        argv = [
            "chatgpt_phone_reg.py",
            "--test-payment-proxies",
            "--checkout-proxy-country",
            "US",
            "--approve-proxy-country",
            "GB",
            "--update-proxy-country",
            "JP",
        ]
        with patch.object(cli, "CFG", cfg):
            with patch("sys.argv", argv):
                with patch("sms_tool.paypal_proxy.probe_proxy", side_effect=fake_probe):
                    with patch("sys.stdout", stdout):
                        cli.main()

        result = json.loads(stdout.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(set(result["stages"]), {"checkout", "approve", "update"})
        self.assertEqual(result["stages"]["update"]["expected_country"], "JP")

    def test_batch_regenerate_forwards_stage_proxy_overrides(self):
        seen = []

        def fake_regenerate(**kwargs):
            seen.append(kwargs)
            return {"ok": True, "email": kwargs["email"]}

        with tempfile.TemporaryDirectory() as tmp:
            email_file = Path(tmp) / "emails.txt"
            email_file.write_text("one@example.com\n", encoding="utf-8")
            argv = [
                "chatgpt_phone_reg.py",
                "--regenerate-paypal-link",
                "--email-file",
                str(email_file),
                "--checkout-proxy",
                "http://checkout",
                "--provider-proxy",
                "http://provider",
                "--approve-proxy",
                "http://approve",
                "--promotion-proxy",
                "http://promotion",
                "--no-require-zero",
            ]
            with patch("sys.argv", argv):
                with patch("sms_tool.paypal_links.regenerate_paypal_link", side_effect=fake_regenerate):
                    cli.main()

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["checkout_proxy"], "http://checkout")
        self.assertEqual(seen[0]["provider_proxy"], "http://provider")
        self.assertEqual(seen[0]["approve_proxy"], "http://approve")
        self.assertEqual(seen[0]["promotion_proxy"], "http://promotion")
        self.assertFalse(seen[0]["require_zero"])

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
