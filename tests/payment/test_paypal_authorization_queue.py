import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import paypal_authorization_queue as queue


class PayPalAuthorizationQueueTests(unittest.TestCase):
    def test_enqueue_deduplicates_ba_token_and_redacts_public_payload(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(queue, "queue_path", return_value=Path(tmp) / "queue.json"):
            first = queue.enqueue_paypal_ba_authorization(
                email="User@example.com",
                approval_url="https://www.paypal.com/agreements/approve?ba_token=BA-ONE",
                batch_id="batch-1",
            )
            second = queue.enqueue_paypal_ba_authorization(
                email="user@example.com",
                approval_url="https://www.paypal.com/agreements/approve?ba_token=BA-ONE",
                batch_id="batch-1",
            )
            stored = queue.list_paypal_ba_authorizations()

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0]["approval_url_present"])
        self.assertTrue(stored[0]["ba_token_present"])
        self.assertNotIn("approval_url", stored[0])
        self.assertNotIn("ba_token", stored[0])

    def test_process_updates_durable_status_and_emits_terminal_event(self):
        events = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(queue, "queue_path", return_value=Path(tmp) / "queue.json"):
            queue.enqueue_paypal_ba_authorization(
                email="user@example.com",
                approval_url="https://www.paypal.com/agreements/approve?ba_token=BA-TWO",
                batch_id="batch-2",
            )
            report = queue.process_paypal_ba_authorizations(
                lambda item: {"ok": True, "status": "completed"}, progress=events.append,
            )
            stored = queue.list_paypal_ba_authorizations()

        self.assertTrue(report["ok"])
        self.assertEqual(stored[0]["status"], "completed")
        self.assertEqual(stored[0]["attempts"], 1)
        self.assertTrue(events[-1]["account_terminal"])
        self.assertEqual(events[-1]["operation"], "paypal_ba_authorize")


if __name__ == "__main__":
    unittest.main()
