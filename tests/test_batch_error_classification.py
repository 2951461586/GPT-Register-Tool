import threading
import unittest

from sms_tool.batch_runner import run_batch_impl
from sms_tool.storage import _status


class BatchErrorClassificationTests(unittest.TestCase):
    def test_batch_runner_honors_ten_requested_workers(self):
        barrier = threading.Barrier(10, timeout=3)

        def run_email(**_):
            barrier.wait()
            return {"success": True}

        results = run_batch_impl(
            count=10,
            workers=10,
            run_email_func=run_email,
        )

        self.assertEqual(len(results), 10)
        self.assertTrue(all(result["success"] for result in results))

    def test_network_failure_is_not_marked_dropped(self):
        results = run_batch_impl(
            count=1,
            workers=1,
            run_email_func=lambda **_: {
                "success": False,
                "error": "[WinError 10060] proxy connection timed out",
            },
        )

        self.assertEqual(results[0]["failure_class"], "network")
        self.assertFalse(results[0]["dropped"])

    def test_account_failure_is_marked_dropped(self):
        results = run_batch_impl(
            count=1,
            workers=1,
            run_email_func=lambda **_: {
                "success": False,
                "error": "account_deactivated",
            },
        )

        self.assertEqual(results[0]["failure_class"], "account")
        self.assertTrue(results[0]["dropped"])

    def test_storage_keeps_network_failures_separate_from_dead_accounts(self):
        status = _status(
            {"success": False, "failure_class": "network", "error": "proxy timeout"},
            {},
            "",
            has_refresh_token=False,
        )

        self.assertEqual(status, "network_failed")


if __name__ == "__main__":
    unittest.main()
