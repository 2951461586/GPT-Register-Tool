import unittest

from sms_tool.checkout_contract import CheckoutSessionContract
from sms_tool.payment_capability import CapabilityProbeError, payment_method_capability_probe


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
    def test_probe_stops_after_stripe_init_and_marks_zero_due_method_eligible(self):
        transport = FakeCapabilityTransport({
            "total_summary": {"due": 0},
            "currency": "idr",
            "payment_method_types": ["card", "gopay"],
        })

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
        transport = FakeCapabilityTransport({
            "invoice": {"amount_due": 290000},
            "currency": "idr",
            "payment_method_types": ["gopay"],
        })
        result = payment_method_capability_probe("access-token", "gopay", transport=transport)
        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "nonzero_offer")
        self.assertFalse(result["eligible"])
        self.assertFalse(result["retryable"])

    def test_missing_method_is_conclusive_ineligible(self):
        transport = FakeCapabilityTransport({
            "total_summary": {"due": 0},
            "payment_method_types": ["card"],
        })
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


if __name__ == "__main__":
    unittest.main()
