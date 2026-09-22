"""Tests for services/protocol-payment/common/endpoints.py.

The point of the module is a single authority for upstream hosts. These tests
pin the exact shapes callers depend on so a careless edit (a dropped path
segment, a scheme flip) fails loudly instead of pointing a payment at the wrong
origin.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services" / "protocol-payment" / "common" / "endpoints.py"
)
SPEC = importlib.util.spec_from_file_location("protocol_payment_endpoints", MODULE_PATH)
EP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = EP
SPEC.loader.exec_module(EP)


class HostTests(unittest.TestCase):
    def test_hosts_are_https_except_none(self):
        self.assertTrue(EP.CHATGPT_BASE.startswith("https://"))
        self.assertTrue(EP.STRIPE_API_BASE.startswith("https://"))
        self.assertTrue(EP.STRIPE_CHECKOUT_BASE.startswith("https://"))
        self.assertTrue(EP.OPENAI_PAY_BASE.startswith("https://"))

    def test_host_constant_matches_base(self):
        self.assertEqual(EP.CHATGPT_HOST, "chatgpt.com")
        self.assertIn(EP.CHATGPT_HOST, EP.CHATGPT_BASE)


class PathTests(unittest.TestCase):
    def test_checkout_api_family_shares_prefix(self):
        for url in (
            EP.CHATGPT_CHECKOUT_SNAPSHOT,
            EP.CHATGPT_CHECKOUT_UPDATE,
            EP.CHATGPT_CHECKOUT_APPROVE,
            EP.CHATGPT_CHECKOUT_TAXES,
        ):
            self.assertTrue(url.startswith(EP.CHATGPT_CHECKOUT_API + "/"))

    def test_checkout_page(self):
        self.assertEqual(
            EP.chatgpt_checkout_page("openai_ie", "cs_1"),
            "https://chatgpt.com/checkout/openai_ie/cs_1",
        )

    def test_checkout_verify(self):
        url = EP.chatgpt_checkout_verify("cs_1", "openai_ie")
        self.assertIn("stripe_session_id=cs_1", url)
        self.assertIn("processor_entity=openai_ie", url)
        self.assertIn("plan_type=plus", url)

    def test_stripe_payment_page(self):
        self.assertEqual(
            EP.stripe_payment_page("cs_1", "confirm"),
            "https://api.stripe.com/v1/payment_pages/cs_1/confirm",
        )

    def test_stripe_payment_methods(self):
        self.assertEqual(EP.STRIPE_PAYMENT_METHODS, "https://api.stripe.com/v1/payment_methods")


if __name__ == "__main__":
    unittest.main()
