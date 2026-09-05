import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_link_manager as manager


# Every module in the ``pay_link`` subpackage binds ``current_config_data``
# with ``from ..config import current_config_data``, so each one owns a
# *separate* module-level name.  Patching the attribute on the
# ``payment_link_manager`` back-compat shell (``patch.object(manager, ...)``)
# rebinds none of them: production keeps reading the real merged config,
# which means the real payment proxy pool and therefore **real geo probes
# against paid proxies** (``us.ipwo.net``).  That is what made
# ``test_subprocess_timeout_...`` flaky -- the run only reached the patched
# ``subprocess.run`` when the live probe happened to succeed.
#
# Patch every binding.  ``base`` is the one ``_config_data()`` resolves; the
# rest are patched so no other code path can slip back to the real config.
_CONFIG_SEAMS = (
    "sms_tool.pay_link.base.current_config_data",
    "sms_tool.pay_link.core.current_config_data",
    "sms_tool.pay_link.adapters.current_config_data",
    "sms_tool.pay_link.normalize.current_config_data",
    "sms_tool.pay_link.persistence.current_config_data",
    "sms_tool.pay_link.registry.current_config_data",
)

_ISOLATED_CONFIG = {
    "chatgpt": {},
    "protocol_payments": {},
    "egress_check": {"enabled": False},
}


class PaymentResultContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for seam in _CONFIG_SEAMS:
            config_patch = patch(seam, return_value=_ISOLATED_CONFIG)
            config_patch.start()
            self.addCleanup(config_patch.stop)
        # Isolate PaymentOperationStore so it never touches the real
        # runtime/payment_operations/ directory.  Every generate_payment_link()
        # call hits core.py:92 → from_config(source) → runtime_file(...) which
        # would otherwise land in the real runtime tree and leave residual
        # files + cross-process lock files that cause flaky gate timeouts.
        #
        # We patch the attribute on the *core* module (where begin() is
        # called) rather than on PaymentOperationStore.from_config because
        # the latter is a classmethod and mock's classmethod wrapper has
        # edge cases when the class is re-imported via different paths.
        from sms_tool.payment_operation import PaymentOperationStore as _Store
        self._store_root = Path(self._tmp.name) / "payment_operations"
        self._store_patch = patch(
            "sms_tool.pay_link.core.PaymentOperationStore",
            _Store,
        )
        self._store_patch.start()
        self.addCleanup(self._store_patch.stop)
        # Patch from_config to point at our isolated root
        self._from_config_patch = patch.object(
            _Store,
            "from_config",
            classmethod(lambda cls, config: cls(self._store_root)),
        )
        self._from_config_patch.start()
        self.addCleanup(self._from_config_patch.stop)

    def _state_file(self, directory: str) -> Path:
        return Path(directory) / "payment-runs.jsonl"

    def test_success_has_non_retryable_empty_error_contract(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)), \
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
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)), \
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
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)), \
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
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)), \
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
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)), \
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
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)), \
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
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.pay_link.adapters._protocol_cfg", return_value={"timeout_seconds": 3}), \
             patch("sms_tool.payment_egress.assert_egress_countries"), \
             patch("sms_tool.payment_link_manager.subprocess.run", side_effect=timeout) as run_mock:
            result = manager.generate_payment_link(
                "token",
                payment_method="ideal",
                seed_proxy="socks5h://127.0.0.1:1080",
            )

        # Diagnostic: confirm the mock was actually invoked exactly once.
        # If call_count != 1 the patch didn't take (flaky root-cause probe).
        # Dump the full result so we can see which early-return path was taken.
        self.assertEqual(1, run_mock.call_count,
                         f"subprocess.run was called {run_mock.call_count} times; "
                         f"the patch may not have taken effect. "
                         f"result={result}")
        self.assertFalse(result["ok"])
        self.assertEqual("timed_out", result["status"])
        self.assertEqual("timed_out", result["manager_state"])
        self.assertEqual("extractor_timed_out", result["error_code"])
        self.assertEqual("adapter_subprocess", result["error_stage"])
        self.assertIs(True, result["retryable"])

    def test_keyboard_interrupt_is_returned_as_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=KeyboardInterrupt):
            result = manager.generate_payment_link("token", payment_method="paypal")

        self.assertEqual("cancelled", result["manager_state"])
        self.assertEqual("cancelled", result["status"])
        self.assertEqual("payment_link_cancelled", result["error_code"])
        self.assertEqual("adapter", result["error_stage"])
        self.assertIs(False, result["retryable"])

    def test_regular_adapter_failure_gets_structured_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)), \
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
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)), \
             patch("sms_tool.gen_pp_link.generate_pp_link", return_value={}):
            result = manager.generate_payment_link("token", payment_method="paypal")

        self.assertEqual("failed", result["manager_state"])
        self.assertEqual("invalid_adapter_result", result["error_code"])
        self.assertEqual("adapter_contract", result["error_stage"])
        self.assertIs(False, result["retryable"])

    def test_validation_failure_has_structured_non_retryable_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch("sms_tool.pay_link.persistence._state_path", return_value=self._state_file(tmp)):
            result = manager.generate_payment_link("token", payment_method="not-supported")

        self.assertEqual("failed", result["manager_state"])
        self.assertEqual("validation", result["error_stage"])
        self.assertIs(False, result["retryable"])


if __name__ == "__main__":
    unittest.main()
