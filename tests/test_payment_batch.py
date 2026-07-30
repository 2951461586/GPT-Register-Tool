import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_batch


class PaymentBatchTests(unittest.TestCase):
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
