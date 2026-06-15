import unittest
from unittest.mock import patch

from sms_tool import gen_pp_link


class GeneratePpLinkContractTests(unittest.TestCase):
    def test_target_country_override_is_passed_to_extractor(self):
        seen = {}

        class FakeExtractor:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def extract(self):
                return {
                    "ok": True,
                    "url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
                    "ba_token": "BA-test",
                    "cs_id": "cs_test",
                    "link_type": "paypal_ba_approve",
                    "amount": 0,
                    "currency": "EUR",
                    "target_country": seen["target_country"],
                    "checkout_proxy": seen.get("checkout_proxy", ""),
                    "provider_proxy": seen.get("provider_proxy", ""),
                    "approve_proxy": seen.get("approve_proxy", ""),
                }

        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"target_country": "GB", "require_zero_due": True}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": ""}):
                with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                    result = gen_pp_link.generate_pp_link("at", target_country="DE", require_zero=True, require_ba_token=True)

        self.assertTrue(result["ok"])
        self.assertEqual(seen["target_country"], "DE")
        self.assertTrue(seen["require_zero"])
        self.assertEqual(result["target_country"], "DE")

    def test_require_ba_token_rejects_hosted_fallback(self):
        class FakeExtractor:
            def __init__(self, **kwargs):
                pass

            def extract(self):
                return {
                    "ok": True,
                    "url": "https://checkout.stripe.com/c/pay/cs_test",
                    "ba_token": "",
                    "cs_id": "cs_test",
                    "link_type": "stripe_hosted",
                    "amount": 1984,
                    "currency": "GBP",
                    "target_country": "GB",
                }

        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"target_country": "GB", "require_zero_due": False}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": ""}):
                with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                    result = gen_pp_link.generate_pp_link("at", require_zero=False, require_ba_token=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ba_not_resolved")
        self.assertEqual(result["url"], "")
        self.assertEqual(result["ba_token"], "")
        self.assertEqual(result["fallback_url"], "https://checkout.stripe.com/c/pay/cs_test")
        self.assertEqual(result["amount"], 1984)
        self.assertEqual(result["currency"], "GBP")

    def test_hosted_fallback_still_allowed_when_ba_not_required(self):
        class FakeExtractor:
            def __init__(self, **kwargs):
                pass

            def extract(self):
                return {
                    "ok": True,
                    "url": "https://checkout.stripe.com/c/pay/cs_test",
                    "ba_token": "",
                    "cs_id": "cs_test",
                    "link_type": "stripe_hosted",
                    "amount": 1984,
                    "currency": "GBP",
                    "target_country": "GB",
                }

        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"target_country": "GB", "require_zero_due": False}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": ""}):
                with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                    result = gen_pp_link.generate_pp_link("at", require_zero=False, require_ba_token=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://checkout.stripe.com/c/pay/cs_test")
        self.assertEqual(result["link_type"], "stripe_hosted")

    def test_load_json_accepts_utf8_bom_config(self):
        import json
        import tempfile
        from pathlib import Path

        payload = {
            "paypal": {
                "target_country": "GB",
                "stage_proxies": {"checkout": "socks5h://user-region-JP:pass@example:443"},
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("\ufeff" + json.dumps(payload), encoding="utf-8")

            loaded = gen_pp_link._load_json(str(path))

        self.assertEqual(loaded["paypal"]["target_country"], "GB")
        self.assertEqual(
            loaded["paypal"]["stage_proxies"]["checkout"],
            "socks5h://user-region-JP:pass@example:443",
        )

    def test_proxies_from_bom_loaded_config_keeps_checkout_proxy(self):
        import json
        import tempfile
        from pathlib import Path

        payload = {
            "paypal": {
                "stage_proxies": {
                    "checkout": "socks5h://user-region-JP:pass@example:443",
                    "stripe_init": "http://127.0.0.1:11001",
                    "confirm": "http://127.0.0.1:11002",
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("\ufeff" + json.dumps(payload), encoding="utf-8")
            loaded = gen_pp_link._load_json(str(path))

        proxies = gen_pp_link._proxies_from_config(loaded)
        self.assertEqual(proxies["checkout"], "socks5h://user-region-JP:pass@example:443")
        self.assertEqual(proxies["provider"], "http://127.0.0.1:11001")
        self.assertEqual(proxies["approve"], "http://127.0.0.1:11002")


if __name__ == "__main__":
    unittest.main()
