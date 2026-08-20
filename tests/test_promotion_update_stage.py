"""Tests for the promotion-update stage in gen_pp_link.

Verifies the "0元 + PayPal" mechanism reverse-engineered from the reference
extractors: after creating the checkout, the extractor calls
POST /backend-api/payments/checkout/update through the promotion-eligible
region egress to attach the 0-due promo to the SAME checkout session, and only
then runs the Stripe init (which gates on amount==0 AND paypal availability).
"""

import unittest
from unittest.mock import patch

from sms_tool import gen_pp_link as g
from sms_tool import paypal_extract


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class PromotionUpdateStageTests(unittest.TestCase):
    def test_promotion_config_is_opt_in(self):
        with_promo = g._proxies_from_config({"paypal": {"stage_proxies": {"provider": "p", "promotion": "http://vn"}}})
        without = g._proxies_from_config({"paypal": {"stage_proxies": {"provider": "p"}}})
        self.assertEqual(with_promo["promotion"], "http://vn")
        self.assertEqual(without["promotion"], "")

    def test_extractor_flags(self):
        e = g.PPLinkExtractor("at", provider_proxy="http://us", promotion_proxy="http://vn", target_country="US")
        self.assertTrue(e.enable_promotion)
        self.assertEqual(e.promotion_proxy, "http://vn")
        self.assertFalse(g.PPLinkExtractor("at", provider_proxy="http://us").enable_promotion)

    def test_update_promotion_posts_to_update_endpoint_via_promotion_proxy(self):
        e = g.PPLinkExtractor(
            "attoken", provider_proxy="http://us", promotion_proxy="http://vn",
            target_country="US", checkout_country="US",
        )
        seen = {}

        def fake_post(url, body, access_token, cookie_header="", proxy="", timeout=30, extra_headers=None):
            seen["url"] = url
            seen["body"] = body
            seen["proxy"] = proxy
            seen["headers"] = extra_headers or {}
            return _Resp(200, {"success": True})

        with patch.object(paypal_extract, "_checkout_post", side_effect=fake_post):
            ok = e._checkout_update_promotion("cs_live_X", "openai_llc")

        self.assertTrue(ok)
        self.assertEqual(seen["url"], "https://chatgpt.com/backend-api/payments/checkout/update")
        self.assertEqual(seen["proxy"], "http://vn")  # routed through promotion egress
        self.assertEqual(seen["body"]["checkout_session_id"], "cs_live_X")
        self.assertEqual(seen["body"]["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")
        self.assertEqual(seen["headers"].get("x-openai-target-path"), "/backend-api/payments/checkout/update")

    def test_update_promotion_non_fatal_on_error(self):
        e = g.PPLinkExtractor("at", provider_proxy="http://us", promotion_proxy="http://vn", target_country="US")
        with patch.object(paypal_extract, "_checkout_post", return_value=_Resp(409, text="checkout_not_active")):
            self.assertFalse(e._checkout_update_promotion("cs_live_X", "openai_llc"))

    def test_extract_runs_stripe_init_before_post_approval_promotion(self):
        """The standard flow must not apply promotion before Stripe init."""
        e = g.PPLinkExtractor(
            "attoken", checkout_proxy="http://us", provider_proxy="http://us",
            promotion_proxy="http://vn", target_country="US", checkout_country="US",
            require_zero=True,
        )
        calls = []

        def fake_checkout_post(url, body, access_token, cookie_header="", proxy="", timeout=30, extra_headers=None):
            calls.append(("POST", url, proxy))
            if url.endswith("/payments/checkout"):
                return _Resp(200, {"checkout_session_id": "cs_live_X", "processor_entity": "openai_llc", "publishable_key": "pk_live_x"})
            if url.endswith("/checkout/update"):
                return _Resp(200, {"success": True})
            raise AssertionError(url)

        # Stop at init; promotion must not have been called yet.
        def fake_stripe_init(cs_id):
            calls.append(("STRIPE_INIT", cs_id, None))
            raise RuntimeError("stop-after-init-order-check")

        with patch.object(paypal_extract, "_checkout_post", side_effect=fake_checkout_post):
            with patch.object(paypal_extract, "_new_session", lambda proxy="": object()):
                with patch.object(e, "_stripe_init", side_effect=fake_stripe_init):
                    with self.assertRaises(RuntimeError):
                        e.extract()

        seq = [c[0] if c[0] != "POST" else c[1].rsplit("/", 1)[-1] for c in calls]
        self.assertEqual(seq[0], "checkout")            # create checkout first
        self.assertEqual(seq[1], "STRIPE_INIT")         # initialize before approval/promotion
        self.assertNotIn("update", seq)

    def test_run_batch_promotion_matrix_searches_and_stops_on_zero_ba(self):
        """promotion_countries triggers paypal_region x promotion_region search."""
        attempts = []

        class FakeExtractor:
            def __init__(self, **kwargs):
                attempts.append((kwargs["target_country"], kwargs["promotion_proxy"]))

            def extract(self):
                if len(attempts) == 1:
                    return {"ok": True, "amount": 2000, "link_type": "paypal_ba_approve", "url": "x"}
                return {"ok": True, "amount": 0, "link_type": "paypal_ba_approve",
                        "url": "https://www.paypal.com/agreements/approve?ba_token=BA-OK123456789",
                        "cs_id": "cs_x"}

        tpl = "http://user-region-JP-sid-abc-t-5:pw@gate:443"
        with patch.object(g, "PPLinkExtractor", FakeExtractor):
            result = g.run_batch("at", tpl, target_countries=["US", "DE"],
                                 promotion_countries=["JP", "TH"], emit=lambda *a, **k: None)

        self.assertTrue(result["ok"])
        self.assertIn("matrix", result)
        self.assertEqual(result["matrix"][-1]["status"], "success")
        self.assertLessEqual(len(attempts), 2)

    def test_generate_pp_link_threads_promotion_from_config(self):
        captured = {}

        class FakeExtractor:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def extract(self):
                return {"ok": True, "link_type": "paypal_ba_approve",
                        "url": "https://www.paypal.com/agreements/approve?ba_token=BA-TEST123456789",
                        "ba_token": "BA-TEST123456789", "cs_id": "cs_test",
                        "promotion_proxy": captured.get("promotion_proxy", "")}

        orig_ext, orig_load = g.PPLinkExtractor, g._load_json
        g.PPLinkExtractor = FakeExtractor
        g._load_json = lambda _p: {"paypal": {"stage_proxies": {"provider": "http://us", "approve": "http://us",
                                                                 "checkout": "http://us", "promotion": "http://vn"},
                                              "link_generation_type": "paypal_direct"}}
        try:
            result = g.generate_pp_link("at")
        finally:
            g.PPLinkExtractor, g._load_json = orig_ext, orig_load

        self.assertEqual(captured.get("promotion_proxy"), "http://vn")
        self.assertEqual(result.get("promotion_proxy"), "http://vn")


if __name__ == "__main__":
    unittest.main()
