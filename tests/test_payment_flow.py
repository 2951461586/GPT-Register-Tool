import unittest

from sms_tool.payment_flow import payment_flow_profile


class PaymentFlowProfileTests(unittest.TestCase):
    def test_gopay_profile_covers_promotion_and_approve(self):
        profile = payment_flow_profile("gopay")
        self.assertEqual(profile.key, "wallet_redirect")
        self.assertIn("promotion", profile.stages)
        self.assertIn("approve", profile.stages)

    def test_config_can_replace_profile_stages(self):
        profile = payment_flow_profile("paypal", {
            "flow_profile": "checkout_only",
            "stages": ["auth_gate", "checkout", "artifact"],
        })
        self.assertEqual(profile.key, "checkout_only")
        self.assertEqual(profile.stages, ("auth_gate", "checkout", "artifact"))


if __name__ == "__main__":
    unittest.main()
