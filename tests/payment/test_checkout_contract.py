import unittest

from sms_tool.checkout_contract import (
    CheckoutContractError,
    CheckoutRequestContract,
    CheckoutSessionContract,
    StripeCapabilityEvidence,
)


class CheckoutContractTests(unittest.TestCase):
    def test_gopay_checkout_and_stripe_init_contract(self):
        contract = CheckoutRequestContract.for_payment_method("gopay")

        self.assertEqual(contract.billing_country, "ID")
        self.assertEqual(contract.currency, "IDR")
        self.assertEqual(contract.stripe_payment_method, "gopay")
        self.assertEqual(contract.checkout_payload(), {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": "ID", "currency": "IDR"},
            "checkout_ui_mode": "custom",
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
        })
        init = contract.stripe_init_payload("pk_live_fixture", stripe_js_id="fixture-js-id")
        self.assertEqual(init["browser_locale"], "id-ID")
        self.assertEqual(init["browser_timezone"], "Asia/Jakarta")
        self.assertEqual(init["elements_session_client[stripe_js_id]"], "fixture-js-id")

    def test_checkout_response_accepts_current_session_id_shapes(self):
        session = CheckoutSessionContract.from_payload(
            {"checkout_session_id": "oaics_fixture", "publishable_key": "pk_live_fixture"},
            billing_country="PH",
        )
        self.assertEqual(session.checkout_session_id, "oaics_fixture")
        self.assertEqual(session.processor_entity, "openai_ie")

    def test_stripe_capability_collects_nested_and_custom_methods(self):
        evidence = StripeCapabilityEvidence.from_payload({
            "elements_options": {"amount": 0, "currency": "idr", "payment_method_types": ["card", "gopay"]},
            "ordered_payment_method_types": ["gopay", "card"],
            "custom_payment_methods": [{"type": "gcash"}],
        })
        self.assertEqual(evidence.amount_minor, 0)
        self.assertEqual(evidence.currency, "IDR")
        self.assertEqual(evidence.payment_method_types, ("card", "gopay", "gcash"))
        self.assertEqual(evidence.classification_for("gopay"), ("eligible", True))

    def test_contract_rejects_invalid_country_before_network(self):
        with self.assertRaises(CheckoutContractError):
            CheckoutRequestContract.for_payment_method("gopay", billing_country="IND")


if __name__ == "__main__":
    unittest.main()
