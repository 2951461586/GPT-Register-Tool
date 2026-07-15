import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_link_manager as manager


class PaymentLinkManagerTests(unittest.TestCase):
    def test_supported_methods_include_reference_adapters(self):
        keys = {item["key"] for item in manager.supported_payment_methods()}
        self.assertEqual(keys, {"paypal", "gopay", "upi", "ideal", "pix", "kakao", "blik", "twint"})

    def test_aliases_are_normalized(self):
        self.assertEqual(manager.normalize_payment_method("go-pay"), "gopay")
        self.assertEqual(manager.normalize_payment_method("upi_qr"), "upi")
        self.assertEqual(manager.normalize_payment_method("kakao pay"), "kakao")

    def test_native_result_has_completed_state_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.gen_pp_link.generate_pp_link", return_value={"ok": True, "url": "https://example.test/pay"}):
                    result = manager.generate_payment_link("token", payment_method="paypal")
        self.assertTrue(result["ok"])
        self.assertEqual(result["manager_state"], "completed")
        self.assertEqual([item["state"] for item in result["state_history"]], [
            "created", "validating", "preparing_proxy", "running", "extracting", "completed"
        ])

    def test_unsupported_method_returns_failed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                result = manager.generate_payment_link("token", payment_method="unknown")
        self.assertFalse(result["ok"])
        self.assertEqual(result["manager_state"], "failed")

    def test_native_failure_preserves_adapter_error_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.gen_pp_link.generate_upi_qr_link", return_value={
                    "ok": False,
                    "error": "UPI unavailable",
                    "error_code": "upi_not_available",
                }):
                    result = manager.generate_payment_link("token", payment_method="upi")
        self.assertEqual(result["error_code"], "upi_not_available")
        self.assertEqual(result["manager_state"], "failed")

    def test_gopay_uses_project_payment_service(self):
        response = {"success": True, "checkoutUrl": "https://pay.openai.com/c/pay/cs_test", "flowId": "flow-1"}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.grpcurl_client.call_grpcurl", return_value=response) as rpc:
                    result = manager.generate_payment_link("token", payment_method="gopay", phone="+628123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["flow_id"], "flow-1")
        self.assertEqual(rpc.call_args.args[0], "StartGoPay")


if __name__ == "__main__":
    unittest.main()
