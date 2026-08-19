import json
import tempfile
import unittest
from pathlib import Path

from sms_tool.payment_operation import PaymentOperationConflict, PaymentOperationStore, conflict_result


class PaymentOperationTests(unittest.TestCase):
    def test_uncertain_operation_cannot_be_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PaymentOperationStore(Path(tmp))
            operation = store.begin(
                payment_method="paypal",
                operation="execute_payment",
                idempotency_key="batch:account",
                operation_id="op-1",
            )
            operation.checkpoint("confirm", "running", side_effect_started=True)
            operation.fail_unknown("confirm", "confirm_response_lost")

            with self.assertRaises(PaymentOperationConflict) as context:
                store.begin(
                    payment_method="paypal",
                    operation="execute_payment",
                    idempotency_key="batch:account",
                    operation_id="op-2",
                )

            result = conflict_result(context.exception)
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(result["requires_reconciliation"])
            self.assertFalse(result["retryable"])

    def test_pre_side_effect_retryable_failure_can_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PaymentOperationStore(Path(tmp))
            operation = store.begin(
                payment_method="ideal",
                operation="extract_link",
                idempotency_key="retry-key",
            )
            operation.checkpoint("checkout", "timed_out", error_code="checkout_timeout")
            operation.record["retryable"] = True
            operation.checkpoint("checkout", "timed_out", error_code="checkout_timeout")
            operation.close()

            resumed = store.begin(
                payment_method="ideal",
                operation="extract_link",
                idempotency_key="retry-key",
            )
            self.assertEqual(resumed.record["attempt"], 2)
            self.assertFalse(resumed.record["recovered_from_stale"])
            resumed.close()

    def test_journal_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PaymentOperationStore(Path(tmp))
            operation = store.begin(
                payment_method="upi",
                operation="extract_link",
                idempotency_key="journal-key",
            )
            operation.close()
            files = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertNotIn("journal-key", files[0].read_text(encoding="utf-8"))

    def test_idempotency_key_is_never_written_to_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_key = "account@example.com:http://user:pass@proxy.example:8080"
            store = PaymentOperationStore(Path(tmp))
            operation = store.begin(
                payment_method="paypal",
                operation="extract_link",
                idempotency_key=secret_key,
            )
            operation.close()
            serialized = next(Path(tmp).glob("*.json")).read_text(encoding="utf-8")
            for secret in ("account@example.com", "user", "pass", "proxy.example"):
                self.assertNotIn(secret, serialized)

    def test_caller_operation_id_is_hashed_before_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PaymentOperationStore(Path(tmp))
            operation = store.begin(
                payment_method="paypal",
                operation="extract_link",
                idempotency_key="safe-key",
                operation_id="batch:account@example.com",
            )
            operation.close()
            self.assertTrue(operation.operation_id.startswith("op_"))
            self.assertNotIn("account@example.com", next(Path(tmp).glob("*.json")).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
