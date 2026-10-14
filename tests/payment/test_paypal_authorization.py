"""Unit tests for sms_tool.paypal_authorization (read-only normalization)."""

from __future__ import annotations

import unittest

from sms_tool.paypal_authorization import (
    PayPalAuthorizationContext,
    classify_authorization_outcome,
    parse_authorization_context,
    to_payment_result,
)
from sms_tool.payment_contracts import PaymentResult, payment_retry_allowed


class TestParseContext(unittest.TestCase):
    def test_approved_billing(self):
        p = {
            "checkoutSessionType": "BILLING_WITHOUT_PURCHASE",
            "billingAgreementId": "B-123",
            "approved": True,
            "data": {"status": "COMPLETED"},
        }
        ctx = parse_authorization_context(p, ba_token="BA-x")
        self.assertEqual(ctx.status, "completed")
        self.assertTrue(ctx.approved)
        self.assertEqual(ctx.billing_agreement_id, "B-123")
        self.assertEqual(ctx.ba_token, "BA-x")

    def test_checkout_type_from_data(self):
        p = {"data": {"checkoutSessionType": "BILLING_WITHOUT_PURCHASE"}}
        ctx = parse_authorization_context(p)
        self.assertEqual(ctx.checkout_session_type, "BILLING_WITHOUT_PURCHASE")

    def test_fatal_contingency(self):
        p = {"errorCode": "PAYER_ACCOUNT_RESTRICTED", "checkoutSessionType": "BILLING_WITHOUT_PURCHASE"}
        ctx = parse_authorization_context(p)
        self.assertEqual(ctx.error_code, "PAYER_ACCOUNT_RESTRICTED")
        self.assertEqual(ctx.status, "failed")

    def test_raw_string_html_extracts_ba(self):
        html = "<html>... /agreements/approve?ba_token=BA-abc123 ...</html>"
        ctx = parse_authorization_context(html)
        self.assertEqual(ctx.ba_token, "BA-abc123")

    def test_none_payload(self):
        ctx = parse_authorization_context(None)
        self.assertEqual(ctx.status, "unknown")
        self.assertFalse(ctx.approved)

    def test_transient_signal_detected(self):
        p = {"errorCode": "RATE_LIMIT", "checkoutSessionType": "BILLING_WITHOUT_PURCHASE"}
        ctx = parse_authorization_context(p)
        self.assertEqual(ctx.error_code, "RATE_LIMIT")


class TestClassifyAndMap(unittest.TestCase):
    def test_ok_outcome(self):
        ctx = parse_authorization_context(
            {"checkoutSessionType": "BILLING_WITHOUT_PURCHASE", "billingAgreementId": "B-1", "approved": True}
        )
        out = classify_authorization_outcome(ctx)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "completed")
        self.assertFalse(out["retryable"])

    def test_fatal_not_retryable(self):
        ctx = parse_authorization_context({"errorCode": "ACCOUNT_LOCKED", "checkoutSessionType": "BILLING_WITHOUT_PURCHASE"})
        out = classify_authorization_outcome(ctx)
        self.assertFalse(out["ok"])
        self.assertFalse(out["retryable"])

    def test_transient_retryable(self):
        ctx = parse_authorization_context({"errorCode": "TOO_MANY_REQUESTS", "checkoutSessionType": "BILLING_WITHOUT_PURCHASE"})
        out = classify_authorization_outcome(ctx)
        self.assertFalse(out["ok"])
        self.assertTrue(out["retryable"])

    def test_unexpected_checkout_type(self):
        ctx = parse_authorization_context({"checkoutSessionType": "PURCHASE", "approved": False})
        out = classify_authorization_outcome(ctx)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_code"], "unexpected_checkout_type")
        self.assertFalse(out["retryable"])

    def test_to_payment_result_alignment(self):
        ctx = parse_authorization_context(
            {"checkoutSessionType": "BILLING_WITHOUT_PURCHASE", "billingAgreementId": "B-1", "approved": True}
        )
        res = to_payment_result(ctx, payment_method="paypal", operation="execute_payment")
        pr = PaymentResult.from_mapping(res, payment_method="paypal", operation="execute_payment")
        self.assertTrue(pr.ok)
        self.assertEqual(pr.status, "completed")
        self.assertFalse(pr.error.retryable)

    def test_payment_retry_allowed_gate(self):
        # fatal -> no retry
        ctx = parse_authorization_context({"errorCode": "PAYER_ACCOUNT_RESTRICTED", "checkoutSessionType": "BILLING_WITHOUT_PURCHASE"})
        res = to_payment_result(ctx)
        self.assertFalse(payment_retry_allowed(res))

    def test_payment_retry_allowed_transient(self):
        ctx = parse_authorization_context({"errorCode": "INTERNAL_SERVER_ERROR", "checkoutSessionType": "BILLING_WITHOUT_PURCHASE"})
        res = to_payment_result(ctx)
        self.assertTrue(payment_retry_allowed(res))


if __name__ == "__main__":
    unittest.main()