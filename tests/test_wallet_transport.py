import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool.checkout_contract import CheckoutRequestContract
from sms_tool.wallet_provider import WalletTransportRequest
from sms_tool.wallet_transport import ChatGPTStripeWalletTransport


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, text="", url=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text
        self.url = url

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.responses.pop(0) if self.responses else FakeResponse()

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.responses.pop(0) if self.responses else FakeResponse(url=url)


def request(stage, *, method="gopay", payload=None, redirect_url=""):
    return WalletTransportRequest(
        stage=stage,
        method=method,
        contract=CheckoutRequestContract.for_payment_method(method),
        flow_id="flow-fixture",
        checkout_session_id="cs_fixture",
        processor_entity="openai_ie",
        access_token="secret-token",
        publishable_key="pk_live_fixture",
        payload=payload or {"key": "value"},
        auth_context={"cookie_header": "session=secret"},
        transport_context={
            "checkout_proxy": "http://checkout.test:80",
            "provider_proxy": "http://provider.test:80",
            "approve_proxy": "http://approve.test:80",
            "redirect_proxy": "http://redirect.test:80",
        },
        redirect_url=redirect_url,
    )


class WalletTransportTests(unittest.TestCase):
    def test_checkout_and_approve_use_chatgpt_contract_and_stage_proxy(self):
        transport = ChatGPTStripeWalletTransport()
        responses = [FakeResponse(payload={"checkout_session_id": "cs_fixture"}), FakeResponse(payload={"result": "approved"})]
        calls = []

        def checkout_post(url, body, access_token, cookie, proxy, timeout, extra_headers=None):
            calls.append((url, body, access_token, cookie, proxy, extra_headers))
            return responses.pop(0)

        with patch("sms_tool.gen_pp_link._checkout_post", side_effect=checkout_post):
            transport.create_checkout(request("checkout"))
            transport.approve_checkout(request("approve", payload={"checkout_session_id": "cs_fixture"}))

        self.assertEqual(calls[0][4], "http://checkout.test:80")
        self.assertEqual(calls[0][5]["x-openai-target-path"], "/backend-api/payments/checkout")
        self.assertEqual(calls[1][4], "http://approve.test:80")
        self.assertEqual(calls[1][5]["x-openai-target-path"], "/backend-api/payments/checkout/approve")

    def test_stripe_stages_use_form_contract_and_provider_proxy(self):
        session = FakeSession([
            FakeResponse(payload={"payment_method_types": ["gopay"]}),
            FakeResponse(payload={"id": "pm_fixture"}),
            FakeResponse(payload={"status": "requires_action"}),
            FakeResponse(payload={"payment_intent": {}}),
        ])
        transport = ChatGPTStripeWalletTransport()
        with patch("sms_tool.gen_pp_link._new_session", return_value=session) as new_session:
            transport.stripe_init(request("stripe_init"))
            transport.create_payment_method(request("payment_method"))
            transport.confirm_payment(request("confirm"))
            transport.poll_payment(request("poll"))

        new_session.assert_called_once_with("http://provider.test:80")
        self.assertEqual(session.calls[0][1], "https://api.stripe.com/v1/payment_pages/cs_fixture/init")
        self.assertEqual(session.calls[1][1], "https://api.stripe.com/v1/payment_methods")
        self.assertTrue(session.calls[2][1].endswith("/cs_fixture/confirm"))
        self.assertIn("params", session.calls[3][2])
        self.assertEqual(session.calls[0][2]["headers"]["Content-Type"], "application/x-www-form-urlencoded")

    def test_redirect_chain_is_manual_and_stops_on_provider_host(self):
        session = FakeSession([
            FakeResponse(302, headers={"Location": "https://app.midtrans.com/snap/v4/redirection/fixture"}),
            FakeResponse(200, url="https://app.midtrans.com/snap/v4/redirection/fixture"),
        ])
        transport = ChatGPTStripeWalletTransport()
        with patch("sms_tool.gen_pp_link._new_session", return_value=session):
            result = transport.follow_redirect(request(
                "follow_redirect",
                redirect_url="https://pm-redirects.stripe.com/authorize/fixture",
            ))
        self.assertEqual(result["final_url"], "https://app.midtrans.com/snap/v4/redirection/fixture")
        self.assertTrue(all(call[2]["allow_redirects"] is False for call in session.calls))

    def test_redirect_chain_rejects_off_allowlist_location(self):
        session = FakeSession([
            FakeResponse(302, headers={"Location": "https://attacker.example/collect"}),
        ])
        transport = ChatGPTStripeWalletTransport()
        with patch("sms_tool.gen_pp_link._new_session", return_value=session):
            with self.assertRaisesRegex(Exception, "not allowed"):
                transport.follow_redirect(request(
                    "follow_redirect",
                    redirect_url="https://pm-redirects.stripe.com/authorize/fixture",
                ))


if __name__ == "__main__":
    unittest.main()
