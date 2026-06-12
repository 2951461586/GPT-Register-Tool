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


class FakePollParamResponse:
    status_code = 400
    text = '{"error":{"code":"parameter_unknown","type":"invalid_request_error","message":"Unknown","param":"elements_options_client[stripe_js_locale]"}}'

    def json(self):
        return {
            "error": {
                "code": "parameter_unknown",
                "type": "invalid_request_error",
                "message": "Unknown",
                "param": "elements_options_client[stripe_js_locale]",
            }
        }


class FakeOkResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


class FakeAcceptedResponse:
    status_code = 202
    text = "{}"

    def json(self):
        return {}


class FakeUnprocessableResponse:
    status_code = 422
    text = "{}"

    def json(self):
        return {}


class FakeRateLimitedResponse:
    status_code = 429
    text = ""
    headers = {"Retry-After": "120"}

    def json(self):
        return {}


class FakeCheckoutOkResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "checkout_session_id": "cs_live_TEST123",
            "processor_entity": "openai_llc",
            "url": "https://checkout.stripe.com/c/pay/cs_live_TEST123",
        }


class FakeCheckoutOpenAiIeResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "checkout_session_id": "cs_live_IE123",
            "processor_entity": "openai_ie",
            "url": "https://checkout.stripe.com/c/pay/cs_live_IE123",
            "publishable_key": "pk_live_OPENAI_IE_TEST",
        }


class FakeCheckoutHostedResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "checkout_session_id": "cs_live_HOSTED123",
            "processor_entity": "openai_llc",
            "url": "https://pay.openai.com/c/pay/cs_live_HOSTED123#fidkdWxOYHwnPyd1blppbHNg",
        }


class FakeCheckoutHostedMissingProviderResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "checkout_session_id": "cs_live_FAKEHOSTED123",
            "processor_entity": "openai_ie",
            "url": "https://chatgpt.com/checkout/openai_ie/cs_live_FAKEHOSTED123",
        }


class FakeCheckoutStripeHostedFieldResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "checkout_session_id": "cs_live_STRIPEHOSTED123",
            "processor_entity": "openai_ie",
            "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_STRIPEHOSTED123#fidkdWxOYHwnPyd1blppbHNg",
        }


class FakeCheckout422Response:
    status_code = 422
    text = '{"error":"currency not supported"}'

    def raise_for_status(self):
        raise RuntimeError("422 should be handled before raise_for_status")

    def json(self):
        return {"error": "currency not supported"}


class FakeZeroStripeInitResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "init_checksum": "init_test",
            "total_summary": {"due": 0, "total": 0},
            "invoice": {"amount_due": 0, "total": 0, "currency": "usd"},
            "payment_method_types": ["card", "paypal"],
        }


class FakeNonZeroStripeInitResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "init_checksum": "init_test",
            "total_summary": {"due": 2300, "total": 2300},
            "invoice": {"amount_due": 2300, "total": 2300, "currency": "eur"},
            "payment_method_types": ["card", "paypal"],
        }


class FakeStripeInitHostedResponse:
    def __init__(self, stripe_hosted_url: str, currency: str = "usd", payment_method_types=None):
        self.status_code = 200
        self.text = "{}"
        self._stripe_hosted_url = stripe_hosted_url
        self._currency = currency
        self._payment_method_types = payment_method_types or ["card", "paypal"]

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "init_checksum": "init_test",
            "total_summary": {"due": 0, "total": 0},
            "invoice": {"amount_due": 0, "total": 0, "currency": self._currency},
            "payment_method_types": self._payment_method_types,
            "stripe_hosted_url": self._stripe_hosted_url,
        }


class FakeElementsSessionResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "session_id": "elements_session_FROM_STRIPE",
            "payment_method_preference": {
                "ordered_payment_method_types": ["card", "paypal"],
            },
            "paypal_express_config": {
                "paypal_merchant_id": "CF9F8FKTUYUAY",
            },
        }


class FakePaymentMethodResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"id": "pm_TESTPAYPAL"}


class FakeConfirmRedirectResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "setup_intent": {
                "next_action": {
                    "type": "redirect_to_url",
                    "redirect_to_url": {
                        "url": (
                            "https://pm-redirects.stripe.com/authorize/acct_1HOrSwC6h1nxGoI3/"
                            "sa_nonce_Ud4yVTu0JcHjhTVXTNTrX4IFmsQOdmp"
                            "?useWebAuthSession=true&followRedirectsInSDK=true"
                        )
                    },
                }
            }
        }


class FakeConfirmOpenResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"status": "open", "mode": "subscription", "setup_intent": {}, "payment_intent": {}}


class FakePaymentPageRedirectResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "status": "open",
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "type": "redirect_to_url",
                    "redirect_to_url": {
                        "url": (
                            "https://pm-redirects.stripe.com/authorize/acct_1HOrSwC6h1nxGoI3/"
                            "sa_nonce_POSTAPPROVE?useWebAuthSession=true&followRedirectsInSDK=true"
                        )
                    },
                },
            },
        }


class FakePaymentPageOpenResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"status": "open", "mode": "subscription", "setup_intent": {}, "payment_intent": {}}


class FakeApproveResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"result": "approved"}


class FakeApproveBlockedResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"result": "blocked"}


class FakeStripeSession:
    def __init__(self):
        self.calls = []
        self.get_calls = []
        self.headers = {}
        self.proxies = {}

    def post(self, url, data=None, timeout=None):
        self.calls.append(dict(data or {}))
        if len(self.calls) == 1:
            return FakeParamResponse()
        return FakeOkResponse()

    def get(self, url, params=None, timeout=None):
        self.get_calls.append({"url": url, "params": list(params or []), "timeout": timeout})
        if "/v1/payment_pages/" in url:
            return FakePaymentPageRedirectResponse()
        return FakeElementsSessionResponse()


class FakeCheckoutSession:
    def __init__(self, response, approve_response=None):
        self.response = response
        self.approve_response = approve_response or FakeApproveResponse()
        self.calls = []
        self.headers = {"User-Agent": "fake-browser"}
        self.proxies = {}

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": None if json is None else dict(json or {}), "headers": dict(self.headers)})
        if "checkout/approve" in url:
            return self.approve_response
        if "sentinel/ping" in url:
            return FakeOkResponse()
        return self.response

    def get(self, url, params=None, timeout=None):
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

    def test_checkout_route_load_retries_accepted_until_ok(self):
        class RouteSession:
            def __init__(self):
                self.headers = {}
                self.calls = []

            def get(self, url, params=None, timeout=None):
                self.calls.append({"url": url, "params": params})
                if url.endswith(".data"):
                    return FakeAcceptedResponse() if len([c for c in self.calls if c["url"].endswith(".data")]) == 1 else FakeOkResponse()
                return FakeOkResponse()

        session = RouteSession()

        result = gen_pp_link._chatgpt_load_checkout_route(
            session,
            checkout_url="https://chatgpt.com/checkout/openai_ie/cs_live_TEST",
            log_prefix="[paypal]",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual([a["status"] for a in result["attempts"]], [202, 200])

    def test_checkout_snapshot_retries_unprocessable_until_ok(self):
        class SnapshotSession:
            def __init__(self):
                self.headers = {}
                self.calls = 0

            def post(self, url, json=None, data=None, timeout=None):
                self.calls += 1
                return FakeUnprocessableResponse() if self.calls <= 5 else FakeOkResponse()

        session = SnapshotSession()

        result = gen_pp_link._chatgpt_checkout_snapshot(
            session,
            checkout_url="https://chatgpt.com/checkout/openai_ie/cs_live_TEST",
            cs_id="cs_live_TEST",
            processor_entity="openai_ie",
            log_prefix="[paypal]",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertGreater(len(result["attempts"]), 5)

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

    def test_missing_hosted_url_falls_back_to_stripe_redirect(self):
        missing_hosted = {
            "ok": False,
            "error": "Hosted checkout requested, but ChatGPT did not return a hosted URL",
            "error_code": "hosted_checkout_url_missing",
            "terminal": True,
        }
        redirect_ok = {
            "ok": True,
            "url": "https://pm-redirects.stripe.com/authorize/acct/test",
            "link_type": "stripe_redirect",
        }

        with patch.object(
            gen_pp_link,
            "_load_json",
            return_value={
                "paypal": {
                    "proxies": ["direct"],
                    "max_checkout_retries": 3,
                    "checkout_only_long_url": False,
                    "checkout_ui_mode": "hosted",
                    "link_mode": "chatgpt_checkout",
                    "confirm_style": "inline_payment_method_data",
                    "approve_missing_redirect": False,
                }
            },
        ):
            with patch.object(gen_pp_link, "_try_paypal_link", side_effect=[missing_hosted, redirect_ok]) as try_paypal:
                result = gen_pp_link.generate_pp_link("eyJ.fake.token")

        self.assertTrue(result["ok"])
        self.assertEqual(result["fallback_from"], "hosted_checkout_url_missing")
        self.assertEqual(result["fallback_link_mode"], "stripe_redirect")
        self.assertEqual(result["checkout_attempt"], 1)
        self.assertEqual(try_paypal.call_count, 2)
        self.assertEqual(try_paypal.call_args_list[0].args[1]["paypal"]["checkout_ui_mode"], "hosted")
        self.assertEqual(try_paypal.call_args_list[1].args[1]["paypal"]["checkout_ui_mode"], "custom")
        self.assertEqual(try_paypal.call_args_list[1].args[1]["paypal"]["link_mode"], "stripe_redirect")
        self.assertEqual(try_paypal.call_args_list[1].args[1]["paypal"]["confirm_style"], "payment_method_id")
        self.assertTrue(try_paypal.call_args_list[1].args[1]["paypal"]["approve_missing_redirect"])

    def test_checkout_rate_limit_stops_retry_loop(self):
        rate_limited = {
            "ok": False,
            "error": "checkout rate limited: status=429 retry_after=120",
            "error_code": "checkout_rate_limited",
            "terminal": True,
            "retryable": True,
            "retry_after": "120",
        }
        with patch.object(gen_pp_link, "_load_json", return_value={"paypal": {"proxies": ["direct"], "max_checkout_retries": 3}}):
            with patch.object(gen_pp_link, "_try_paypal_link", return_value=rate_limited) as try_paypal:
                result = gen_pp_link.generate_pp_link("eyJ.fake.token")

        self.assertEqual(result["error_code"], "checkout_rate_limited")
        self.assertEqual(result["retry_after"], "120")
        self.assertEqual(result["checkout_attempt"], 1)
        self.assertEqual(try_paypal.call_count, 1)

    def test_try_paypal_link_returns_checkout_rate_limit(self):
        checkout = FakeCheckoutSession(FakeRateLimitedResponse())
        cfg = {"paypal": {"stage_proxies": {"checkout": "direct"}}}
        region = {
            "country": "US",
            "currency": "USD",
            "label": "United States (USD)",
            "address": {"country": "US"},
        }

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()):
                result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertEqual(result["error_code"], "checkout_rate_limited")
        self.assertTrue(result["terminal"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["retry_after"], "120")
        self.assertEqual(len(checkout.calls), 1)

    def test_checkout_body_defaults_to_custom_ui_with_promo(self):
        body = gen_pp_link._checkout_body(
            {},
            {"country": "US", "currency": "USD"},
            "plus-1-month-free",
        )

        self.assertEqual(body["checkout_ui_mode"], "custom")
        self.assertEqual(body["cancel_url"], "https://chatgpt.com/#pricing")
        self.assertEqual(body["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")

    def test_checkout_body_can_disable_promo(self):
        body = gen_pp_link._checkout_body(
            {"checkout_ui_mode": "custom"},
            {"country": "US", "currency": "USD"},
            "",
        )

        self.assertEqual(body["checkout_ui_mode"], "custom")
        self.assertEqual(body["cancel_url"], "https://chatgpt.com/#pricing")
        self.assertNotIn("promo_campaign", body)

    def test_paypal_default_link_mode_matches_plugin_checkout(self):
        self.assertEqual(gen_pp_link._payment_link_mode({}, "paypal"), "chatgpt_checkout")
        self.assertEqual(gen_pp_link._payment_link_mode({}, "gopay"), "stripe_redirect")
        self.assertEqual(gen_pp_link._payment_link_mode({}, "upi"), "chatgpt_checkout")

    def test_paypal_chatgpt_checkout_link_mode_skips_confirm(self):
        checkout = FakeCheckoutSession(FakeCheckoutOkResponse())
        cfg = {
            "paypal": {
                "stop_after_pm_create": False,
                "checkout_only_long_url": False,
                "require_zero_due": True,
                "refresh_tax_region": False,
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = {
            "country": "US",
            "currency": "USD",
            "label": "United States (USD)",
            "address": {"country": "US"},
        }

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()):
                with patch.object(gen_pp_link, "_post_stripe_form", return_value=FakeZeroStripeInitResponse()) as post_form:
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "chatgpt_checkout")
        self.assertEqual(result["url"], "https://chatgpt.com/checkout/openai_llc/cs_live_TEST123")
        self.assertEqual(result["pm_id"], "")
        self.assertTrue(result["zero_due_verified"])
        self.assertEqual(result["stage_proxies"]["payment_method"], "SKIPPED")
        called_urls = [call.args[1] for call in post_form.call_args_list]
        self.assertEqual(called_urls, ["https://api.stripe.com/v1/payment_pages/cs_live_TEST123/init"])
        self.assertFalse(any("/confirm" in url or "/payment_methods" in url for url in called_urls))

    def test_checkout_response_publishable_key_is_used_for_openai_ie(self):
        checkout = FakeCheckoutSession(FakeCheckoutOpenAiIeResponse())
        cfg = {
            "paypal": {
                "link_mode": "chatgpt_checkout",
                "stop_after_pm_create": False,
                "checkout_only_long_url": False,
                "require_zero_due": True,
                "refresh_tax_region": False,
                "stage_proxies": {"checkout": "direct"},
            },
            "stripe": {
                "publishable_key": "pk_live_WRONG_ACCOUNT",
            },
        }
        region = {
            "country": "DE",
            "currency": "EUR",
            "label": "Germany (EUR)",
            "address": {"country": "DE"},
        }
        posted_bodies = {}

        def fake_post_form(session, url, body, *, timeout, step):
            posted_bodies[step] = dict(body)
            return FakeZeroStripeInitResponse()

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()):
                with patch.object(gen_pp_link, "_post_stripe_form", side_effect=fake_post_form):
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["processor_entity"], "openai_ie")
        self.assertEqual(result["stripe_publishable_key_source"], "checkout_response")
        self.assertEqual(posted_bodies["stripe init"]["key"], "pk_live_OPENAI_IE_TEST")

    def test_paypal_hosted_checkout_link_mode_uses_provider_long_url(self):
        checkout = FakeCheckoutSession(FakeCheckoutHostedResponse())
        cfg = {
            "paypal": {
                "link_mode": "chatgpt_checkout",
                "stop_after_pm_create": False,
                "checkout_only_long_url": False,
                "checkout_ui_mode": "hosted",
                "require_zero_due": True,
                "refresh_tax_region": False,
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = {
            "country": "US",
            "currency": "USD",
            "label": "United States (USD)",
            "address": {"country": "US"},
        }

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()):
                with patch.object(
                    gen_pp_link,
                    "_post_stripe_form",
                    return_value=FakeStripeInitHostedResponse(
                        "https://checkout.stripe.com/c/pay/cs_live_HOSTED123#fidkdWxOYHwnPyd1blppbHNg",
                        currency="eur"
                    ),
                ) as post_form:
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["checkout_ui_mode"], "hosted")
        self.assertEqual(result["url"], "https://pay.openai.com/c/pay/cs_live_HOSTED123#fidkdWxOYHwnPyd1blppbHNg")
        self.assertEqual(result["hosted_checkout_url"], "https://pay.openai.com/c/pay/cs_live_HOSTED123#fidkdWxOYHwnPyd1blppbHNg")
        self.assertEqual(result["provider_url"], "https://pay.openai.com/c/pay/cs_live_HOSTED123#fidkdWxOYHwnPyd1blppbHNg")
        called_urls = [call.args[1] for call in post_form.call_args_list]
        self.assertEqual(called_urls, ["https://api.stripe.com/v1/payment_pages/cs_live_HOSTED123/init"])

    def test_paypal_hosted_checkout_uses_stripe_init_pay_openai_url(self):
        checkout = FakeCheckoutSession(FakeCheckoutHostedMissingProviderResponse())
        cfg = {
            "paypal": {
                "link_mode": "chatgpt_checkout",
                "stop_after_pm_create": False,
                "checkout_only_long_url": False,
                "checkout_ui_mode": "hosted",
                "require_zero_due": True,
                "refresh_tax_region": False,
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = {
            "country": "DE",
            "currency": "EUR",
            "label": "Germany (EUR)",
            "address": {"country": "DE"},
        }

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()):
                with patch.object(
                    gen_pp_link,
                    "_post_stripe_form",
                    return_value=FakeStripeInitHostedResponse(
                        "https://checkout.stripe.com/c/pay/cs_live_FAKEHOSTED123#fidkdWxOYHwnPyd1blppbHNg"
                    ),
                ):
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://pay.openai.com/c/pay/cs_live_FAKEHOSTED123#fidkdWxOYHwnPyd1blppbHNg")
        self.assertEqual(result["hosted_checkout_url"], "https://pay.openai.com/c/pay/cs_live_FAKEHOSTED123#fidkdWxOYHwnPyd1blppbHNg")
        self.assertEqual(result["provider_url"], "https://chatgpt.com/checkout/openai_ie/cs_live_FAKEHOSTED123")
        self.assertIn("pay.openai.com/c/pay/cs_live_FAKEHOSTED123", str(result))

    def test_paypal_default_checkout_only_long_url_uses_de_and_stripe_init(self):
        checkout = FakeCheckoutSession(FakeCheckoutHostedResponse())
        cfg = {
            "paypal": {
                "checkout_only_long_url": True,
                "billing_regions": ["DE"],
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = gen_pp_link._billing_regions(cfg["paypal"])[0]

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()) as new_session:
                with patch.object(
                    gen_pp_link,
                    "_post_stripe_form",
                    return_value=FakeStripeInitHostedResponse(
                        "https://checkout.stripe.com/c/pay/cs_live_HOSTED123#fidkdWxOYHwnPyd1blppbHNg",
                        currency="eur"
                    ),
                ) as post_form:
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "hosted_long_url")
        self.assertEqual(result["url"], "https://pay.openai.com/c/pay/cs_live_HOSTED123#fidkdWxOYHwnPyd1blppbHNg")
        self.assertEqual(result["hosted_checkout_url"], "https://pay.openai.com/c/pay/cs_live_HOSTED123#fidkdWxOYHwnPyd1blppbHNg")
        self.assertEqual(result["billing_country"], "DE")
        self.assertEqual(result["currency"], "EUR")
        self.assertEqual(result["stage_proxies"]["stripe_init"], "DIRECT")
        self.assertEqual(result["stage_proxies"]["payment_method"], "SKIPPED")
        self.assertEqual(result["stage_proxies"]["confirm"], "SKIPPED")
        self.assertGreaterEqual(new_session.call_count, 1)
        self.assertGreaterEqual(post_form.call_count, 1)
        checkout_posts = [call for call in checkout.calls if "/payments/checkout" in call["url"]]
        self.assertEqual(len(checkout_posts), 1)
        self.assertEqual(checkout_posts[0]["json"]["billing_details"], {"country": "DE", "currency": "EUR"})
        self.assertEqual(checkout_posts[0]["json"]["checkout_ui_mode"], "hosted")

    def test_paypal_default_checkout_only_rejects_chatgpt_checkout_when_no_hosted_url(self):
        checkout = FakeCheckoutSession(FakeCheckoutHostedMissingProviderResponse())
        cfg = {
            "paypal": {
                "checkout_only_long_url": True,
                "billing_regions": ["DE"],
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = gen_pp_link._billing_regions(cfg["paypal"])[0]

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()) as new_session:
                with patch.object(gen_pp_link, "_post_stripe_form", return_value=FakeZeroStripeInitResponse()) as post_form:
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "hosted_checkout_url_missing")
        self.assertEqual(result["billing_country"], "DE")
        self.assertEqual(result["stage_proxies"]["stripe_init"], "DIRECT")
        self.assertGreaterEqual(new_session.call_count, 1)
        self.assertGreaterEqual(post_form.call_count, 1)

    def test_checkout_only_allow_flag_does_not_bypass_hosted_url_requirement(self):
        checkout = FakeCheckoutSession(FakeCheckoutHostedMissingProviderResponse())
        cfg = {
            "paypal": {
                "checkout_only_long_url": True,
                "allow_chatgpt_checkout_fallback": True,
                "billing_regions": ["DE"],
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = gen_pp_link._billing_regions(cfg["paypal"])[0]

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()) as new_session:
                with patch.object(gen_pp_link, "_post_stripe_form", return_value=FakeZeroStripeInitResponse()) as post_form:
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "hosted_checkout_url_missing")
        self.assertGreaterEqual(new_session.call_count, 1)
        self.assertGreaterEqual(post_form.call_count, 1)

    def test_checkout_only_uses_js_hosted_url_priority(self):
        checkout = FakeCheckoutSession(FakeCheckoutStripeHostedFieldResponse())
        cfg = {
            "paypal": {
                "checkout_only_long_url": True,
                "billing_regions": ["DE"],
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = gen_pp_link._billing_regions(cfg["paypal"])[0]

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()) as new_session:
                with patch.object(
                    gen_pp_link,
                    "_post_stripe_form",
                    return_value=FakeStripeInitHostedResponse(
                        "https://checkout.stripe.com/c/pay/cs_live_STRIPEHOSTED123#fidkdWxOYHwnPyd1blppbHNg",
                        currency="eur"
                    ),
                ) as post_form:
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "hosted_long_url")
        self.assertEqual(
            result["url"],
            "https://pay.openai.com/c/pay/cs_live_STRIPEHOSTED123#fidkdWxOYHwnPyd1blppbHNg",
        )
        self.assertEqual(result["hosted_checkout_url"], "https://pay.openai.com/c/pay/cs_live_STRIPEHOSTED123#fidkdWxOYHwnPyd1blppbHNg")
        self.assertEqual(result["checkout_response_url"], "https://checkout.stripe.com/c/pay/cs_live_STRIPEHOSTED123#fidkdWxOYHwnPyd1blppbHNg")
        self.assertGreaterEqual(new_session.call_count, 1)
        self.assertGreaterEqual(post_form.call_count, 1)

    def test_checkout_only_422_retries_same_country_with_usd(self):
        class SequentialCheckoutSession(FakeCheckoutSession):
            def __init__(self):
                super().__init__(FakeCheckout422Response())
                self.responses = [FakeCheckout422Response(), FakeCheckoutHostedResponse()]

            def post(self, url, json=None, timeout=None):
                self.calls.append({"url": url, "json": None if json is None else dict(json or {}), "headers": dict(self.headers)})
                return self.responses.pop(0)

        checkout = SequentialCheckoutSession()
        cfg = {
            "paypal": {
                "checkout_only_long_url": True,
                "hosted_usd_fallback_on_422": True,
                "billing_regions": ["DE"],
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = gen_pp_link._billing_regions(cfg["paypal"])[0]

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()) as new_session:
                with patch.object(
                    gen_pp_link,
                    "_post_stripe_form",
                    return_value=FakeStripeInitHostedResponse(
                        "https://checkout.stripe.com/c/pay/cs_live_HOSTED123#fidkdWxOYHwnPyd1blppbHNg",
                        currency="usd",
                    ),
                ):
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        checkout_posts = [call for call in checkout.calls if "/payments/checkout" in call["url"]]
        self.assertEqual(checkout_posts[0]["json"]["billing_details"], {"country": "DE", "currency": "EUR"})
        self.assertEqual(checkout_posts[1]["json"]["billing_details"], {"country": "DE", "currency": "USD"})
        self.assertEqual(result["currency"], "USD")
        self.assertGreaterEqual(new_session.call_count, 1)

    def test_checkout_only_non_hosted_retries_usd_then_rejects_if_still_non_hosted(self):
        class SequentialCheckoutSession(FakeCheckoutSession):
            def __init__(self):
                super().__init__(FakeCheckoutHostedMissingProviderResponse())
                self.responses = [
                    FakeCheckoutHostedMissingProviderResponse(),
                    FakeCheckoutHostedMissingProviderResponse(),
                ]

            def post(self, url, json=None, timeout=None):
                self.calls.append({"url": url, "json": None if json is None else dict(json or {}), "headers": dict(self.headers)})
                return self.responses.pop(0)

        checkout = SequentialCheckoutSession()
        cfg = {
            "paypal": {
                "checkout_only_long_url": True,
                "hosted_usd_fallback_on_non_hosted": True,
                "billing_regions": ["DE"],
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = gen_pp_link._billing_regions(cfg["paypal"])[0]

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()) as new_session:
                with patch.object(gen_pp_link, "_post_stripe_form", return_value=FakeZeroStripeInitResponse()):
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "hosted_checkout_url_missing")
        checkout_posts = [call for call in checkout.calls if "/payments/checkout" in call["url"]]
        self.assertEqual(checkout_posts[0]["json"]["billing_details"], {"country": "DE", "currency": "EUR"})
        self.assertEqual(len(checkout_posts), 1)
        self.assertGreaterEqual(new_session.call_count, 1)

    def test_paypal_stop_after_pm_create_returns_success_without_confirm(self):
        checkout = FakeCheckoutSession(FakeCheckoutOkResponse())
        stripe_session = FakeStripeSession()
        cfg = {
            "paypal": {
                "link_mode": "stripe_redirect",
                "checkout_only_long_url": False,
                "stop_after_pm_create": True,
                "require_zero_due": True,
                "use_elements_session": True,
                "refresh_tax_region": True,
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = {
            "country": "US",
            "currency": "USD",
            "label": "United States (USD)",
            "address": {"country": "US"},
        }
        steps = []

        def fake_post_form(session, url, body, *, timeout, step):
            steps.append(step)
            if step == "pm create":
                return FakePaymentMethodResponse()
            if step.startswith("confirm"):
                self.fail("confirm must not run when stop_after_pm_create is enabled")
            return FakeZeroStripeInitResponse()

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=stripe_session):
                with patch.object(gen_pp_link, "_post_stripe_form", side_effect=fake_post_form):
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "pm_created")
        self.assertEqual(result["paypal_status"], "pm_created")
        self.assertEqual(result["pm_id"], "pm_TESTPAYPAL")
        self.assertEqual(result["url"], "")
        self.assertIn("pm create", steps)
        self.assertNotIn("confirm", steps)
        self.assertEqual(result["stage_proxies"]["confirm"], "SKIPPED")

    def test_paypal_stripe_redirect_uses_stripe_elements_session_id(self):
        checkout = FakeCheckoutSession(FakeCheckoutOkResponse())
        stripe_session = FakeStripeSession()
        cfg = {
            "paypal": {
                "link_mode": "stripe_redirect",
                "stop_after_pm_create": False,
                "checkout_only_long_url": False,
                "require_zero_due": True,
                "use_elements_session": True,
                "refresh_tax_region": True,
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = {
            "country": "US",
            "currency": "USD",
            "label": "United States (USD)",
            "address": {"country": "US"},
        }
        posted_bodies = {}

        def fake_post_form(session, url, body, *, timeout, step):
            posted_bodies[step] = dict(body)
            if step == "pm create":
                return FakePaymentMethodResponse()
            if step.startswith("confirm"):
                return FakeConfirmRedirectResponse()
            return FakeZeroStripeInitResponse()

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=stripe_session):
                with patch.object(gen_pp_link, "_post_stripe_form", side_effect=fake_post_form):
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["elements_session_id"], "elements_session_FROM_STRIPE")
        self.assertEqual(posted_bodies["tax refresh"]["elements_session_client[session_id]"], "elements_session_FROM_STRIPE")
        self.assertEqual(posted_bodies["confirm"]["elements_session_client[session_id]"], "elements_session_FROM_STRIPE")
        self.assertEqual(posted_bodies["confirm"]["client_attribution_metadata[merchant_integration_additional_elements][0]"], "expressCheckout")
        self.assertEqual(posted_bodies["confirm"]["elements_options_client[saved_payment_method][enable_save]"], "never")
        self.assertEqual(posted_bodies["confirm"]["elements_options_client[saved_payment_method][enable_redisplay]"], "never")
        element_params = dict(stripe_session.get_calls[0]["params"])
        self.assertEqual(element_params["checkout_session_id"], "cs_live_TEST123")
        self.assertEqual(element_params["deferred_intent[payment_method_types][1]"], "paypal")

    def test_reference_confirm_mode_matches_fast_external_flow(self):
        checkout = FakeCheckoutSession(FakeCheckoutOpenAiIeResponse())
        stripe_session = FakeStripeSession()
        cfg = {
            "paypal": {
                "reference_confirm_mode": True,
                "link_mode": "chatgpt_checkout",
                "checkout_ui_mode": "custom",
                "require_zero_due": True,
                "use_elements_session": True,
                "refresh_tax_region": True,
                "approve_missing_redirect": True,
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = gen_pp_link._billing_regions({"billing_regions": ["DE"]})[0]
        steps = []
        posted_bodies = {}

        def fake_post_form(session, url, body, *, timeout, step):
            steps.append(step)
            posted_bodies[step] = dict(body)
            if step == "pm create":
                return FakePaymentMethodResponse()
            if step.startswith("confirm"):
                return FakeConfirmRedirectResponse()
            return FakeZeroStripeInitResponse()

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=stripe_session):
                with patch.object(gen_pp_link, "_post_stripe_form", side_effect=fake_post_form):
                    with patch.object(gen_pp_link, "_chatgpt_load_checkout_route") as route_load:
                        with patch.object(gen_pp_link, "_chatgpt_checkout_snapshot") as snapshot:
                            with patch.object(gen_pp_link, "_chatgpt_approve_checkout") as approve:
                                result = gen_pp_link._try_paypal_link(
                                    "eyJ.fake.token",
                                    cfg,
                                    region,
                                    "",
                                    payment_method="paypal",
                                )

        self.assertTrue(result["ok"])
        self.assertIn("pm-redirects.stripe.com/authorize", result["url"])
        self.assertEqual(result["checkout_ui_mode"], "hosted")
        self.assertEqual(result["link_mode"], "stripe_redirect")
        self.assertEqual(result["region"], "Germany (EUR)")
        self.assertEqual(steps, ["stripe init", "pm create", "confirm"])
        self.assertNotIn("tax refresh", steps)
        route_load.assert_not_called()
        snapshot.assert_not_called()
        approve.assert_not_called()
        checkout_posts = [call for call in checkout.calls if "/payments/checkout" in call["url"]]
        self.assertEqual(checkout_posts[0]["json"]["billing_details"], {"country": "DE", "currency": "EUR"})
        self.assertEqual(checkout_posts[0]["json"]["checkout_ui_mode"], "hosted")
        self.assertNotIn("elements_options_client[saved_payment_method][enable_save]", posted_bodies["stripe init"])
        self.assertEqual(posted_bodies["confirm"]["client_attribution_metadata[merchant_integration_additional_elements][0]"], "payment")
        self.assertEqual(posted_bodies["confirm"]["client_attribution_metadata[merchant_integration_additional_elements][1]"], "address")
        self.assertNotIn("client_attribution_metadata[merchant_integration_additional_elements][2]", posted_bodies["confirm"])

    def test_paypal_confirm_open_approves_and_polls_payment_page_redirect(self):
        checkout = FakeCheckoutSession(FakeCheckoutOkResponse())
        stripe_session = FakeStripeSession()
        cfg = {
            "paypal": {
                "link_mode": "stripe_redirect",
                "stop_after_pm_create": False,
                "checkout_only_long_url": False,
                "require_zero_due": True,
                "use_elements_session": True,
                "approve_missing_redirect": True,
                "redirect_poll_timeout_seconds": 1,
                "redirect_poll_interval_seconds": 0.2,
                "refresh_tax_region": True,
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = {
            "country": "US",
            "currency": "USD",
            "label": "United States (USD)",
            "address": {"country": "US"},
        }

        def fake_post_form(session, url, body, *, timeout, step):
            if step == "pm create":
                return FakePaymentMethodResponse()
            if step.startswith("confirm"):
                return FakeConfirmOpenResponse()
            return FakeZeroStripeInitResponse()

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=stripe_session):
                with patch.object(gen_pp_link, "_post_stripe_form", side_effect=fake_post_form):
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["redirect_source"], "post_approve_payment_page")
        self.assertIn("sa_nonce_POSTAPPROVE", result["url"])
        approve_calls = [call for call in checkout.calls if "checkout/approve" in call["url"]]
        self.assertEqual(len(approve_calls), 1)
        self.assertIsNone(approve_calls[0]["json"])
        self.assertTrue(approve_calls[0]["headers"].get("Referer", "").startswith("https://chatgpt.com/checkout/"))
        payment_page_gets = [call for call in stripe_session.get_calls if "/v1/payment_pages/" in call["url"]]
        self.assertEqual(len(payment_page_gets), 1)

    def test_paypal_approve_blocked_reports_specific_error(self):
        checkout = FakeCheckoutSession(FakeCheckoutOkResponse(), approve_response=FakeApproveBlockedResponse())
        stripe_session = FakeStripeSession()
        cfg = {
            "paypal": {
                "link_mode": "ba_redirect",
                "stop_after_pm_create": False,
                "checkout_only_long_url": False,
                "redirect_url_format": "any",
                "require_zero_due": True,
                "use_elements_session": True,
                "approve_missing_redirect": True,
                "redirect_poll_timeout_seconds": 0.01,
                "redirect_poll_interval_seconds": 0.01,
                "refresh_tax_region": True,
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = {
            "country": "US",
            "currency": "USD",
            "label": "United States (USD)",
            "address": {"country": "US"},
        }

        def fake_post_form(session, url, body, *, timeout, step):
            if step == "pm create":
                return FakePaymentMethodResponse()
            if step.startswith("confirm"):
                return FakeConfirmOpenResponse()
            return FakeZeroStripeInitResponse()

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=stripe_session):
                with patch.object(gen_pp_link, "_post_stripe_form", side_effect=fake_post_form):
                    with patch.object(stripe_session, "get", return_value=FakePaymentPageOpenResponse()) as get:
                        result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "checkout_approve_blocked")
        approve_calls = [call for call in checkout.calls if "checkout/approve" in call["url"]]
        self.assertEqual(len(approve_calls), 1)
        self.assertIsNone(approve_calls[0]["json"])
        self.assertTrue(approve_calls[0]["headers"].get("Referer", "").startswith("https://chatgpt.com/checkout/"))
        self.assertEqual(result["approve_result"]["result"], "blocked")
        self.assertIn("approve was blocked", result["error"])
        get.assert_called()

    def test_payment_page_poll_removes_unknown_get_param(self):
        class PollSession:
            def __init__(self):
                self.calls = []

            def get(self, url, params=None, timeout=None):
                self.calls.append(dict(params or {}))
                if len(self.calls) == 1:
                    return FakePollParamResponse()
                return FakePaymentPageRedirectResponse()

        session = PollSession()

        redirect_url, summary = gen_pp_link._poll_payment_page_redirect_url(
            session,
            cs_id="cs_live_TEST123",
            elements_session_id="elements_session_TEST",
            stripe_js_id="stripe-js-id",
            stripe_locale="auto",
            stripe_pk="pk_test",
            payment_method="paypal",
            redirect_format="any",
            timeout_seconds=1,
            poll_interval=0.01,
        )

        self.assertIn("sa_nonce_POSTAPPROVE", redirect_url)
        self.assertEqual(summary["status"], 200)
        self.assertIn("elements_options_client[stripe_js_locale]", session.calls[0])
        self.assertNotIn("elements_options_client[stripe_js_locale]", session.calls[1])

    def test_zero_due_paypal_confirm_decline_retries_without_promo_once(self):
        terminal_result = {
            "ok": False,
            "error": "Stripe confirm declined: status=402 reason=generic_decline",
            "error_code": "stripe_confirm_declined",
            "terminal": True,
            "zero_due_verified": True,
            "promo_campaign_id": "plus-1-month-free",
        }
        ok_result = {
            "ok": True,
            "url": "https://www.paypal.com/agreements/approve?ba_token=BA-123",
            "promo_campaign_id": "",
        }
        cfg = {
            "paypal": {
                "proxies": ["direct"],
                "max_checkout_retries": 3,
                "disable_promo_on_confirm_decline": True,
            }
        }

        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "_try_paypal_link", side_effect=[terminal_result, ok_result]) as try_paypal:
                result = gen_pp_link.generate_pp_link("eyJ.fake.token")

        self.assertTrue(result["ok"])
        self.assertTrue(result["promo_fallback_attempted"])
        self.assertEqual(try_paypal.call_count, 2)
        self.assertIsNone(try_paypal.call_args_list[0].kwargs.get("promo_campaign_id"))
        self.assertEqual(try_paypal.call_args_list[1].kwargs["promo_campaign_id"], "")

    def test_zero_due_paypal_confirm_decline_does_not_fallback_when_disabled(self):
        terminal_result = {
            "ok": False,
            "error": "Stripe confirm declined: status=402 reason=generic_decline",
            "error_code": "stripe_confirm_declined",
            "terminal": True,
            "zero_due_verified": True,
            "promo_campaign_id": "plus-1-month-free",
        }
        cfg = {
            "paypal": {
                "proxies": ["direct"],
                "max_checkout_retries": 3,
                "require_zero_due": True,
                "disable_promo_on_confirm_decline": False,
            }
        }

        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "_try_paypal_link", return_value=terminal_result) as try_paypal:
                result = gen_pp_link.generate_pp_link("eyJ.fake.token")

        self.assertEqual(result["error_code"], "stripe_confirm_declined")
        self.assertEqual(result["checkout_attempt"], 1)
        self.assertEqual(try_paypal.call_count, 1)
        self.assertNotIn("promo_fallback_attempted", result)

    def test_confirm_redirect_extraction_searches_nested_payload(self):
        payload = {
            "setup_intent": {"status": "requires_action", "next_action": {"type": "unknown"}},
            "nested": {
                "actions": [
                    {
                        "href": "https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_abc",
                    }
                ]
            },
        }

        self.assertEqual(
            gen_pp_link._find_payment_redirect_url(payload, "paypal"),
            "https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_abc",
        )

    def test_confirm_redirect_extraction_can_require_stripe_authorize_url(self):
        stripe_url = (
            "https://pm-redirects.stripe.com/authorize/acct_1HOrSwC6h1nxGoI3/"
            "sa_nonce_Ud4yVTu0JcHjhTVXTNTrX4IFmsQOdmp"
            "?useWebAuthSession=true&followRedirectsInSDK=true"
        )
        ba_url = "https://www.paypal.com/agreements/approve?ba_token=BA-123"
        hosted_url = "https://pay.openai.com/c/pay/cs_live_123#fidabc"
        payload = {
            "url": ba_url,
            "hosted": hosted_url,
            "nested": {"href": stripe_url},
        }

        self.assertEqual(
            gen_pp_link._find_payment_redirect_url(payload, "paypal", redirect_format="stripe_authorize"),
            stripe_url,
        )
        self.assertEqual(
            gen_pp_link._find_payment_redirect_url({"url": ba_url}, "paypal", redirect_format="stripe_authorize"),
            "",
        )
        self.assertEqual(
            gen_pp_link._find_payment_redirect_url({"url": hosted_url}, "paypal", redirect_format="stripe_authorize"),
            "",
        )
        self.assertEqual(
            gen_pp_link._find_payment_redirect_url({"url": ba_url}, "paypal", redirect_format="paypal_approve"),
            ba_url,
        )

    def test_paypal_redirect_format_defaults_to_stripe_authorize(self):
        self.assertEqual(gen_pp_link._paypal_redirect_format({}), "stripe_authorize")
        self.assertEqual(gen_pp_link._paypal_redirect_format({"redirect_url_format": "pm_redirect"}), "stripe_authorize")
        self.assertEqual(gen_pp_link._paypal_redirect_format({"redirect_url_format": "ba"}), "paypal_approve")
        self.assertEqual(gen_pp_link._paypal_redirect_format({"redirect_url_format": "any"}), "any")

    def test_missing_confirm_redirect_is_terminal(self):
        terminal_result = {
            "ok": False,
            "error": "Stripe confirm did not return PayPal redirect URL",
            "error_code": "stripe_confirm_missing_redirect",
            "terminal": True,
            "retryable": False,
            "confirm_summary": {
                "setup_intent": {
                    "status": "succeeded",
                    "next_action_type": "",
                }
            },
        }
        cfg = {"paypal": {"proxies": ["direct"], "max_checkout_retries": 3}}

        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "_try_paypal_link", return_value=terminal_result) as try_paypal:
                result = gen_pp_link.generate_pp_link("eyJ.fake.token")

        self.assertEqual(result["error_code"], "stripe_confirm_missing_redirect")
        self.assertEqual(result["checkout_attempt"], 1)
        self.assertEqual(try_paypal.call_count, 1)
        self.assertEqual(result["confirm_summary"]["setup_intent"]["status"], "succeeded")

    def test_explicit_proxy_keeps_configured_stage_proxies_by_default(self):
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
        self.assertFalse(try_paypal.call_args.kwargs["force_proxy"])
        self.assertEqual(gen_pp_link._stage_proxy(cfg["paypal"], "confirm", proxy, force_fallback=False), "")

    def test_explicit_proxy_can_force_all_stages_when_configured(self):
        terminal_result = {
            "ok": False,
            "error": "terminal",
            "error_code": "terminal",
            "terminal": True,
        }
        cfg = {
            "paypal": {
                "proxies": ["direct"],
                "explicit_proxy_overrides_stage_proxies": True,
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
        self.assertEqual(gen_pp_link._stage_proxy(cfg["paypal"], "confirm", proxy, force_fallback=True), proxy)

    def test_configured_japan_billing_region(self):
        regions = gen_pp_link._billing_regions({"billing_regions": ["JP"]})

        self.assertEqual(regions[0]["country"], "JP")
        self.assertEqual(regions[0]["currency"], "JPY")
        self.assertEqual(regions[0]["browser_timezone"], "Asia/Tokyo")
        self.assertEqual(regions[0]["address"]["country"], "JP")

    def test_configured_germany_billing_region_uses_eur(self):
        regions = gen_pp_link._billing_regions({"billing_regions": ["DE"]})

        self.assertEqual(regions[0]["country"], "DE")
        self.assertEqual(regions[0]["currency"], "EUR")
        self.assertEqual(regions[0]["browser_locale"], "de-DE")
        self.assertEqual(regions[0]["browser_timezone"], "Europe/Berlin")
        self.assertEqual(regions[0]["address"]["country"], "DE")

    def test_configured_france_billing_region_uses_eur(self):
        regions = gen_pp_link._billing_regions({"billing_regions": ["FR"]})

        self.assertEqual(regions[0]["country"], "FR")
        self.assertEqual(regions[0]["currency"], "EUR")
        self.assertEqual(regions[0]["browser_locale"], "fr-FR")
        self.assertEqual(regions[0]["browser_timezone"], "Europe/Paris")
        self.assertEqual(regions[0]["address"]["country"], "FR")

    def test_configured_united_kingdom_billing_region_uses_gbp(self):
        regions = gen_pp_link._billing_regions({"billing_regions": ["GB"]})

        self.assertEqual(regions[0]["country"], "GB")
        self.assertEqual(regions[0]["currency"], "GBP")
        self.assertEqual(regions[0]["browser_locale"], "en-GB")
        self.assertEqual(regions[0]["browser_timezone"], "Europe/London")
        self.assertEqual(regions[0]["address"]["country"], "GB")

    def test_configured_india_billing_region_uses_inr(self):
        regions = gen_pp_link._billing_regions({"billing_regions": ["IN"]})

        self.assertEqual(regions[0]["country"], "IN")
        self.assertEqual(regions[0]["currency"], "INR")
        self.assertEqual(regions[0]["browser_locale"], "en-IN")
        self.assertEqual(regions[0]["browser_timezone"], "Asia/Kolkata")
        self.assertEqual(regions[0]["address"]["country"], "IN")

    def test_configured_brazil_billing_region_uses_brl(self):
        regions = gen_pp_link._billing_regions({"billing_regions": ["BR"]})

        self.assertEqual(regions[0]["country"], "BR")
        self.assertEqual(regions[0]["currency"], "BRL")
        self.assertEqual(regions[0]["browser_locale"], "pt-BR")
        self.assertEqual(regions[0]["browser_timezone"], "America/Sao_Paulo")
        self.assertEqual(regions[0]["address"]["country"], "BR")

    def test_configured_us_billing_region_matches_original_flow(self):
        regions = gen_pp_link._billing_regions({"billing_regions": ["US"]})

        self.assertEqual(regions[0]["country"], "US")
        self.assertEqual(regions[0]["currency"], "USD")
        self.assertEqual(regions[0]["browser_timezone"], "Asia/Shanghai")
        self.assertEqual(regions[0]["address"]["country"], "US")

    def test_configured_australia_billing_region_uses_aud(self):
        regions = gen_pp_link._billing_regions({"billing_regions": ["AU"]})

        self.assertEqual(regions[0]["country"], "AU")
        self.assertEqual(regions[0]["currency"], "AUD")
        self.assertEqual(regions[0]["browser_locale"], "en-AU")
        self.assertEqual(regions[0]["browser_timezone"], "Australia/Sydney")
        self.assertEqual(regions[0]["address"]["country"], "AU")
        self.assertEqual(regions[0]["address"]["state"], "NSW")

    def test_gopay_default_billing_region_is_indonesia(self):
        cfg = {"paypal": {"billing_regions": ["US"]}}
        payment_cfg = gen_pp_link._payment_cfg(cfg, "gopay")
        regions = gen_pp_link._billing_regions(payment_cfg)

        self.assertEqual(regions[0]["country"], "ID")
        self.assertEqual(regions[0]["currency"], "IDR")
        self.assertEqual(regions[0]["browser_timezone"], "Asia/Jakarta")

    def test_upi_default_billing_region_is_india_hosted_long_link(self):
        cfg = {"paypal": {"billing_regions": ["US"], "link_mode": "stripe_redirect", "checkout_ui_mode": "custom"}}
        payment_cfg = gen_pp_link._payment_cfg(cfg, "upi")
        regions = gen_pp_link._billing_regions(payment_cfg)

        self.assertEqual(regions[0]["country"], "IN")
        self.assertEqual(regions[0]["currency"], "INR")
        self.assertEqual(gen_pp_link._checkout_ui_mode(payment_cfg), "hosted")
        self.assertEqual(gen_pp_link._payment_link_mode(payment_cfg, "upi"), "chatgpt_checkout")

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

    def test_generate_payment_link_passes_upi_method_and_india_region(self):
        ok_result = {"ok": True, "url": "https://pay.openai.com/c/pay/cs_live_UPI"}
        cfg = {
            "paypal": {"proxies": ["direct"], "billing_regions": ["DE"], "max_checkout_retries": 3},
            "upi": {"billing_regions": ["IN"], "max_checkout_retries": 1},
        }

        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "_try_paypal_link", return_value=ok_result) as try_paypal:
                result = gen_pp_link.generate_payment_link("eyJ.fake.token", payment_method="upi")

        self.assertTrue(result["ok"])
        self.assertEqual(result["payment_method"], "upi")
        self.assertEqual(try_paypal.call_args.kwargs["payment_method"], "upi")
        self.assertEqual(try_paypal.call_args.args[2]["country"], "IN")

    def test_upi_hosted_checkout_uses_stripe_init_long_url(self):
        checkout = FakeCheckoutSession(FakeCheckoutHostedMissingProviderResponse())
        cfg = {
            "paypal": {
                "billing_regions": ["DE"],
                "stage_proxies": {"checkout": "direct"},
            },
            "upi": {
                "billing_regions": ["IN"],
                "checkout_ui_mode": "hosted",
                "link_mode": "chatgpt_checkout",
                "require_zero_due": True,
                "refresh_tax_region": False,
            },
        }
        region = gen_pp_link._billing_regions(gen_pp_link._payment_cfg(cfg, "upi"))[0]

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()) as new_session:
                with patch.object(
                    gen_pp_link,
                    "_post_stripe_form",
                    return_value=FakeStripeInitHostedResponse(
                        "https://checkout.stripe.com/c/pay/cs_live_UPI123#fidkdWxOYHwnPyd1blppbHNg",
                        currency="inr",
                        payment_method_types=["card", "upi"],
                    ),
                ) as post_form:
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="upi")

        self.assertTrue(result["ok"])
        self.assertEqual(result["payment_method"], "upi")
        self.assertEqual(result["billing_country"], "IN")
        self.assertEqual(result["currency"], "INR")
        self.assertTrue(result["has_upi"])
        self.assertEqual(result["link_type"], "hosted_long_url")
        self.assertEqual(result["url"], "https://pay.openai.com/c/pay/cs_live_UPI123#fidkdWxOYHwnPyd1blppbHNg")
        called_urls = [call.args[1] for call in post_form.call_args_list]
        self.assertEqual(called_urls, ["https://api.stripe.com/v1/payment_pages/cs_live_FAKEHOSTED123/init"])
        self.assertGreaterEqual(new_session.call_count, 1)

    def test_paypal_generation_type_hosted_patches_long_link_flow(self):
        cfg = {
            "paypal": {
                "link_generation_type": "hosted_long_url",
                "checkout_ui_mode": "custom",
                "link_mode": "stripe_redirect",
                "resolve_ba_redirect": True,
                "require_ba_token": True,
                "require_zero_due": False,
            }
        }

        payment_cfg = gen_pp_link._payment_cfg(cfg, "paypal")

        self.assertEqual(payment_cfg["link_generation_type"], "hosted_long_url")
        self.assertEqual(gen_pp_link._checkout_ui_mode(payment_cfg), "hosted")
        self.assertEqual(gen_pp_link._payment_link_mode(payment_cfg, "paypal"), "chatgpt_checkout")
        self.assertFalse(payment_cfg["resolve_ba_redirect"])
        self.assertFalse(payment_cfg["require_ba_token"])

    def test_paypal_generation_type_direct_patches_ba_approve_flow(self):
        cfg = {
            "paypal": {
                "link_generation_type": "paypal_direct",
                "checkout_ui_mode": "hosted",
                "link_mode": "chatgpt_checkout",
                "confirm_style": "inline_payment_method_data",
                "require_zero_due": True,
            }
        }

        payment_cfg = gen_pp_link._payment_cfg(cfg, "paypal")

        self.assertEqual(payment_cfg["link_generation_type"], "paypal_direct")
        self.assertEqual(gen_pp_link._checkout_ui_mode(payment_cfg), "custom")
        self.assertEqual(gen_pp_link._payment_link_mode(payment_cfg, "paypal"), "stripe_redirect")
        self.assertEqual(gen_pp_link._paypal_confirm_style(payment_cfg), "payment_method_id")
        self.assertTrue(payment_cfg["resolve_ba_redirect"])
        self.assertTrue(payment_cfg["require_ba_token"])
        self.assertFalse(payment_cfg["require_zero_due"])

    def test_paypal_generation_type_direct_zero_due_requires_zero_trial(self):
        cfg = {
            "paypal": {
                "link_generation_type": "paypal_direct_zero_due",
                "checkout_ui_mode": "hosted",
                "link_mode": "chatgpt_checkout",
                "confirm_style": "inline_payment_method_data",
                "require_zero_due": False,
            }
        }

        payment_cfg = gen_pp_link._payment_cfg(cfg, "paypal")

        self.assertEqual(payment_cfg["link_generation_type"], "paypal_direct_zero_due")
        self.assertEqual(gen_pp_link._checkout_ui_mode(payment_cfg), "custom")
        self.assertEqual(gen_pp_link._payment_link_mode(payment_cfg, "paypal"), "stripe_redirect")
        self.assertEqual(gen_pp_link._paypal_confirm_style(payment_cfg), "payment_method_id")
        self.assertTrue(payment_cfg["resolve_ba_redirect"])
        self.assertTrue(payment_cfg["require_ba_token"])
        self.assertTrue(payment_cfg["require_zero_due"])


    def test_paypal_generation_type_gpt_pp_core_patches_protocol_flow(self):
        cfg = {
            "paypal": {
                "link_generation_type": "gpt_pp_core",
                "checkout_ui_mode": "custom",
                "link_mode": "chatgpt_checkout",
                "resolve_ba_redirect": True,
                "require_ba_token": True,
            }
        }

        payment_cfg = gen_pp_link._payment_cfg(cfg, "paypal")

        self.assertEqual(payment_cfg["link_generation_type"], "gpt_pp_core")
        self.assertEqual(gen_pp_link._payment_link_mode(payment_cfg, "paypal"), "stripe_redirect")
        self.assertEqual(payment_cfg["redirect_url_format"], "stripe_authorize")
        self.assertFalse(payment_cfg["resolve_ba_redirect"])
        self.assertFalse(payment_cfg["require_ba_token"])
        self.assertFalse(payment_cfg["approve_missing_redirect"])

    def test_generate_payment_link_routes_gpt_pp_core_engine(self):
        ok_result = {
            "ok": True,
            "url": "https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_test",
            "source": "gpt_pp_core",
            "payment_method": "paypal",
        }
        cfg = {
            "paypal": {
                "link_generation_type": "gpt_pp_core",
                "billing_regions": ["DE"],
                "max_checkout_retries": 1,
            }
        }

        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "_try_gpt_pp_core_link", return_value=ok_result) as try_core:
                result = gen_pp_link.generate_payment_link("eyJ.fake.token", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "gpt_pp_core")
        self.assertEqual(result["checkout_attempt"], 1)
        try_core.assert_called_once()

    def test_paypal_direct_generation_resolves_stripe_redirect_to_ba_url(self):
        checkout = FakeCheckoutSession(FakeCheckoutOkResponse())
        stripe_session = FakeStripeSession()
        ba_url = "https://www.paypal.com/agreements/approve?ba_token=BA-123"
        cfg = {
            "paypal": {
                "link_generation_type": "paypal_direct",
                "use_elements_session": True,
                "refresh_tax_region": True,
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = gen_pp_link._billing_regions({"billing_regions": ["DE"]})[0]

        def fake_post_form(session, url, body, *, timeout, step):
            if step == "pm create":
                return FakePaymentMethodResponse()
            if step.startswith("confirm"):
                return FakeConfirmRedirectResponse()
            return FakeZeroStripeInitResponse()

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=stripe_session):
                with patch.object(gen_pp_link, "_post_stripe_form", side_effect=fake_post_form):
                    with patch.object(gen_pp_link, "_resolve_paypal_approve_url", return_value=(ba_url, {"ok": True, "has_ba_token": True})):
                        result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], ba_url)
        self.assertTrue(result["ba_resolved"])
        self.assertTrue(result["ba_token_present"])
        self.assertIn("pm-redirects.stripe.com/authorize", result["stripe_redirect_url"])
        self.assertEqual(result["link_mode"], "stripe_redirect")
        self.assertEqual(result["checkout_ui_mode"], "custom")

    def test_paypal_direct_zero_due_rejects_non_zero_checkout(self):
        checkout = FakeCheckoutSession(FakeCheckoutOkResponse())
        cfg = {
            "paypal": {
                "link_generation_type": "paypal_direct_zero_due",
                "refresh_tax_region": False,
                "stage_proxies": {"checkout": "direct"},
            }
        }
        region = gen_pp_link._billing_regions({"billing_regions": ["DE"]})[0]

        with patch.object(gen_pp_link, "_build_chatgpt_session", return_value=checkout):
            with patch.object(gen_pp_link, "_new_session", return_value=FakeStripeSession()):
                with patch.object(gen_pp_link, "_post_stripe_form", return_value=FakeNonZeroStripeInitResponse()):
                    result = gen_pp_link._try_paypal_link("eyJ.fake.token", cfg, region, "", payment_method="paypal")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "checkout_not_zero_due")
        self.assertFalse(result["zero_due_verified"])
        self.assertEqual(result["amount_due"], 2300)

    def test_paypal_direct_zero_due_does_not_fallback_to_hosted_when_approve_blocked(self):
        blocked = {
            "ok": False,
            "error": "ChatGPT checkout approve was blocked after Stripe confirm returned no redirect",
            "error_code": "checkout_approve_blocked",
            "terminal": True,
            "zero_due_verified": True,
        }
        cfg = {"paypal": {"link_generation_type": "paypal_direct_zero_due", "max_checkout_retries": 1}}

        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "_try_paypal_link", return_value=blocked) as try_link:
                result = gen_pp_link.generate_payment_link("eyJ.fake.token", payment_method="paypal")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "checkout_approve_blocked")
        self.assertTrue(result["zero_due_verified"])
        self.assertEqual(try_link.call_count, 1)

    def test_generate_payment_link_can_override_paypal_generation_type(self):
        ok_result = {
            "ok": True,
            "url": "https://www.paypal.com/agreements/approve?ba_token=BA-123",
            "payment_method": "paypal",
        }
        cfg = {
            "paypal": {
                "link_generation_type": "hosted_long_url",
                "billing_regions": ["DE"],
            }
        }

        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "_try_paypal_link", return_value=ok_result) as try_link:
                result = gen_pp_link.generate_payment_link(
                    "eyJ.fake.token",
                    payment_method="paypal",
                    paypal_generation_type="paypal_direct_zero_due",
                )

        self.assertTrue(result["ok"])
        payment_cfg = try_link.call_args.args[1]["paypal"]
        self.assertEqual(payment_cfg["link_generation_type"], "paypal_direct_zero_due")

    def test_default_paypal_config_matches_committed_chatgpt_checkout_us(self):
        cfg = {
            "reference_confirm_mode": False,
            "link_mode": "chatgpt_checkout",
            "checkout_ui_mode": "hosted",
            "redirect_url_format": "any",
            "checkout_only_long_url": False,
            "stop_after_pm_create": False,
            "use_elements_session": True,
            "refresh_tax_region": True,
            "approve_missing_redirect": False,
            "resolve_ba_redirect": False,
            "require_ba_token": False,
            "billing_regions": ["US"],
            "confirm_style": "inline_payment_method_data",
        }

        self.assertFalse(gen_pp_link._reference_confirm_mode(cfg, "paypal"))
        self.assertEqual(gen_pp_link._payment_link_mode(cfg, "paypal"), "chatgpt_checkout")
        self.assertEqual(gen_pp_link._checkout_ui_mode(cfg), "hosted")
        self.assertEqual(gen_pp_link._paypal_redirect_format(cfg), "any")
        self.assertFalse(gen_pp_link._checkout_only_long_url(cfg, "paypal"))
        self.assertFalse(gen_pp_link._stop_after_pm_create(cfg, "paypal"))
        self.assertEqual(gen_pp_link._paypal_confirm_style(cfg), "inline_payment_method_data")
        region = gen_pp_link._billing_regions(cfg)[0]
        self.assertEqual(region["country"], "US")
        self.assertEqual(region["currency"], "USD")


if __name__ == "__main__":
    unittest.main()
