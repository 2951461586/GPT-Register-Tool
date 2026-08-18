import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_link_manager as manager


class PaymentResultContractTests(unittest.TestCase):
    def setUp(self):
        self._config_patch = patch.object(
            manager,
            "current_config_data",
            return_value={"chatgpt": {}, "protocol_payments": {}},
        )
        self._config_patch.start()
        self.addCleanup(self._config_patch.stop)

    def _state_file(self, directory: str) -> Path:
        return Path(directory) / "payment-runs.jsonl"

    def test_success_has_non_retryable_empty_error_contract(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_pp_link", return_value={
                 "ok": True,
                 "url": "https://example.test/approve",
                 "retryable": True,
                 "error_stage": "stale-adapter-stage",
             }):
            result = manager.generate_payment_link("token", payment_method="paypal")

        self.assertTrue(result["ok"])
        self.assertEqual("completed", result["manager_state"])
        self.assertIs(False, result["retryable"])
        self.assertEqual("", result["error_stage"])

    def test_explicit_adapter_cancellation_is_not_collapsed_into_failure(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_pp_link", return_value={
                 "ok": False,
                 "status": "canceled",
                 "error": "stopped by operator",
                 "stage": "provider_redirect",
                 "retryable": True,
             }):
            result = manager.generate_payment_link("token", payment_method="paypal")

        self.assertFalse(result["ok"])
        self.assertEqual("cancelled", result["status"])
        self.assertEqual("cancelled", result["manager_state"])
        self.assertEqual("payment_link_cancelled", result["error_code"])
        self.assertEqual("provider_redirect", result["error_stage"])
        self.assertIs(False, result["retryable"])
        self.assertEqual("cancelled", result["state_history"][-1]["state"])

    def test_unknown_adapter_outcome_requires_reconciliation_and_is_not_retryable(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_pp_link", return_value={
                 "ok": False,
                 "state": "unknown",
                 "error_code": "payment_outcome_unknown",
                 "error": "confirm response was lost",
                 "stage": "confirm",
                 "outcome_unknown": True,
                 "retry_safe": True,
             }):
            result = manager.generate_payment_link("token", payment_method="paypal")

        self.assertEqual("unknown", result["manager_state"])
        self.assertEqual("unknown", result["status"])
        self.assertTrue(result["requires_reconciliation"])
        self.assertIs(False, result["retryable"])
        self.assertEqual("confirm", result["error_stage"])

    def test_exception_marked_outcome_unknown_is_not_reported_as_failure(self):
        class OutcomeUnknownError(Exception):
            outcome_unknown = True
            stage = "approve"

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=OutcomeUnknownError("response lost")):
            result = manager.generate_payment_link("token", payment_method="paypal")

        self.assertEqual("unknown", result["manager_state"])
        self.assertEqual("payment_outcome_unknown", result["error_code"])
        self.assertEqual("approve", result["error_stage"])
        self.assertTrue(result["requires_reconciliation"])
        self.assertIs(False, result["retryable"])

    def test_structured_exception_preserves_terminal_code_and_stage(self):
        class StructuredUnknownError(Exception):
            status = "unknown"
            error_code = "confirm_response_lost"
            error_stage = "confirm"
            retryable = True

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=StructuredUnknownError("response lost")):
            result = manager.generate_payment_link("token", payment_method="paypal")

        self.assertEqual("unknown", result["manager_state"])
        self.assertEqual("confirm_response_lost", result["error_code"])
        self.assertEqual("confirm", result["error_stage"])
        self.assertTrue(result["requires_reconciliation"])
        self.assertIs(False, result["retryable"])

    def test_incomplete_pending_result_is_unknown_but_pending_link_is_complete(self):
        pending_without_link = {
            "ok": False,
            "status": "processing",
            "error": "provider has not returned a final result",
            "stage": "provider",
        }
        pending_with_link = {
            "ok": True,
            "status": "pending",
            "url": "https://example.test/authorize",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=[pending_without_link, pending_with_link]):
            unknown = manager.generate_payment_link("token", payment_method="paypal")
            complete = manager.generate_payment_link("token", payment_method="paypal")

        self.assertEqual("unknown", unknown["manager_state"])
        self.assertFalse(unknown["ok"])
        self.assertEqual("completed", complete["manager_state"])
        self.assertTrue(complete["ok"])
        self.assertEqual("pending", complete["status"])

    def test_subprocess_timeout_has_distinct_retryable_terminal_contract(self):
        timeout = subprocess.TimeoutExpired(cmd=["extractor"], timeout=3)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)), \
             patch.object(manager, "_protocol_cfg", return_value={"timeout_seconds": 3}), \
             patch("sms_tool.payment_link_manager.subprocess.run", side_effect=timeout):
            result = manager.generate_payment_link(
                "token",
                payment_method="ideal",
                seed_proxy="socks5h://127.0.0.1:1080",
            )

        self.assertFalse(result["ok"])
        self.assertEqual("timed_out", result["status"])
        self.assertEqual("timed_out", result["manager_state"])
        self.assertEqual("extractor_timed_out", result["error_code"])
        self.assertEqual("adapter_subprocess", result["error_stage"])
        self.assertIs(True, result["retryable"])

    def test_keyboard_interrupt_is_returned_as_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=KeyboardInterrupt):
            result = manager.generate_payment_link("token", payment_method="paypal")

        self.assertEqual("cancelled", result["manager_state"])
        self.assertEqual("cancelled", result["status"])
        self.assertEqual("payment_link_cancelled", result["error_code"])
        self.assertEqual("adapter", result["error_stage"])
        self.assertIs(False, result["retryable"])

    def test_regular_adapter_failure_gets_structured_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_upi_qr_link", return_value={
                 "ok": False,
                 "error": "UPI is unavailable",
                 "error_code": "upi_not_available",
             }):
            result = manager.generate_payment_link("token", payment_method="upi")

        self.assertEqual("failed", result["manager_state"])
        self.assertEqual("upi_not_available", result["error_code"])
        self.assertEqual("adapter", result["error_stage"])
        self.assertIs(False, result["retryable"])

    def test_invalid_adapter_result_remains_a_definitive_contract_failure(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_pp_link", return_value={}):
            result = manager.generate_payment_link("token", payment_method="paypal")

        self.assertEqual("failed", result["manager_state"])
        self.assertEqual("invalid_adapter_result", result["error_code"])
        self.assertEqual("adapter_contract", result["error_stage"])
        self.assertIs(False, result["retryable"])

    def test_validation_failure_has_structured_non_retryable_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=self._state_file(tmp)):
            result = manager.generate_payment_link("token", payment_method="not-supported")

        self.assertEqual("failed", result["manager_state"])
        self.assertEqual("validation", result["error_stage"])
        self.assertIs(False, result["retryable"])


if __name__ == "__main__":
    unittest.main()
