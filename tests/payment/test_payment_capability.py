import unittest
from types import SimpleNamespace
from unittest.mock import patch

from curl_cffi.requests.impersonate import BrowserType

from sms_tool import gen_pp_link, paypal_extract, payment_wire
from sms_tool.auth_headers import AUTH_FINGERPRINT_PROFILES
from sms_tool.checkout_contract import CheckoutSessionContract
from sms_tool.payment_capability import CapabilityProbeError, _response_json, payment_method_capability_probe


class FakeCapabilityTransport:
    def __init__(self, init_payload):
        self.init_payload = init_payload
        self.checkout_calls = []
        self.init_calls = []

    def create_checkout(self, contract, **kwargs):
        self.checkout_calls.append((contract, kwargs))
        return CheckoutSessionContract("cs_fixture", "openai_ie", "pk_live_fixture")

    def stripe_init(self, contract, checkout, **kwargs):
        self.init_calls.append((contract, checkout, kwargs))
        return self.init_payload


class PaymentCapabilityProbeTests(unittest.TestCase):
    def test_checkout_post_uses_a_supported_matching_browser_profile(self):
        with patch.object(paypal_extract.curl_requests, "post", return_value=SimpleNamespace(status_code=200)) as post:
            paypal_extract._checkout_post(
                "https://chatgpt.com/backend-api/payments/checkout",
                {"plan_name": "plus"},
                "access-token",
                "oai-did=device-1",
                "http://exit.test:80",
                20,
            )
        kwargs = post.call_args.kwargs
        self.assertIn(kwargs["impersonate"], {browser.value for browser in BrowserType})
        profile = AUTH_FINGERPRINT_PROFILES[kwargs["impersonate"]]
        self.assertEqual(kwargs["headers"]["User-Agent"], profile["user_agent"])

    def test_checkout_without_browser_impersonation_never_falls_back_to_plain_requests(self):
        # Patch the **defining** module. ``_checkout_post`` reads ``curl_requests``
        # from its own globals, so ``patch.object(paypal_extract, "curl_requests", None)``
        # (which only rebinds the name on the paypal_extract facade) no longer
        # reaches the body -- the facade re-exports the same function object but
        # not the lookup. The sibling test above keeps patching
        # ``paypal_extract.curl_requests`` and works either way, because it patches
        # ``.post`` on the curl_cffi *module object*, which both modules share.
        with (
            patch.object(payment_wire, "curl_requests", None),
            patch.object(payment_wire.requests, "post", side_effect=AssertionError("plain TLS")),
        ):
            with self.assertRaisesRegex(RuntimeError, "curl_cffi is required"):
                payment_wire._checkout_post("https://chatgpt.com/checkout", {}, "token")

    def test_one_custom_probe_reuses_device_cookie_proxy_and_browser_identity(self):
        checkout_response = SimpleNamespace(
            status_code=200,
            json=lambda: {"checkout_session_id": "oaics_fixture"},
        )
        custom_response = SimpleNamespace(
            status_code=200,
            json=lambda: {"currency": "inr", "payment_method_types": ["upi"]},
        )
        context = {
            "device_id": "persisted-device",
            "oai_did": "persisted-device",
            "cookie_header": "oai-did=persisted-device; session=fixture",
        }
        with (
            patch.object(gen_pp_link, "_checkout_post", return_value=checkout_response) as post,
            patch.object(gen_pp_link, "_checkout_get", return_value=custom_response) as get,
            patch.object(gen_pp_link, "_new_session", side_effect=AssertionError("unexpected network session")),
        ):
            result = payment_method_capability_probe(
                "access-token",
                "direct_card",
                billing_country="IN",
                currency="INR",
                require_zero=False,
                auth_context=context,
                checkout_proxy="http://exit.test:80",
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(post.call_args.args[3], context["cookie_header"])
        self.assertEqual(post.call_args.args[4], "http://exit.test:80")
        self.assertEqual(post.call_args.kwargs["extra_headers"]["OAI-Device-Id"], "persisted-device")
        self.assertEqual(get.call_args.args[2], context["cookie_header"])
        self.assertEqual(get.call_args.args[3], "http://exit.test:80")
        self.assertEqual(get.call_args.kwargs["extra_headers"]["OAI-Device-Id"], "persisted-device")

    def test_custom_session_get_uses_the_same_supported_browser_profile(self):
        with patch.object(paypal_extract.curl_requests, "get", return_value=SimpleNamespace(status_code=200)) as get:
            paypal_extract._checkout_get(
                "https://chatgpt.com/backend-api/payments/checkout/sessions/oaics_fixture",
                "access-token",
                "oai-did=device-1",
                "http://exit.test:80",
                20,
                extra_headers={"OAI-Device-Id": "device-1"},
            )
        kwargs = get.call_args.kwargs
        profile = AUTH_FINGERPRINT_PROFILES[kwargs["impersonate"]]
        self.assertEqual(kwargs["headers"]["User-Agent"], profile["user_agent"])
        self.assertEqual(kwargs["headers"]["OAI-Device-Id"], "device-1")

    def test_conflicting_cookie_device_does_not_start_checkout(self):
        with patch.object(gen_pp_link, "_checkout_post", side_effect=AssertionError("checkout must not start")):
            result = payment_method_capability_probe(
                "access-token",
                "direct_card",
                billing_country="IN",
                currency="INR",
                auth_context={
                    "device_id": "saved-device",
                    "oai_did": "saved-device",
                    "cookie_header": "oai-did=another-device; session=fixture",
                },
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "checkout_identity_mismatch")
        self.assertNotIn("saved-device", str(result))
        self.assertNotIn("another-device", str(result))

    def test_checkout_risk_block_preserves_only_safe_classification(self):
        response = SimpleNamespace(
            status_code=400,
            json=lambda: {
                "detail": "Our systems have detected unusual activity. Please try again later.",
                "authorization": "Bearer secret-token",
            },
        )
        with self.assertRaises(CapabilityProbeError) as raised:
            _response_json(
                response,
                stage="checkout_create",
                unauthorized_code="checkout_unauthorized",
                failure_code="checkout_failed",
            )
        self.assertEqual(raised.exception.error_code, "checkout_risk_blocked")
        self.assertEqual(raised.exception.http_status, 400)
        self.assertNotIn("secret-token", str(raised.exception))

    def test_checkout_rate_limit_is_distinct_from_risk_block(self):
        response = SimpleNamespace(
            status_code=429,
            json=lambda: {"detail": {"code": "checkout_creation_rate_limited", "message": "Bearer secret-token"}},
        )
        with self.assertRaises(CapabilityProbeError) as raised:
            _response_json(
                response,
                stage="checkout_create",
                unauthorized_code="checkout_unauthorized",
                failure_code="checkout_failed",
            )
        self.assertEqual(raised.exception.error_code, "checkout_creation_rate_limited")
        self.assertEqual(raised.exception.http_status, 429)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("secret-token", str(raised.exception))

    def test_unrecognized_checkout_error_keeps_only_http_status(self):
        response = SimpleNamespace(status_code=400, json=lambda: {"detail": {"code": "unknown_secret_value"}})
        with self.assertRaises(CapabilityProbeError) as raised:
            _response_json(
                response,
                stage="checkout_create",
                unauthorized_code="checkout_unauthorized",
                failure_code="checkout_failed",
            )
        self.assertEqual(raised.exception.error_code, "checkout_failed")
        self.assertEqual(raised.exception.http_status, 400)
        self.assertNotIn("unknown_secret_value", str(raised.exception))

    def test_probe_stops_after_stripe_init_and_marks_zero_due_method_eligible(self):
        transport = FakeCapabilityTransport(
            {
                "total_summary": {"due": 0},
                "currency": "idr",
                "payment_method_types": ["card", "gopay"],
            }
        )

        result = payment_method_capability_probe(
            "access-token",
            "gopay",
            transport=transport,
            checkout_proxy="http://checkout.test:80",
            stripe_init_proxy="http://stripe.test:80",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["offer_state"], "zero_due")
        self.assertEqual(len(transport.checkout_calls), 1)
        self.assertEqual(len(transport.init_calls), 1)
        self.assertEqual(transport.checkout_calls[0][1]["proxy"], "http://checkout.test:80")
        self.assertEqual(transport.init_calls[0][2]["proxy"], "http://stripe.test:80")

    def test_nonzero_offer_is_conclusive_ineligible(self):
        transport = FakeCapabilityTransport(
            {
                "invoice": {"amount_due": 290000},
                "currency": "idr",
                "payment_method_types": ["gopay"],
            }
        )
        result = payment_method_capability_probe("access-token", "gopay", transport=transport)
        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "nonzero_offer")
        self.assertFalse(result["eligible"])
        self.assertFalse(result["retryable"])

    def test_missing_method_is_conclusive_ineligible(self):
        transport = FakeCapabilityTransport(
            {
                "total_summary": {"due": 0},
                "payment_method_types": ["card"],
            }
        )
        result = payment_method_capability_probe("access-token", "gcash", transport=transport)
        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "payment_method_unavailable")

    def test_transport_failure_is_unknown_and_retryable(self):
        class FailedTransport(FakeCapabilityTransport):
            def create_checkout(self, contract, **kwargs):
                raise CapabilityProbeError(
                    "checkout timed out",
                    error_code="checkout_transport_failed",
                    error_stage="checkout_create",
                    retryable=True,
                    status="unknown",
                )

        result = payment_method_capability_probe("access-token", "gopay", transport=FailedTransport({}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["classification"], "unknown")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["error_stage"], "checkout_create")

    def test_custom_checkout_reads_oaics_methods_without_stripe_or_payment_submission(self):
        class CustomTransport(FakeCapabilityTransport):
            def create_checkout(self, contract, **kwargs):
                return CheckoutSessionContract("oaics_fixture", "openai_ie", "")

            def custom_checkout_session(self, contract, checkout, **kwargs):
                return {
                    "currency": "inr",
                    "custom_payment_methods": [
                        {"type": "upi"},
                        {"id": "cpmt_opaque_without_type"},
                    ],
                }

            def stripe_init(self, contract, checkout, **kwargs):
                self.fail("oaics session must not call Stripe init")

        result = payment_method_capability_probe(
            "access-token",
            "direct_card",
            billing_country="IN",
            currency="INR",
            require_zero=False,
            transport=CustomTransport({}),
        )
        self.assertEqual(result["checkout_kind"], "oaics")
        self.assertEqual(result["payment_method_types"], ["upi"])
        self.assertEqual(result["evidence_sources"], ["custom_checkout"])

    def test_stripe_elements_explicit_specs_extend_init_methods(self):
        class ElementsTransport(FakeCapabilityTransport):
            def stripe_elements(self, contract, checkout, init_payload, **kwargs):
                return {"payment_method_specs": [{"type": "upi"}, {"type": "momo"}]}

        result = payment_method_capability_probe(
            "access-token",
            "direct_card",
            billing_country="IN",
            currency="INR",
            require_zero=False,
            transport=ElementsTransport({"currency": "inr", "payment_method_types": ["card"]}),
        )
        self.assertEqual(result["checkout_kind"], "stripe")
        self.assertEqual(result["payment_method_types"], ["card", "upi", "momo"])
        self.assertEqual(result["evidence_sources"], ["stripe_init", "stripe_elements"])

    def test_failed_elements_read_keeps_explicit_init_methods(self):
        class FailedElements(FakeCapabilityTransport):
            def stripe_elements(self, contract, checkout, init_payload, **kwargs):
                raise RuntimeError("Bearer private-token")

        result = payment_method_capability_probe(
            "access-token",
            "direct_card",
            billing_country="IN",
            currency="INR",
            require_zero=False,
            transport=FailedElements({"currency": "inr", "payment_method_types": ["card"]}),
        )
        self.assertEqual(result["payment_method_types"], ["card"])
        self.assertNotIn("private-token", str(result))

    def test_checkout_with_opaque_custom_id_does_not_invent_card_or_provider(self):
        class CustomTransport(FakeCapabilityTransport):
            def create_checkout(self, contract, **kwargs):
                return CheckoutSessionContract("oaics_fixture", "openai_ie", "")

            def custom_checkout_session(self, contract, checkout, **kwargs):
                return {"customPaymentMethods": [{"id": "cpmt_opaque"}, "cpmt_opaque"]}

        result = payment_method_capability_probe(
            "access-token",
            "direct_card",
            billing_country="IN",
            currency="INR",
            require_zero=False,
            transport=CustomTransport({}),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["payment_method_types"], [])

    def test_mismatched_currency_never_certifies_unrelated_methods(self):
        result = payment_method_capability_probe(
            "access-token",
            "direct_card",
            billing_country="IN",
            currency="INR",
            require_zero=False,
            transport=FakeCapabilityTransport({"currency": "usd", "payment_method_types": ["upi"]}),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "checkout_currency_mismatch")


if __name__ == "__main__":
    unittest.main()
