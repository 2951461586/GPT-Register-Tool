import unittest
from unittest.mock import patch

from sms_tool import gen_pp_link
from sms_tool import paypal_extract
from sms_tool import upi_link


class GeneratePpLinkContractTests(unittest.TestCase):
    def test_runtime_config_uses_canonical_paypal_stage_countries(self):
        cfg = {
            "paypal": {
                "stage_proxy_countries": {
                    "checkout": "JP",
                    "provider": "JP",
                    "stripe_init": "JP",
                    "approve": "JP",
                }
            },
            "protocol_payments": {
                "methods": {
                    "paypal": {
                        "stage_proxy_countries": {"checkout": "US", "approve": "US"}
                    }
                }
            },
        }

        self.assertEqual(
            gen_pp_link._paypal_config(cfg)["stage_proxy_countries"],
            {"checkout": "US", "approve": "US"},
        )

    def test_strict_zero_due_rejects_unknown_amount(self):
        class FakeResponse:
            status_code = 200
            text = "{}"

            def json(self):
                return {}

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakeResponse()

        extractor = gen_pp_link.PPLinkExtractor("at", require_zero=True)
        extractor._stripe_session = FakeSession()

        with self.assertRaises(gen_pp_link.CheckoutNotZeroDueError):
            extractor._stripe_init("cs_test")

    def test_zero_due_generation_type_forces_zero_and_ba_requirements(self):
        seen = {}

        class FakeExtractor:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def extract(self):
                return {
                    "ok": True,
                    "url": "https://checkout.stripe.com/c/pay/cs_test",
                    "ba_token": "",
                    "link_type": "stripe_hosted",
                }

        cfg = {"paypal": {"require_zero_due": False, "require_ba_token": False}}
        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": "", "promotion": ""}):
                with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                    result = gen_pp_link.generate_pp_link(
                        "at",
                        paypal_generation_type="paypal_direct_zero_due",
                    )

        self.assertTrue(seen["require_zero"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ba_not_resolved")

    def test_hosted_strict_zero_rejects_unknown_amount(self):
        class FakeResponse:
            def __init__(self, body):
                self.status_code = 200
                self._body = body
                self.text = "{}"
                self.headers = {}

            def json(self):
                return self._body

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakeResponse({})

        checkout = FakeResponse({
            "checkout_session_id": "cs_test",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test",
        })
        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"require_zero_due": True}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": "", "promotion": ""}):
                with patch.object(gen_pp_link, "_checkout_post", return_value=checkout):
                    with patch.object(gen_pp_link, "_new_session", return_value=FakeSession()):
                        result = gen_pp_link.generate_hosted_long_url("at", require_zero=True)

        # 金额取不到时按协议模糊归为 unknown，不能当 0 元放行也不能当非零误杀。
        # 该路径不进 ok=True 的成功分支，ok=False；error_code 区分"金额未知"。
        self.assertFalse(result["ok"])
        self.assertIn(result["error_code"], {"checkout_not_zero_due", "checkout_amount_unknown"})
        self.assertIsNone(result["amount"])

    def test_default_proxy_does_not_override_configured_stage_proxies(self):
        seen = {}

        class FakeExtractor:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def extract(self):
                return {
                    "ok": True,
                    "url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
                    "ba_token": "BA-test",
                    "link_type": "paypal_ba_approve",
                }

        cfg = {
            "paypal": {
                "explicit_proxy_overrides_stage_proxies": False,
                "stage_proxies": {
                    "checkout": "http://checkout",
                    "provider": "http://provider",
                    "approve": "http://approve",
                },
            }
        }
        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                result = gen_pp_link.generate_pp_link("at", proxy="http://default")

        self.assertTrue(result["ok"])
        self.assertEqual(seen["checkout_proxy"], "http://checkout")
        self.assertEqual(seen["provider_proxy"], "http://provider")
        self.assertEqual(seen["approve_proxy"], "http://approve")

    def test_operator_stage_country_overrides_configured_preflight_countries(self):
        seen = {}

        class FakeExtractor:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def extract(self):
                return {
                    "ok": True,
                    "url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
                    "ba_token": "BA-test",
                    "link_type": "paypal_ba_approve",
                }

        cfg = {
            "paypal": {
                "stage_proxy_countries": {"checkout": "US", "approve": "TR", "promotion": "TR"},
                "stage_proxies": {
                    "checkout": "http://checkout",
                    "provider": "http://provider",
                    "approve": "http://approve",
                    "promotion": "http://promotion",
                },
            }
        }
        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                result = gen_pp_link.generate_pp_link(
                    "at",
                    stage_proxy_countries={"checkout": "JP", "approve": "DE", "promotion": "BR"},
                )

        self.assertTrue(result["ok"])
        self.assertEqual(seen["stage_proxy_countries"]["checkout"], "JP")
        self.assertEqual(seen["stage_proxy_countries"]["approve"], "DE")
        self.assertEqual(seen["stage_proxy_countries"]["promotion"], "BR")

    def test_explicit_proxy_can_override_stages_when_enabled(self):
        seen = {}

        class FakeExtractor:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def extract(self):
                return {
                    "ok": True,
                    "url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
                    "ba_token": "BA-test",
                    "link_type": "paypal_ba_approve",
                }

        cfg = {
            "paypal": {
                "explicit_proxy_overrides_stage_proxies": True,
                "stage_proxies": {
                    "checkout": "http://checkout",
                    "provider": "http://provider",
                    "approve": "http://approve",
                },
            }
        }
        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                result = gen_pp_link.generate_pp_link("at", proxy="http://explicit")

        self.assertTrue(result["ok"])
        self.assertEqual(seen["checkout_proxy"], "http://explicit")
        self.assertEqual(seen["provider_proxy"], "http://explicit")
        self.assertEqual(seen["approve_proxy"], "http://explicit")

    def test_supported_billing_countries_do_not_reuse_german_address(self):
        germany = gen_pp_link.billing_for_country("DE")

        for country in ("IN", "BR"):
            billing = gen_pp_link.billing_for_country(country)
            self.assertEqual(billing["country"], country)
            self.assertNotEqual(billing["street"], germany["street"])

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

    def test_oaics_checkout_link_preserves_no_side_effect_contract(self):
        class FakeExtractor:
            def __init__(self, **kwargs):
                pass

            def extract(self):
                return {
                    "ok": True,
                    "url": "https://chatgpt.com/checkout/openai_ie/oaics_fixture",
                    "ba_token": "",
                    "cs_id": "oaics_fixture",
                    "link_type": "chatgpt_checkout_link",
                    "currency": "GBP",
                    "target_country": "GB",
                    "checkout_country": "GB",
                    "side_effect_started": False,
                }

        config = {"paypal": {"require_zero_due": True, "require_ba_token": False}}
        with patch.object(gen_pp_link, "_load_json", return_value=config):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": "", "promotion": ""}):
                with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                    result = gen_pp_link.generate_pp_link(
                        "at", target_country="GB", checkout_country="GB"
                    )

        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "chatgpt_checkout_link")
        self.assertFalse(result["side_effect_started"])




    def test_chatgpt_checkout_link_returns_chatgpt_checkout_url_without_stripe_init(self):
        posted = []

        class FakeResponse:
            status_code = 200
            text = "{}"
            headers = {}

            def json(self):
                return {
                    "checkout_session_id": "cs_live_CHATGPT",
                    "processor_entity": "openai_llc",
                    "publishable_key": "pk_test_unused",
                }

            def raise_for_status(self):
                pass

        class FakeSession:
            def __init__(self, proxy):
                self.headers = {}

            def post(self, url, json=None, data=None, timeout=None, headers=None):
                posted.append((url, json, data))
                raise AssertionError(f"unexpected Stripe call: {url}")

        def fake_checkout_post(url, json_body, access_token, cookie_header="", proxy="", timeout=30):
            posted.append((url, json_body, None))
            if url.endswith("/backend-api/payments/checkout"):
                return FakeResponse()
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"link_generation_type": "chatgpt_checkout_link", "target_country": "US", "billing_regions": ["US"]}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": ""}):
                with patch.object(gen_pp_link, "_new_session", side_effect=lambda proxy="": FakeSession(proxy)):
                    with patch.object(gen_pp_link, "_checkout_post", side_effect=fake_checkout_post):
                        result = gen_pp_link.generate_pp_link("at", paypal_generation_type="chatgpt_checkout_link", target_country="US", checkout_country="US")

        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://chatgpt.com/checkout/openai_llc/cs_live_CHATGPT")
        self.assertEqual(result["link_type"], "chatgpt_checkout_link")
        self.assertEqual(result["short_url"], result["url"])
        self.assertEqual(result["target_country"], "US")
        self.assertEqual(result["checkout_country"], "US")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][1]["checkout_ui_mode"], "custom")


    def test_hosted_generation_uses_jp_checkout_and_short_pay_url(self):
        posted = []

        class FakeResponse:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body
                self.text = "{}"
                self.headers = {}

            def json(self):
                return self._body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"http {self.status_code}")

        class FakeSession:
            def __init__(self, proxy):
                self.proxy = proxy
                self.headers = {}

            def post(self, url, json=None, data=None, timeout=None):
                posted.append((self.proxy, url, json, data))
                if "/payment_pages/cs_live_SHORT/init" in url:
                    return FakeResponse(200, {
                        "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_SHORT#fragment",
                        "payment_method_types": ["card", "paypal"],
                        "currency": "jpy",
                        "total_summary": {"due": 0, "currency": "jpy"},
                    })
                raise AssertionError(url)

        def fake_checkout_post(url, json_body, access_token, cookie_header="", proxy="", timeout=30):
            posted.append((proxy, url, json_body, None))
            if url.endswith("/backend-api/payments/checkout"):
                return FakeResponse(200, {
                    "checkout_session_id": "cs_live_SHORT",
                    "processor_entity": "openai_ie",
                    "publishable_key": "pk_test",
                })
            raise AssertionError(url)

        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"link_generation_type": "hosted_long_url", "billing_regions": ["JP"], "require_zero_due": True}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "socks5h://jp-checkout", "provider": "http://jp-provider:11001", "approve": "http://unused"}):
                with patch.object(gen_pp_link, "_new_session", side_effect=lambda proxy="": FakeSession(proxy)):
                    with patch.object(gen_pp_link, "_checkout_post", side_effect=fake_checkout_post):
                        result = gen_pp_link.generate_pp_link("at")

        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://pay.openai.com/c/pay/cs_live_SHORT#fragment")
        self.assertEqual(result["short_url"], "https://pay.openai.com/c/pay/cs_live_SHORT")
        self.assertEqual(result["link_type"], "chatgpt_checkout_hosted_long_url")
        self.assertEqual(result["checkout_country"], "JP")
        self.assertEqual(result["billing_country"], "JP")
        self.assertEqual(result["currency"], "JPY")
        self.assertEqual(result["ba_token"], "")
        self.assertEqual(posted[0][2]["billing_details"], {"country": "JP", "currency": "JPY"})
        self.assertEqual(posted[0][2]["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")
        self.assertEqual(posted[0][2]["checkout_ui_mode"], "custom")
        self.assertEqual(len(posted), 2)
        self.assertNotIn("confirm", posted[1][1])


    def test_hosted_generation_keeps_target_separate_from_checkout_country(self):
        posted = []

        class FakeResponse:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body
                self.text = "{}"
                self.headers = {}

            def json(self):
                return self._body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"http {self.status_code}")

        class FakeSession:
            def __init__(self, proxy):
                self.headers = {}

            def post(self, url, json=None, data=None, timeout=None):
                posted.append((url, json, data))
                if "/payment_pages/cs_live_SPLIT/init" in url:
                    return FakeResponse(200, {"stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_SPLIT#fragment", "payment_method_types": ["card"], "currency": "jpy", "total_summary": {"due": 2727, "currency": "jpy"}})
                raise AssertionError(url)

        def fake_checkout_post(url, json_body, access_token, cookie_header="", proxy="", timeout=30):
            posted.append((url, json_body, None))
            if url.endswith("/backend-api/payments/checkout"):
                return FakeResponse(200, {"checkout_session_id": "cs_live_SPLIT", "processor_entity": "openai_llc", "publishable_key": "pk_test"})
            raise AssertionError(url)

        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"link_generation_type": "hosted_long_url", "target_country": "US", "billing_regions": ["JP"], "require_zero_due": False}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": ""}):
                with patch.object(gen_pp_link, "_new_session", side_effect=lambda proxy="": FakeSession(proxy)):
                    with patch.object(gen_pp_link, "_checkout_post", side_effect=fake_checkout_post):
                        result = gen_pp_link.generate_pp_link("at", target_country="US", checkout_country="JP", require_zero=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["target_country"], "US")
        self.assertEqual(result["checkout_country"], "JP")
        self.assertEqual(result["billing_country"], "JP")
        self.assertEqual(result["currency"], "JPY")
        self.assertEqual(posted[0][1]["billing_details"], {"country": "JP", "currency": "JPY"})

    def test_generate_upi_qr_splits_checkout_and_payment_countries(self):
        import tempfile
        from pathlib import Path

        calls = []
        posted = []

        class FakeResponse:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body
                self.text = "{}"
                self.headers = {}
                self.url = ""

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"http {self.status_code}")

            def json(self):
                return self._body

        class FakeSession:
            def __init__(self, proxy):
                self.proxy = proxy
                self.headers = {}

            def post(self, url, json=None, data=None, timeout=None):
                posted.append((self.proxy, url, json, data))
                if url.endswith("/backend-api/payments/checkout"):
                    return FakeResponse(200, {
                        "checkout_session_id": "cs_live_UPI",
                        "processor_entity": "openai_ie",
                        "publishable_key": "pk_test_upi",
                    })
                if "/payment_pages/cs_live_UPI/init" in url:
                    return FakeResponse(200, {
                        "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_UPI#fidkdWxOYHwnPyd1blpxYHZxWjA0",
                        "payment_method_types": ["card", "upi"],
                        "currency": "inr",
                        "total_summary": {"due": 0, "currency": "inr"},
                    })
                if url.endswith("/payment_pages/cs_live_UPI"):
                    return FakeResponse(200, {
                        "payment_method_types": ["card", "upi"],
                        "currency": "inr",
                        "total_summary": {"due": 0, "currency": "inr"},
                    })
                if "/payment_pages/cs_live_UPI/confirm" in url:
                    return FakeResponse(200, {
                        "status": "succeeded",
                        "payment_method_types": ["card", "upi"],
                        "currency": "inr",
                        "total_summary": {"due": 0, "currency": "inr"},
                    })
                raise AssertionError(url)

        def fake_new_session(proxy=""):
            calls.append(proxy)
            return FakeSession(proxy)

        with tempfile.TemporaryDirectory() as tmp:
            qr_path = Path(tmp) / "upi.png"
            with patch.object(upi_link, "_new_session", side_effect=fake_new_session):
                with patch.object(upi_link, "_write_qr_png", side_effect=lambda data, path="": (Path(path).write_bytes(b"qr"), str(path))[1]):
                    result = gen_pp_link.generate_upi_qr_link(
                        "at",
                        checkout_proxy="socks5h://jp-checkout",
                        provider_proxy="http://in-provider:11001",
                        checkout_country="JP",
                        payment_country="IN",
                        require_zero=True,
                        qr_path=str(qr_path),
                    )

            self.assertTrue(result["ok"])
            self.assertEqual(result["payment_method"], "upi")
            self.assertEqual(result["currency"], "INR")
            self.assertEqual(result["amount"], 0)
            self.assertEqual(result["target_country"], "JP")
            self.assertEqual(result["checkout_country"], "JP")
            self.assertEqual(result["billing_country"], "JP")
            self.assertEqual(result["payment_country"], "IN")
            self.assertEqual(result["checkout_proxy"], "socks5h://jp-checkout")
            self.assertEqual(result["provider_proxy"], "http://in-provider:11001")
            self.assertEqual(result["qr_path"], str(qr_path))
            self.assertTrue(qr_path.exists())
            checkout_body = posted[0][2]
            self.assertEqual(checkout_body["billing_details"], {"country": "JP", "currency": "JPY"})
            self.assertEqual(checkout_body["checkout_ui_mode"], "hosted")
            self.assertEqual(calls[:2], ["socks5h://jp-checkout", "http://in-provider:11001"])

    def test_generate_upi_qr_requires_upi_payment_method(self):
        class FakeResponse:
            status_code = 200
            text = "{}"
            headers = {}
            url = ""

            def __init__(self, body):
                self._body = body

            def raise_for_status(self):
                pass

            def json(self):
                return self._body

        class FakeSession:
            def __init__(self, proxy):
                self.headers = {}

            def post(self, url, json=None, data=None, timeout=None):
                if url.endswith("/backend-api/payments/checkout"):
                    return FakeResponse({"checkout_session_id": "cs_live_NOUPI", "publishable_key": "pk_test"})
                return FakeResponse({"stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_NOUPI", "payment_method_types": ["card"], "currency": "inr", "total_summary": {"due": 0, "currency": "inr"}})

        cfg = {"upi": {"checkout_country": "JP", "payment_country": "IN", "require_zero_due": True}}
        with patch.object(upi_link, "_load_json", return_value=cfg), patch.object(upi_link, "_new_session", side_effect=lambda proxy="": FakeSession(proxy)):
            result = gen_pp_link.generate_upi_qr_link("at", checkout_proxy="jp", provider_proxy="in")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "upi_not_available")
        self.assertEqual(result["checkout_country"], "JP")
        self.assertEqual(result["payment_country"], "IN")

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


    def test_checkout_country_resolved_from_billing_regions_and_passed_to_extractor(self):
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
                    "currency": "USD",
                    "target_country": seen["target_country"],
                    "checkout_country": seen.get("checkout_country", ""),
                    "checkout_proxy": seen.get("checkout_proxy", ""),
                    "provider_proxy": seen.get("provider_proxy", ""),
                    "approve_proxy": seen.get("approve_proxy", ""),
                }

        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"target_country": "US", "billing_regions": ["JP"], "require_zero_due": True}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": ""}):
                with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                    result = gen_pp_link.generate_pp_link("at", require_zero=True, require_ba_token=True)

        self.assertTrue(result["ok"])
        self.assertEqual(seen["target_country"], "US")
        self.assertEqual(seen["checkout_country"], "JP")
        self.assertEqual(result["target_country"], "US")
        self.assertEqual(result["checkout_country"], "JP")

    def test_checkout_country_defaults_to_target_when_not_configured(self):
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
                    "checkout_country": seen.get("checkout_country", ""),
                }

        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"target_country": "DE", "require_zero_due": True}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": ""}):
                with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                    result = gen_pp_link.generate_pp_link("at", require_zero=True, require_ba_token=True)

        self.assertTrue(result["ok"])
        self.assertEqual(seen["target_country"], "DE")
        self.assertEqual(seen["checkout_country"], "DE")

    def test_checkout_country_explicit_override_takes_priority(self):
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
                    "currency": "USD",
                    "target_country": seen["target_country"],
                    "checkout_country": seen.get("checkout_country", ""),
                }

        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"target_country": "US", "billing_regions": ["JP"], "require_zero_due": True}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": ""}):
                with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                    result = gen_pp_link.generate_pp_link("at", target_country="US", checkout_country="TR", require_zero=True, require_ba_token=True)

        self.assertTrue(result["ok"])
        self.assertEqual(seen["target_country"], "US")
        self.assertEqual(seen["checkout_country"], "TR")

    def test_extractor_uses_checkout_country_for_billing_and_processor(self):
        posted = []

        class FakeResponse:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body
                self.text = "{}"
                self.headers = {}

            def json(self):
                return self._body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"http {self.status_code}")

        class FakeSession:
            def __init__(self, proxy):
                self.headers = {}

            def post(self, url, json=None, data=None, timeout=None):
                posted.append((url, json, data))
                if url.endswith("/backend-api/payments/checkout"):
                    return FakeResponse(200, {
                        "checkout_session_id": "cs_live_SPLIT",
                        "processor_entity": "openai_ie",
                        "publishable_key": "pk_test",
                    })
                if "/payment_pages/cs_live_SPLIT/init" in url:
                    return FakeResponse(200, {
                        "payment_method_types": ["card", "paypal"],
                        "currency": "jpy",
                        "total_summary": {"due": 0, "currency": "jpy"},
                    })
                if "/payment_methods" in url:
                    return FakeResponse(200, {"id": "pm_test_123"})
                if "/payment_pages/cs_live_SPLIT/confirm" in url:
                    return FakeResponse(200, {
                        "next_action": {
                            "type": "redirect_to_url",
                            "redirect_to_url": {"url": "https://www.paypal.com/agreements/approve?ba_token=BA-TEST123"},
                        },
                    })
                if url.endswith("/backend-api/sentinel/ping"):
                    return FakeResponse(200, {})
                if url.endswith("/backend-api/payments/checkout/approve"):
                    return FakeResponse(200, {"result": "approved"})
                raise AssertionError(url)

            def get(self, url, params=None, timeout=None, allow_redirects=True):
                raise AssertionError(f"unexpected GET: {url}")

        def fake_checkout_post(url, json_body, access_token, cookie_header="", proxy="", timeout=30):
            posted.append((url, json_body, None))
            if url.endswith("/backend-api/payments/checkout"):
                return FakeResponse(200, {
                    "checkout_session_id": "cs_live_SPLIT",
                    "processor_entity": "openai_ie",
                    "publishable_key": "pk_test",
                })
            raise AssertionError(url)

        with patch.object(paypal_extract, "_new_session", side_effect=lambda proxy="": FakeSession(proxy)):
            with patch.object(paypal_extract, "_checkout_post", side_effect=fake_checkout_post):
                with patch.object(paypal_extract.PPLinkExtractor, "_poll_payment_page", return_value="https://www.paypal.com/agreements/approve?ba_token=BA-TEST123"):
                    extractor = gen_pp_link.PPLinkExtractor(
                        access_token="at",
                        target_country="US",
                        checkout_country="JP",
                        require_zero=True,
                    )
                    result = extractor.extract()

        self.assertTrue(result["ok"])
        self.assertEqual(result["target_country"], "US")
        self.assertEqual(result["checkout_country"], "JP")
        checkout_body = posted[0][1]
        self.assertEqual(checkout_body["billing_details"], {"country": "JP", "currency": "JPY"})
        self.assertEqual(result["link_type"], "paypal_ba_approve")
        self.assertTrue(result["ba_token"])


if __name__ == "__main__":
    unittest.main()
