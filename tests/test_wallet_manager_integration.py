import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_link_manager as manager


class WalletManagerIntegrationTests(unittest.TestCase):
    def test_wallet_adapter_result_uses_common_manager_contract(self):
        adapter_result = {
            "ok": True,
            "status": "completed",
            "operation": "extract_link",
            "url": "https://app.midtrans.com/snap/v4/redirection/fixture",
            "provider_redirect_url": "https://app.midtrans.com/snap/v4/redirection/fixture",
            "link_type": "gopay_protocol",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(manager.CFG, {"protocol_payments": {"enabled_methods": ["gopay"]}}, clear=False), \
             patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"), \
             patch.object(manager, "_run_wallet_adapter", return_value=adapter_result) as adapter:
            result = manager.generate_payment_link("token", payment_method="gopay")

        self.assertTrue(result["ok"])
        self.assertEqual(result["manager_state"], "completed")
        self.assertEqual(result["error_stage"], "")
        adapter.assert_called_once()

    def test_probe_only_uses_capability_probe_and_never_runs_wallet_full_flow(self):
        probe_result = {
            "ok": True,
            "status": "completed",
            "operation": "payment_method_capability_probe",
            "classification": "eligible",
            "eligible": True,
            "conclusive": True,
            "payment_method_types": ["gopay"],
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(manager.CFG, {"protocol_payments": {"enabled_methods": ["gopay"]}}, clear=False), \
             patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"), \
             patch("sms_tool.payment_capability.payment_method_capability_probe", return_value=probe_result) as probe, \
             patch.object(manager, "_run_wallet_adapter") as adapter:
            result = manager.generate_payment_link("token", payment_method="gopay", probe_only=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["manager_state"], "completed")
        self.assertEqual(result["operation"], "payment_method_capability_probe")
        probe.assert_called_once()
        adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
