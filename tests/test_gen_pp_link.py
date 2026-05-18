import unittest
from unittest.mock import patch

from sms_tool import gen_pp_link


class FakeResponse:
    status_code = 402
    text = '{"error":{"code":"setup_attempt_failed","decline_code":"generic_decline"}}'

    def json(self):
        return {
            "error": {
                "code": "setup_attempt_failed",
                "decline_code": "generic_decline",
                "type": "card_error",
                "message": "Your payment method was declined.",
                "doc_url": "https://stripe.com/docs/error-codes/generic-decline",
            }
        }


class StripeConfirmErrorTests(unittest.TestCase):
    def test_confirm_decline_is_terminal(self):
        details = gen_pp_link._stripe_error_details(FakeResponse())

        self.assertEqual(details["status"], 402)
        self.assertEqual(details["code"], "setup_attempt_failed")
        self.assertEqual(details["decline_code"], "generic_decline")
        self.assertTrue(gen_pp_link._is_terminal_confirm_decline(details))

    def test_terminal_confirm_decline_stops_retry_loop(self):
        terminal_result = {
            "ok": False,
            "error": "Stripe confirm declined: status=402 reason=generic_decline",
            "error_code": "stripe_confirm_declined",
            "terminal": True,
        }
        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"proxies": ["direct"], "max_checkout_retries": 3}}):
            with patch.object(gen_pp_link, "_try_paypal_link", return_value=terminal_result) as try_paypal:
                result = gen_pp_link.generate_pp_link("eyJ.fake.token")

        self.assertEqual(result["error_code"], "stripe_confirm_declined")
        self.assertEqual(result["checkout_attempt"], 1)
        self.assertEqual(try_paypal.call_count, 1)


if __name__ == "__main__":
    unittest.main()
