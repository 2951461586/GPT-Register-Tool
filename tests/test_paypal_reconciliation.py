import json
import unittest
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from sms_tool.paypal_reconciliation import (
    PaymentOutcome,
    ReconciliationClassification,
    RemoteStatus,
    ReturnStage,
    ReturnURLValidationError,
    normalize_return_state,
    reconcile_paypal_return,
)


@dataclass
class FakeResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""


class FakeTransport:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, timeout: float, allow_redirects: bool) -> Any:
        self.calls.append(
            {
                "url": url,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PayPalReconciliationTests(unittest.TestCase):
    def test_normalizes_state_without_retaining_url_secrets(self):
        verify_url = "https://chatgpt.com/checkout/verify?checkout_session_id=cs_live_VERIFYSECRET"
        pay_url = (
            "https://pay.openai.com/c/pay/cs_live_PATHSECRET"
            "?redirect_status=succeeded"
            "&setup_intent=seti_PRIVATE"
            "&setup_intent_client_secret=seti_PRIVATE_secret_VALUE"
            f"&success_return_url={quote(verify_url, safe='')}"
        )

        state = normalize_return_state(pay_url)

        self.assertEqual(state.stage, ReturnStage.OPENAI_PAY)
        self.assertEqual(state.redirect_status, RemoteStatus.SUCCEEDED)
        self.assertTrue(state.has_setup_intent)
        self.assertTrue(state.has_client_secret)
        self.assertTrue(state.has_success_return_url)
        self.assertEqual(state.success_return_stage, ReturnStage.CHECKOUT_VERIFY)
        serialized = json.dumps(state.to_dict(), sort_keys=True)
        self.assertNotIn("PATHSECRET", serialized)
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("VERIFYSECRET", serialized)

    def test_reconciles_pm_redirect_through_pay_and_checkout_verify(self):
        verify_url = "https://chatgpt.com/checkout/verify?checkout_session_id=cs_live_VERIFYSECRET"
        pay_url = (
            "https://pay.openai.com/c/pay/cs_live_PAYSECRET"
            "?redirect_status=succeeded"
            "&setup_intent=seti_PRIVATE"
            "&setup_intent_client_secret=seti_PRIVATE_secret_VALUE"
            f"&success_return_url={quote(verify_url, safe='')}"
        )
        start_url = (
            "https://pm-redirects.stripe.com/return?status=success"
            "&billing_agreement=BA-PRIVATE"
        )
        transport = FakeTransport(
            FakeResponse(302, {"Location": pay_url}),
            FakeResponse(302, {"location": verify_url}),
            FakeResponse(200, text="Your payment was successful"),
        )

        result = reconcile_paypal_return(start_url, transport=transport)

        self.assertEqual(result.classification, ReconciliationClassification.CONCLUSIVE)
        self.assertEqual(result.outcome, PaymentOutcome.SUCCEEDED)
        self.assertTrue(result.ok)
        self.assertFalse(result.retryable)
        self.assertEqual(result.final_stage, ReturnStage.CHECKOUT_VERIFY)
        self.assertEqual(result.redirect_status, RemoteStatus.SUCCEEDED)
        self.assertEqual(result.stripe_return_status, RemoteStatus.SUCCEEDED)
        self.assertTrue(result.observed_setup_intent)
        self.assertTrue(result.observed_client_secret)
        self.assertEqual(len(result.hops), 3)
        self.assertTrue(all(call["allow_redirects"] is False for call in transport.calls))

        persisted = json.dumps(result.to_dict(), sort_keys=True)
        for secret in ("BA-PRIVATE", "PAYSECRET", "VERIFYSECRET", "seti_PRIVATE"):
            self.assertNotIn(secret, persisted)
        self.assertNotIn("https://", persisted)

    def test_nested_job_result_is_normalized_and_explicit_failure_is_conclusive(self):
        source = {
            "redirect_status": "failed",
            "data": {
                "billing": {
                    "authorize": {
                        "returnURL": {
                            "href": "https://pm-redirects.stripe.com/return?status=success&token=PRIVATE"
                        }
                    }
                }
            },
        }
        transport = FakeTransport()

        result = reconcile_paypal_return(source, transport=transport)

        self.assertEqual(result.classification, ReconciliationClassification.CONCLUSIVE)
        self.assertEqual(result.outcome, PaymentOutcome.FAILED)
        self.assertEqual(result.error_code, "remote_payment_failed")
        self.assertFalse(result.retryable)
        self.assertEqual(transport.calls, [])

    def test_cancelled_query_is_a_conclusive_cancelled_outcome(self):
        transport = FakeTransport()

        result = reconcile_paypal_return(
            "https://pay.openai.com/c/pay/cs_live_PRIVATE?redirect_status=cancelled",
            transport=transport,
        )

        self.assertEqual(result.classification, ReconciliationClassification.CONCLUSIVE)
        self.assertEqual(result.outcome, PaymentOutcome.CANCELLED)
        self.assertEqual(result.error_code, "remote_payment_cancelled")
        self.assertEqual(transport.calls, [])

    def test_final_redirect_failure_is_observed_even_when_return_url_is_preferred(self):
        source = {
            "return_url": "https://pm-redirects.stripe.com/return?status=success",
            "final_redirect_url": (
                "https://pay.openai.com/c/pay/cs_live_PRIVATE?redirect_status=failed"
            ),
        }
        transport = FakeTransport()

        result = reconcile_paypal_return(source, transport=transport)

        self.assertEqual(result.classification, ReconciliationClassification.CONCLUSIVE)
        self.assertEqual(result.outcome, PaymentOutcome.FAILED)
        self.assertEqual(transport.calls, [])

    def test_rejects_non_allowlisted_and_unsupported_urls(self):
        invalid_urls = (
            "http://pay.openai.com/c/pay/cs_live_PRIVATE",
            "https://pay.openai.com.evil.example/c/pay/cs_live_PRIVATE",
            "https://user:pass@pay.openai.com/c/pay/cs_live_PRIVATE",
            "https://pay.openai.com./c/pay/cs_live_PRIVATE",
            "https://pm-redirects.stripe.com/return-unrelated?status=success",
            "https://chatgpt.com/backend-api/accounts/check",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                transport = FakeTransport()
                result = reconcile_paypal_return(url, transport=transport)
                self.assertEqual(result.classification, ReconciliationClassification.FAILED)
                self.assertEqual(result.outcome, PaymentOutcome.UNKNOWN)
                self.assertFalse(result.retryable)
                self.assertEqual(transport.calls, [])

    def test_rejects_off_allowlist_redirect_without_requesting_it(self):
        start = "https://pm-redirects.stripe.com/return?status=success"
        transport = FakeTransport(
            FakeResponse(
                302,
                {"Location": "https://attacker.example/collect?token=PRIVATE"},
            )
        )

        result = reconcile_paypal_return(start, transport=transport)

        self.assertEqual(result.classification, ReconciliationClassification.FAILED)
        self.assertEqual(result.error_stage, "redirect_validation")
        self.assertEqual(result.error_code, "host_not_allowed")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("PRIVATE", json.dumps(result.to_dict()))

    def test_detects_redirect_loop_using_secret_free_hop_output(self):
        first = "https://pay.openai.com/c/pay/cs_live_FIRST?redirect_status=pending"
        second = "https://pay.openai.com/c/pay/cs_live_SECOND?redirect_status=pending"
        transport = FakeTransport(
            FakeResponse(302, {"Location": second}),
            FakeResponse(302, {"Location": first}),
        )

        result = reconcile_paypal_return(first, transport=transport)

        self.assertEqual(result.classification, ReconciliationClassification.FAILED)
        self.assertEqual(result.error_code, "redirect_loop")
        self.assertFalse(result.retryable)
        serialized = json.dumps(result.to_dict())
        self.assertNotIn("FIRST", serialized)
        self.assertNotIn("SECOND", serialized)

    def test_max_hop_limit_returns_unknown_without_leaking_urls(self):
        first = "https://pay.openai.com/c/pay/cs_live_FIRST?redirect_status=pending"
        second = "https://pay.openai.com/c/pay/cs_live_SECOND?redirect_status=pending"
        third = "https://pay.openai.com/c/pay/cs_live_THIRD?redirect_status=pending"
        transport = FakeTransport(
            FakeResponse(302, {"Location": second}),
            FakeResponse(302, {"Location": third}),
        )

        result = reconcile_paypal_return(first, transport=transport, max_hops=2)

        self.assertEqual(result.classification, ReconciliationClassification.UNKNOWN)
        self.assertEqual(result.outcome, PaymentOutcome.UNKNOWN)
        self.assertEqual(result.error_code, "max_hops_exceeded")
        self.assertEqual(len(result.hops), 2)

    def test_transport_error_is_unknown_retryable_and_scrubbed(self):
        transport = FakeTransport(RuntimeError("request failed for https://example/?token=PRIVATE"))

        result = reconcile_paypal_return(
            "https://pm-redirects.stripe.com/return?status=success&token=PRIVATE",
            transport=transport,
        )

        self.assertEqual(result.classification, ReconciliationClassification.UNKNOWN)
        self.assertEqual(result.outcome, PaymentOutcome.UNKNOWN)
        self.assertTrue(result.retryable)
        self.assertEqual(result.error_code, "transport_error")
        self.assertIn("RuntimeError", result.reason)
        self.assertNotIn("PRIVATE", json.dumps(result.to_dict()))

    def test_pending_processing_page_is_unknown_and_retryable(self):
        transport = FakeTransport(FakeResponse(200, text="Processing payment. Please wait."))

        result = reconcile_paypal_return(
            "https://pay.openai.com/c/pay/cs_live_PRIVATE?redirect_status=pending",
            transport=transport,
        )

        self.assertEqual(result.classification, ReconciliationClassification.UNKNOWN)
        self.assertEqual(result.outcome, PaymentOutcome.UNKNOWN)
        self.assertTrue(result.retryable)
        self.assertEqual(result.error_stage, "openai_pay")
        self.assertEqual(result.error_code, "payment_pending")

    def test_succeeded_pay_redirect_does_not_override_processing_verify_page(self):
        verify_url = "https://chatgpt.com/checkout/verify?checkout_session_id=PRIVATE"
        pay_url = (
            "https://pay.openai.com/c/pay/cs_live_PRIVATE"
            "?redirect_status=succeeded"
            f"&success_return_url={quote(verify_url, safe='')}"
        )
        transport = FakeTransport(
            FakeResponse(302, {"Location": verify_url}),
            FakeResponse(200, text="Processing your payment"),
        )

        result = reconcile_paypal_return(pay_url, transport=transport)

        self.assertEqual(result.classification, ReconciliationClassification.UNKNOWN)
        self.assertEqual(result.outcome, PaymentOutcome.UNKNOWN)
        self.assertTrue(result.retryable)
        self.assertEqual(result.error_stage, "checkout_verify")
        self.assertEqual(result.error_code, "payment_pending")

    def test_follows_meta_refresh_and_json_url_candidates(self):
        verify_url = "https://chatgpt.com/checkout/verify?checkout_session_id=cs_live_PRIVATE"
        pay_url = (
            "https://pay.openai.com/c/pay/cs_live_PRIVATE"
            "?redirect_status=succeeded"
            f"&success_return_url={quote(verify_url, safe='')}"
        )
        transport = FakeTransport(
            FakeResponse(
                200,
                text=f'<meta http-equiv="refresh" content="0; url={pay_url}">',
            ),
            FakeResponse(200, text=json.dumps({"verification_url": verify_url})),
            FakeResponse(200, text="Your payment was successful"),
        )

        result = reconcile_paypal_return(
            "https://pm-redirects.stripe.com/return?status=success",
            transport=transport,
        )

        self.assertTrue(result.ok)
        self.assertEqual([hop.next_stage for hop in result.hops[:2]], [
            ReturnStage.OPENAI_PAY,
            ReturnStage.CHECKOUT_VERIFY,
        ])

    def test_transient_http_and_authentication_have_distinct_retryability(self):
        for status, retryable, code in (
            (503, True, "transient_http_error"),
            (429, True, "transient_http_error"),
            (403, False, "authentication_required"),
        ):
            with self.subTest(status=status):
                transport = FakeTransport(FakeResponse(status))
                result = reconcile_paypal_return(
                    "https://chatgpt.com/checkout/verify?checkout_session_id=PRIVATE",
                    transport=transport,
                )
                self.assertEqual(result.classification, ReconciliationClassification.UNKNOWN)
                self.assertEqual(result.retryable, retryable)
                self.assertEqual(result.error_code, code)

    def test_normalizer_raises_safe_typed_validation_error(self):
        with self.assertRaises(ReturnURLValidationError) as captured:
            normalize_return_state("https://evil.example/?token=PRIVATE")

        self.assertEqual(captured.exception.code, "host_not_allowed")
        self.assertNotIn("PRIVATE", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
