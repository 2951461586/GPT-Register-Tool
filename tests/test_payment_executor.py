import unittest

from sms_tool.payment_executor import PaymentExecutionRequest, PaymentFlowExecutor
from sms_tool.payment_routing import PaymentRoutePlan


class PaymentFlowExecutorTests(unittest.TestCase):
    def test_unknown_post_side_effect_result_is_not_retryable(self):
        executor = PaymentFlowExecutor(lambda _request: {
            "ok": False,
            "status": "unknown",
            "error": "confirmation outcome unknown",
            "error_stage": "confirm",
            "retryable": True,
            "side_effect_started": True,
        })
        result = executor.run(PaymentExecutionRequest(
            payment_method="gopay",
            access_token="token",
            route_plan=PaymentRoutePlan.empty("gopay"),
            auth_context={},
            runtime_config={},
            options={},
        ))
        self.assertEqual(result["manager_state"], "unknown")
        self.assertTrue(result["requires_reconciliation"])
        self.assertFalse(result["retryable"])

    def test_adapter_error_code_survives_normalization(self):
        executor = PaymentFlowExecutor(lambda _request: {
            "ok": False,
            "error": "not available",
            "error_code": "method_not_available",
        })
        result = executor.run(PaymentExecutionRequest(
            payment_method="upi",
            access_token="token",
            route_plan=PaymentRoutePlan.empty("upi"),
            auth_context={},
            runtime_config={},
            options={},
        ))
        self.assertEqual(result["error_code"], "method_not_available")


if __name__ == "__main__":
    unittest.main()
