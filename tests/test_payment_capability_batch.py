import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_batch


class PaymentCapabilityBatchTests(unittest.TestCase):
    def setUp(self):
        config = patch.object(payment_batch, "CFG", {})
        config.start()
        self.addCleanup(config.stop)

    def test_matrix_checkout_country_is_forwarded_to_capability_probe(self):
        auth = {
            "ok": True,
            "access_token": "secret",
            "auth_context": {"registration_country": "ID"},
            "probed": 1,
        }
        capability = {
            "ok": True,
            "status": "completed",
            "classification": "eligible",
            "eligible": True,
            "conclusive": True,
            "decision": "payment_method_available",
        }
        matrix = {"cells": [{
            "name": "id-gopay",
            "payment_method": "gopay",
            "registration_country": "ID",
            "checkout_country": "ID",
            "provider_country": "ID",
        }]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "normalize_payment_method", return_value="gopay"), \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "payment_method_capability_probe", return_value=capability) as probe, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "probe.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="gopay", probe_only=True, matrix=matrix,
            )
        self.assertEqual(probe.call_args.kwargs["checkout_country"], "ID")
        self.assertEqual(report["matrix"][0]["eligible"], 1)

    def test_unknown_capability_canary_pauses_profile(self):
        report = {
            "probe_only": True,
            "results": [{
                "capability_probed": True,
                "classification": "unknown",
                "decision": "stripe_init_failed",
                "retryable": True,
            }],
            "counts": {},
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "_canary_state_path", return_value=Path(tmp) / "state.json"):
            state = payment_batch._record_canary_state("gopay", report)
        self.assertTrue(state["paused"])
        self.assertEqual(state["capability_probed"], 1)

    def test_conclusive_unavailable_method_does_not_pause_profile(self):
        report = {
            "probe_only": True,
            "results": [{
                "capability_probed": True,
                "classification": "ineligible",
                "conclusive": True,
                "decision": "payment_method_unavailable",
            }],
            "counts": {},
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "_canary_state_path", return_value=Path(tmp) / "state.json"):
            state = payment_batch._record_canary_state("gopay", report)
        self.assertFalse(state["paused"])
        self.assertEqual(state["completed"], 1)


if __name__ == "__main__":
    unittest.main()
