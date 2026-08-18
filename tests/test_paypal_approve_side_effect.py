"""Side-effect discipline for the PayPal approve stage and the checkout contract.

``approve``/``poll`` are declared side-effect stages: once the approve POST is
issued the extractor must never re-drive it, because a duplicate approval cannot
be undone. These tests pin that behaviour plus the two contract fixes that ride
with it (configured promo campaign, egress-matched Stripe fingerprint).
"""

import unittest
from unittest.mock import patch

from sms_tool import gen_pp_link
from sms_tool import paypal_extract
from sms_tool.payment_contracts import payment_retry_allowed


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


def _extractor(**kwargs):
    defaults = {
        "approve_proxy": "http://approve",
        "target_country": "US",
        "checkout_country": "US",
        "max_stage_retries": 3,
    }
    defaults.update(kwargs)
    return paypal_extract.PPLinkExtractor("attoken", **defaults)


class ApproveSideEffectTests(unittest.TestCase):
    def test_approve_failure_is_not_retried(self):
        extractor = _extractor()
        calls = []

        def failing_approve(cs_id, processor_entity):
            calls.append(cs_id)
            raise RuntimeError("connection reset")

        with patch.object(extractor, "_prepare_stage_proxy", return_value="http://approve"):
            with patch.object(extractor, "_chatgpt_approve", side_effect=failing_approve):
                with self.assertRaises(paypal_extract.PaymentOutcomeUnknownError) as ctx:
                    extractor._approve_and_poll("cs_live_X", "openai_llc")

        self.assertEqual(len(calls), 1, "approve must be issued at most once")
        self.assertEqual(ctx.exception.error_stage, "approve")
        self.assertEqual(ctx.exception.error_code, "approve_outcome_unknown")
        self.assertEqual(ctx.exception.status, "unknown")
        self.assertFalse(ctx.exception.retryable)

    def test_poll_failure_after_approve_never_reapproves(self):
        extractor = _extractor()
        calls = []

        with patch.object(extractor, "_prepare_stage_proxy", return_value="http://approve"):
            with patch.object(extractor, "_chatgpt_approve", side_effect=lambda *a: calls.append(a)):
                with patch.object(extractor, "_poll_payment_page", side_effect=RuntimeError("requires_approval")):
                    with self.assertRaises(paypal_extract.PaymentOutcomeUnknownError) as ctx:
                        extractor._approve_and_poll("cs_live_X", "openai_llc")

        self.assertEqual(len(calls), 1, "a poll failure must not re-issue approve")
        self.assertEqual(ctx.exception.error_stage, "poll")
        self.assertEqual(ctx.exception.error_code, "approve_poll_outcome_unknown")

    def test_proxy_preparation_failure_is_retried_before_any_side_effect(self):
        extractor = _extractor()
        attempts = []
        approvals = []

        def failing_proxy(stage, proxy, attempt=1):
            attempts.append(attempt)
            raise RuntimeError("proxy_preflight_failed:approve")

        with patch.object(extractor, "_prepare_stage_proxy", side_effect=failing_proxy):
            with patch.object(extractor, "_chatgpt_approve", side_effect=lambda *a: approvals.append(a)):
                with self.assertRaises(RuntimeError) as ctx:
                    extractor._approve_and_poll("cs_live_X", "openai_llc")

        self.assertEqual(attempts, [1, 2, 3], "pre-request proxy setup stays retryable")
        self.assertEqual(approvals, [])
        self.assertNotIsInstance(ctx.exception, paypal_extract.PaymentOutcomeUnknownError)

    def test_successful_approve_returns_redirect(self):
        extractor = _extractor()
        with patch.object(extractor, "_prepare_stage_proxy", return_value="http://approve"):
            with patch.object(extractor, "_chatgpt_approve", return_value=None):
                with patch.object(extractor, "_poll_payment_page", return_value="https://pm-redirects.stripe.com/authorize/x"):
                    url = extractor._approve_and_poll("cs_live_X", "openai_llc")
        self.assertEqual(url, "https://pm-redirects.stripe.com/authorize/x")

    def test_confirm_stage_failure_reports_unknown_outcome(self):
        extractor = _extractor(provider_proxy="http://us")

        def failing_confirm(cs_id, pm_id, init):
            # Real request stages claim the marker before touching the network.
            extractor._active_stage = "confirm"
            raise RuntimeError("gateway timeout")

        with patch.object(extractor, "_prepare_stage_proxy", return_value=""):
            with patch.object(paypal_extract, "_new_session", lambda proxy="": object()):
                with patch.object(extractor, "_stripe_init", return_value={"stripe_hosted_url": ""}):
                    with patch.object(extractor, "_create_payment_method", return_value="pm_x"):
                        with patch.object(extractor, "_stripe_confirm", side_effect=failing_confirm):
                            with self.assertRaises(paypal_extract.PaymentOutcomeUnknownError) as ctx:
                                extractor._run_provider_stages("cs_live_X")
        self.assertEqual(ctx.exception.error_stage, "confirm")

    def test_confirm_proxy_preflight_failure_stays_retryable(self):
        """A confirm-stage proxy preflight sends nothing, so it must not be unknown."""
        extractor = _extractor(provider_proxy="http://us")
        attempts = []

        def failing_proxy(stage, proxy, attempt=1):
            attempts.append((stage, attempt))
            if stage == "confirm":
                raise RuntimeError("proxy_preflight_failed:confirm")
            return proxy

        with patch.object(extractor, "_prepare_stage_proxy", side_effect=failing_proxy):
            with self.assertRaises(RuntimeError) as ctx:
                extractor._run_provider_stages("cs_live_X")

        self.assertNotIsInstance(ctx.exception, paypal_extract.PaymentOutcomeUnknownError)
        self.assertEqual([stage for stage, _ in attempts].count("confirm"), 3)


class ApproveUnknownContractTests(unittest.TestCase):
    """The unknown outcome must survive the whole manager pipeline."""

    def _run_manager(self):
        from sms_tool import payment_link_manager

        def boom(**kwargs):
            raise paypal_extract.PaymentOutcomeUnknownError(
                "approve_outcome_unknown: connection reset",
                stage="approve",
                error_code="approve_outcome_unknown",
            )

        config = {"chatgpt": {}, "protocol_payments": {"enabled_methods": ["paypal"]}}
        with patch.object(payment_link_manager, "_safe_persist_run", lambda result: None):
            with patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=boom):
                return payment_link_manager.generate_payment_link(
                    access_token="at",
                    payment_method="paypal",
                    runtime_config=config,
                )

    def test_generate_pp_link_maps_unknown_outcome(self):
        class FakeExtractor:
            def __init__(self, **kwargs):
                self.proxy_state = self
                self.checkout_proxy = self.provider_proxy = self.approve_proxy = ""

            def record_pair_result(self, *args, **kwargs):
                return None

            def extract(self):
                raise paypal_extract.PaymentOutcomeUnknownError(
                    "approve_outcome_unknown: connection reset",
                    stage="approve",
                    error_code="approve_outcome_unknown",
                )

        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {}}):
            with patch.object(gen_pp_link, "_proxies_from_config", return_value={"checkout": "", "provider": "", "approve": "", "promotion": ""}):
                with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                    result = gen_pp_link.generate_pp_link("at")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "approve_outcome_unknown")
        self.assertTrue(result["requires_reconciliation"])
        self.assertFalse(result["retryable"])
        self.assertFalse(payment_retry_allowed(result))

    def test_manager_terminal_state_is_unknown_and_not_retryable(self):
        result = self._run_manager()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["manager_state"], "unknown")
        self.assertTrue(result["requires_reconciliation"])
        self.assertFalse(result["retryable"])
        self.assertFalse(payment_retry_allowed(result))


class CheckoutContractReuseTests(unittest.TestCase):
    def test_configured_promo_campaign_reaches_stage_one_checkout(self):
        extractor = _extractor(promo_campaign_id="plus-3-months-free")
        seen = {}

        def fake_post(url, body, access_token, cookie_header="", proxy="", timeout=30, extra_headers=None):
            seen["body"] = body
            return _Resp(200, {
                "checkout_session_id": "cs_live_X",
                "processor_entity": "openai_llc",
                "publishable_key": "pk_live_x",
            })

        with patch.object(extractor, "_prepare_stage_proxy", return_value=""):
            with patch.object(paypal_extract, "_checkout_post", side_effect=fake_post):
                extractor._create_checkout()

        self.assertEqual(seen["body"]["promo_campaign"]["promo_campaign_id"], "plus-3-months-free")
        self.assertEqual(seen["body"]["billing_details"], {"country": "US", "currency": "USD"})
        self.assertEqual(seen["body"]["checkout_ui_mode"], "custom")

    def test_checkout_accepts_oaics_session(self):
        extractor = _extractor()

        def fake_post(url, body, access_token, cookie_header="", proxy="", timeout=30, extra_headers=None):
            return _Resp(200, {
                "checkout_session_id": "oaics_live_X",
                "processor_entity": "openai_llc",
                "publishable_key": "pk_live_x",
            })

        with patch.object(extractor, "_prepare_stage_proxy", return_value=""):
            with patch.object(paypal_extract, "_checkout_post", side_effect=fake_post):
                checkout = extractor._create_checkout()

        self.assertEqual(checkout["cs_id"], "oaics_live_X")
        self.assertEqual(checkout["processor_entity"], "openai_llc")

    def test_extract_returns_oaics_checkout_link_without_provider_side_effects(self):
        extractor = _extractor(promotion_proxy="http://promotion")
        checkout = {
            "cs_id": "oaics_live_X",
            "processor_entity": "openai_ie",
            "stripe_publishable_key": "",
            "billing_country": "US",
            "currency": "USD",
        }

        with patch.object(extractor, "_create_checkout", return_value=checkout):
            with patch.object(extractor, "_checkout_update_promotion", return_value=True) as promotion:
                with patch.object(extractor, "_run_provider_stages") as provider:
                    result = extractor.extract()

        promotion.assert_called_once_with("oaics_live_X", "openai_ie")
        provider.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "chatgpt_checkout_link")
        self.assertEqual(result["url"], "https://chatgpt.com/checkout/openai_ie/oaics_live_X")
        self.assertEqual(result["cs_id"], "oaics_live_X")
        self.assertFalse(result["side_effect_started"])

    def test_stripe_init_fingerprint_follows_checkout_country(self):
        extractor = _extractor(target_country="JP", checkout_country="JP")
        seen = {}

        class FakeSession:
            def post(self, url, data=None, timeout=None):
                seen["data"] = data
                return _Resp(200, {
                    "payment_method_types": ["paypal"],
                    "currency": "jpy",
                    "total_summary": {"due": 0, "currency": "jpy"},
                })

        extractor._stripe_session = FakeSession()
        extractor._stripe_init("cs_live_X")

        self.assertEqual(seen["data"]["browser_timezone"], "Asia/Tokyo")
        self.assertEqual(seen["data"]["browser_locale"], "ja-JP")
        self.assertEqual(seen["data"]["key"], extractor.stripe_pk)


if __name__ == "__main__":
    unittest.main()
