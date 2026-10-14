import unittest

from sms_tool.payment_batch import _is_transient
from sms_tool.payment_contracts import PaymentResult, PaymentTerminalState


class PaymentRetryGateTests(unittest.TestCase):
    def test_typed_result_preserves_compatible_dict_fields(self):
        result = PaymentResult.from_mapping({
            "ok": False,
            "status": "timed_out",
            "payment_method": "ideal",
            "operation": "extract_link",
            "error": "checkout timed out",
            "error_code": "checkout_timeout",
            "error_stage": "checkout",
            "retryable": True,
            "url": "",
            "adapter_detail": "kept",
        }).to_dict()

        self.assertEqual(result["status"], PaymentTerminalState.TIMED_OUT.value)
        self.assertTrue(result["retryable"])
        self.assertFalse(result["side_effect_started"])
        self.assertEqual(result["adapter_detail"], "kept")

    def test_confirm_approve_and_redirect_failures_are_never_retried(self):
        for stage in (
            "confirm", "approve", "provider_redirect", "redirect", "follow_redirect", "poll",
        ):
            with self.subTest(stage=stage):
                result = {
                    "ok": False,
                    "status": "timed_out",
                    "error_stage": stage,
                    "retryable": True,
                }
                self.assertFalse(_is_transient(result))
                self.assertTrue(PaymentResult.from_mapping(result).outcome.side_effect_started)

    def test_blik_submission_is_a_side_effect_retry_barrier(self):
        self.assertFalse(_is_transient({
            "ok": False,
            "status": "timed_out",
            "payment_method": "blik",
            "operation": "execute_payment",
            "error_stage": "blik_submit",
            "retryable": True,
        }))

    def test_unknown_or_reconciliation_result_is_never_retried(self):
        for result in (
            {"ok": False, "status": "unknown", "retryable": True},
            {"ok": False, "status": "timed_out", "retryable": True, "requires_reconciliation": True},
            {"ok": False, "status": "timed_out", "retryable": True, "side_effect_started": True},
        ):
            with self.subTest(result=result):
                self.assertFalse(_is_transient(result))

    def test_untyped_error_text_does_not_enable_retry(self):
        self.assertFalse(_is_transient({
            "ok": False,
            "error": "network timeout through proxy",
        }))

    def test_explicit_pre_side_effect_timeout_can_retry(self):
        self.assertTrue(_is_transient({
            "ok": False,
            "status": "timed_out",
            "error_stage": "checkout",
            "retryable": True,
            "side_effect_started": False,
        }))


if __name__ == "__main__":
    unittest.main()
