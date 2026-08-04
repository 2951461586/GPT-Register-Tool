import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_batch


class PaymentBatchTests(unittest.TestCase):
    def setUp(self):
        config = patch.object(payment_batch, "CFG", {})
        config.start()
        self.addCleanup(config.stop)
        canary_pause = patch.object(payment_batch, "_active_canary_pause", return_value={})
        canary_pause.start()
        self.addCleanup(canary_pause.stop)

    def test_batch_runs_jit_gate_and_reports_matrix_counts(self):
        auth = {
            "ok": True,
            "access_token": "secret-token",
            "auth_context": {"email": "hidden@example.com"},
            "probed": 1,
            "refreshed": False,
            "probe": {"status_code": 200},
        }
        payment = {
            "ok": True,
            "payment_method": "momo",
            "decision": "ready_with_qr",
            "amount_due": 0,
            "has_momo": True,
            "url": "https://payment.momo.vn/v2/gateway/pay?t=1",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "report.json"):
            report = payment_batch.run_payment_batch(
                ["A@example.com", "a@example.com"],
                payment_method="momo",
                workers=5,
                matrix={"cells": [{"name": "vn", "sample_size": 1}]},
            )
        self.assertEqual(report["counts"]["requested"], 1)
        self.assertEqual(report["counts"]["qr_ready"], 1)
        self.assertEqual(report["matrix"][0]["eligible"], 1)
        self.assertNotIn("access_token", report["results"][0]["auth"])
        self.assertNotIn("email", report["results"][0])

    def test_conclusive_ineligible_result_is_not_retried(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        payment = {"ok": False, "decision": "account_trial_ineligible", "error": "no trial"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment) as generate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "report.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="momo", retries=2,
            )
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(report["counts"]["trial_ineligible"], 1)

    def test_terminal_result_counts_preserve_unknown_cancelled_and_timeout(self):
        counts = payment_batch._batch_counts([
            {"status": "cancelled", "ok": False},
            {"status": "unknown", "ok": False, "retryable": False},
            {"status": "timed_out", "ok": False, "retryable": True},
        ], 3)

        self.assertEqual(counts["cancelled"], 1)
        self.assertEqual(counts["unknown"], 1)
        self.assertEqual(counts["timed_out"], 1)
        self.assertEqual(counts["retryable"], 1)

    def test_matrix_matches_payment_method_and_registration_country(self):
        auth = {
            "ok": True,
            "access_token": "secret",
            "auth_context": {"registration_country": "VN"},
            "probed": 1,
        }
        payment = {"ok": False, "decision": "account_trial_ineligible"}
        matrix = {"cells": [
            {"name": "kr-kakao", "payment_method": "kakao", "registration_country": "KR"},
            {"name": "vn-momo", "payment_method": "momo", "registration_country": "VN"},
        ]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "report.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="momo", matrix=matrix,
            )
        self.assertEqual(report["results"][0]["matrix_cell"], "vn-momo")

    def test_matrix_country_mismatch_stops_before_checkout(self):
        auth = {
            "ok": True,
            "access_token": "secret",
            "auth_context": {"registration_country": "US"},
            "probed": 1,
        }
        matrix = {"cells": [
            {"name": "vn-momo", "payment_method": "momo", "registration_country": "VN"},
        ]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link") as generate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "report.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="momo", matrix=matrix,
            )
        self.assertEqual(report["results"][0]["decision"], "matrix_registration_country_mismatch")
        generate.assert_not_called()

    def test_stable_batch_id_resumes_checkpointed_accounts(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        payment = {"ok": True, "decision": "ready_with_qr", "amount_due": 0, "has_momo": True,
                   "url": "https://payment.momo.vn/pay/1"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth) as ensure, \
             patch.object(payment_batch, "generate_payment_link", return_value=payment), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "resume.json"):
            payment_batch.run_payment_batch(["a@example.com"], payment_method="momo", batch_id="resume")
            report = payment_batch.run_payment_batch(["a@example.com"], payment_method="momo", batch_id="resume")
        self.assertEqual(ensure.call_count, 1)
        self.assertEqual(report["status"], "finished")
        self.assertEqual(report["resumed"], 1)

    def test_probe_only_runs_checkout_capability_without_payment_link_generation(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        capability = {
            "ok": True,
            "status": "completed",
            "classification": "eligible",
            "eligible": True,
            "conclusive": True,
            "decision": "payment_method_available",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "payment_method_capability_probe", return_value=capability) as probe, \
             patch.object(payment_batch, "generate_payment_link") as generate, \
             patch.object(payment_batch, "_active_canary_pause") as active_pause, \
             patch.object(payment_batch, "_record_canary_state") as record_canary, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "probe.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", probe_only=True,
            )
        generate.assert_not_called()
        probe.assert_called_once()
        active_pause.assert_not_called()
        record_canary.assert_not_called()
        self.assertEqual(report["counts"]["authenticated"], 1)
        self.assertEqual(report["counts"]["attempted"], 0)
        self.assertEqual(report["counts"]["completed"], 0)
        self.assertEqual(report["counts"]["capability_probed"], 1)
        self.assertEqual(report["results"][0]["decision"], "payment_method_available")

    def test_probe_only_canary_records_capability_state(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        capability = {
            "ok": False,
            "status": "unknown",
            "classification": "unknown",
            "eligible": None,
            "conclusive": False,
            "decision": "stripe_init_failed",
            "retryable": True,
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "payment_method_capability_probe", return_value=capability), \
             patch.object(payment_batch, "generate_payment_link") as generate, \
             patch.object(payment_batch, "_record_canary_state", return_value={"paused": True}) as record_canary, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "probe-canary.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", probe_only=True, canary=1,
            )
        generate.assert_not_called()
        record_canary.assert_called_once()
        self.assertTrue(report["canary_state"]["paused"])

    def test_probe_checkpoint_is_not_reused_for_payment_execution(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        payment = {"ok": True, "url": "https://example.test/pay"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth) as ensure, \
             patch.object(payment_batch, "payment_method_capability_probe", return_value={
                 "ok": True, "status": "completed", "classification": "eligible",
                 "eligible": True, "conclusive": True, "decision": "payment_method_available",
             }), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment) as generate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "same-id.json"):
            payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", batch_id="same-id", probe_only=True,
            )
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", batch_id="same-id", probe_only=False,
            )
        self.assertEqual(ensure.call_count, 2)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(report["resumed"], 0)
        self.assertFalse(report["probe_only"])
        self.assertEqual(report["counts"]["link_ready"], 1)

    def test_report_recursively_redacts_proxy_credentials(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        payment = {
            "ok": False,
            "decision": "checkout_failed",
            "error": "connect http://user:pass@proxy.example:8080 failed",
            "detail": {"checkout_proxy": "http://user:pass@proxy.example:8080"},
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "report.json"):
            report = payment_batch.run_payment_batch(["a@example.com"], payment_method="momo", retries=0)
        serialized = str(report)
        self.assertNotIn("user:pass", serialized)
        self.assertNotIn("checkout_proxy", serialized)


if __name__ == "__main__":
    unittest.main()
