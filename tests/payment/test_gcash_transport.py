import unittest
from unittest.mock import patch

from sms_tool.checkout_contract import CheckoutRequestContract
from sms_tool.gcash_provider import GCashProviderError, GCashTransportRequest
from sms_tool.gcash_transport import ChatGPTGCashTransport


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FailingSession:
    def get(self, url, **kwargs):
        raise RuntimeError("proxy=http://user:password@host.test:8080 access_token=fixture-secret")


def request(stage, payload=None):
    return GCashTransportRequest(
        stage=stage,
        contract=CheckoutRequestContract.for_payment_method("gcash"),
        checkout_session_id="cs_test_gcash_transport",
        processor_entity="openai_ie",
        access_token="fixture-access-token",
        payload=payload or {},
        auth_context={"account_id": "account-fixture", "device_id": "device-fixture"},
        transport_context={"checkout_proxy": "host.test:8080:user:pass"},
    )


class GCashTransportTests(unittest.TestCase):
    def test_transport_diagnostic_is_sanitized(self):
        with patch("sms_tool.gcash_transport._new_http_session", return_value=FailingSession()):
            with self.assertRaises(GCashProviderError) as caught:
                ChatGPTGCashTransport().resolve_checkout(request("resolve"))

        message = str(caught.exception)
        self.assertNotIn("user:password", message)
        self.assertNotIn("fixture-secret", message)
        self.assertIn("[REDACTED]", message)

    def test_custom_capability_uses_stripe_elements_contract(self):
        session = FakeSession(FakeResponse(payload={"custom_payment_method_data": []}))
        base = request("custom_capability")
        capability_request = GCashTransportRequest(**{
            **base.__dict__,
            "payload": {
                "checkout": {
                    "customer_session_client_secret": "customer-secret-fixture",
                    "payment_method_types": ["card", "custom_payment_method"],
                },
                "custom_payment_method_type_id": "cpmt_fixture_gcash",
            },
        })
        with patch("sms_tool.gcash_transport._new_http_session", return_value=session):
            ChatGPTGCashTransport().probe_custom_payment(capability_request)

        url, kwargs = session.calls[0]
        self.assertTrue(url.endswith("/v1/elements/sessions"))
        self.assertEqual(kwargs["params"]["custom_payment_methods[0]"], "cpmt_fixture_gcash")
        self.assertEqual(kwargs["params"]["deferred_intent[amount]"], "0")

    def test_stage_proxy_keeps_checkout_chain_and_uses_promotion_override(self):
        transport = ChatGPTGCashTransport()
        base = request("resolve")
        context = {
            "checkout_proxy": "checkout.test:8080:user:pass",
            "provider_proxy": "checkout.test:8080:user:pass",
            "promotion_proxy": "promotion.test:8080:user:pass",
        }
        resolve_request = GCashTransportRequest(**{**base.__dict__, "transport_context": context})
        update_request = GCashTransportRequest(**{**base.__dict__, "stage": "update", "transport_context": context})

        self.assertIn("checkout.test:8080", transport._stage_proxy(resolve_request))
        self.assertIn("promotion.test:8080", transport._stage_proxy(update_request))

    def test_confirm_posts_chatgpt_custom_payment_contract(self):
        calls = []

        def fake_post(url, body, token, cookie, proxy, timeout, extra_headers=None):
            calls.append((url, body, token, proxy, extra_headers))
            return FakeResponse(payload={"status": "confirmed"})

        body = {
            "checkout_session_id": "cs_test_gcash_transport",
            "type": "custom_payment_method",
            "selected_payment_method_type": "cpmt_fixture_gcash",
        }
        with patch("sms_tool.gen_pp_link._checkout_post", side_effect=fake_post):
            result = ChatGPTGCashTransport().confirm_custom_payment(request("confirm", body))

        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(calls[0][0].endswith("/backend-api/payments/checkout/confirm"))
        self.assertEqual(calls[0][1], body)
        self.assertEqual(calls[0][4]["ChatGPT-Account-Id"], "account-fixture")
        self.assertIn("user:pass@host.test:8080", calls[0][3])

    def test_resolve_uses_checkout_identity_headers(self):
        session = FakeSession(FakeResponse(payload={"payment_status": "unpaid"}))
        with patch("sms_tool.gcash_transport._new_http_session", return_value=session):
            result = ChatGPTGCashTransport().resolve_checkout(request("resolve"))

        self.assertEqual(result["payment_status"], "unpaid")
        _, kwargs = session.calls[0]
        self.assertEqual(kwargs["headers"]["ChatGPT-Account-Id"], "account-fixture")
        self.assertEqual(kwargs["headers"]["OAI-Device-Id"], "device-fixture")

    def test_confirm_409_has_explicit_not_confirmed_classification(self):
        with patch("sms_tool.gen_pp_link._checkout_post", return_value=FakeResponse(409, {"detail": "conflict"})):
            with self.assertRaises(GCashProviderError) as caught:
                ChatGPTGCashTransport().confirm_custom_payment(request("confirm"))

        self.assertEqual(caught.exception.error_code, "gcash_checkout_not_confirmed")
        self.assertEqual(caught.exception.status, "failed")
        self.assertFalse(caught.exception.retryable)

    def test_confirm_unsupported_custom_method_is_terminal_contract_failure(self):
        with patch(
            "sms_tool.gen_pp_link._checkout_post",
            return_value=FakeResponse(400, {"detail": "Unsupported custom payment method type for this checkout session"}),
        ):
            with self.assertRaises(GCashProviderError) as caught:
                ChatGPTGCashTransport().confirm_custom_payment(request("confirm"))

        self.assertEqual(caught.exception.error_code, "gcash_custom_method_unsupported")
        self.assertEqual(caught.exception.status, "failed")
        self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
