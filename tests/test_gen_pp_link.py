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


class FakeParamResponse:
    status_code = 400
    text = '{"error":{"code":"parameter_unknown","type":"invalid_request_error","message":"Invalid locale","param":"elements_session_client[locale]"}}'

    def json(self):
        return {
            "error": {
                "code": "parameter_unknown",
                "type": "invalid_request_error",
                "message": "Invalid locale",
                "param": "elements_session_client[locale]",
            }
        }


class FakeOkResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


class FakeStripeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append(dict(data or {}))
        if len(self.calls) == 1:
            return FakeParamResponse()
        return FakeOkResponse()


class StripeConfirmErrorTests(unittest.TestCase):
    def test_confirm_decline_is_terminal(self):
        details = gen_pp_link._stripe_error_details(FakeResponse())

        self.assertEqual(details["status"], 402)
        self.assertEqual(details["code"], "setup_attempt_failed")
        self.assertEqual(details["decline_code"], "generic_decline")
        self.assertTrue(gen_pp_link._is_terminal_confirm_decline(details))

    def test_stripe_error_details_include_param(self):
        details = gen_pp_link._stripe_error_details(FakeParamResponse())

        self.assertEqual(details["status"], 400)
        self.assertEqual(details["type"], "invalid_request_error")
        self.assertEqual(details["param"], "elements_session_client[locale]")

    def test_unknown_stripe_param_is_removed_and_retried(self):
        session = FakeStripeSession()

        response = gen_pp_link._post_stripe_form(
            session,
            "https://api.stripe.com/v1/payment_pages/cs_test/init",
            {"elements_session_client[locale]": "ja", "key": "pk_test"},
            timeout=30,
            step="stripe init",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session.calls), 2)
        self.assertIn("elements_session_client[locale]", session.calls[0])
        self.assertNotIn("elements_session_client[locale]", session.calls[1])
        self.assertEqual(response.removed_unknown_params, ["elements_session_client[locale]"])

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

    def test_explicit_proxy_is_forced_for_regeneration(self):
        terminal_result = {
            "ok": False,
            "error": "terminal",
            "error_code": "terminal",
            "terminal": True,
        }
        cfg = {
            "paypal": {
                "proxies": ["direct"],
                "stage_proxies": {"checkout": "direct", "stripe_init": "direct", "payment_method": "direct", "confirm": "direct"},
                "max_checkout_retries": 3,
            }
        }
        proxy = "socks5h://127.0.0.1:7897"

        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "_try_paypal_link", return_value=terminal_result) as try_paypal:
                result = gen_pp_link.generate_pp_link("eyJ.fake.token", proxy=proxy)

        self.assertEqual(result["checkout_attempt"], 1)
        self.assertEqual(try_paypal.call_args.args[3], proxy)
        self.assertTrue(try_paypal.call_args.kwargs["force_proxy"])
        self.assertEqual(gen_pp_link._stage_proxy(cfg["paypal"], "checkout", proxy, force_fallback=True), proxy)

    def test_configured_japan_billing_region(self):
        regions = gen_pp_link._billing_regions({"billing_regions": ["JP"]})

        self.assertEqual(regions[0]["country"], "JP")
        self.assertEqual(regions[0]["currency"], "JPY")
        self.assertEqual(regions[0]["browser_timezone"], "Asia/Tokyo")
        self.assertEqual(regions[0]["address"]["country"], "JP")

    def test_configured_us_billing_region_matches_original_flow(self):
        regions = gen_pp_link._billing_regions({"billing_regions": ["US"]})

        self.assertEqual(regions[0]["country"], "US")
        self.assertEqual(regions[0]["currency"], "USD")
        self.assertEqual(regions[0]["browser_timezone"], "Asia/Shanghai")
        self.assertEqual(regions[0]["address"]["country"], "US")

    def test_gopay_default_billing_region_is_indonesia(self):
        cfg = {"paypal": {"billing_regions": ["US"]}}
        payment_cfg = gen_pp_link._payment_cfg(cfg, "gopay")
        regions = gen_pp_link._billing_regions(payment_cfg)

        self.assertEqual(regions[0]["country"], "ID")
        self.assertEqual(regions[0]["currency"], "IDR")
        self.assertEqual(regions[0]["browser_timezone"], "Asia/Jakarta")

    def test_generate_payment_link_passes_gopay_method(self):
        ok_result = {"ok": True, "url": "https://app.midtrans.com/snap/v4/redirection/snap"}
        cfg = {
            "paypal": {"proxies": ["direct"], "max_checkout_retries": 3},
            "gopay": {"billing_regions": ["ID"], "max_checkout_retries": 1},
        }

        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "_try_paypal_link", return_value=ok_result) as try_paypal:
                result = gen_pp_link.generate_payment_link("eyJ.fake.token", payment_method="gopay")

        self.assertTrue(result["ok"])
        self.assertEqual(result["payment_method"], "gopay")
        self.assertEqual(try_paypal.call_args.kwargs["payment_method"], "gopay")
        self.assertEqual(try_paypal.call_args.args[2]["country"], "ID")


if __name__ == "__main__":
    unittest.main()
