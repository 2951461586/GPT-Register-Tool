import threading
import unittest
from unittest.mock import patch

from sms_tool.batch_runner import run_batch_impl, select_registration_proxy_base
from sms_tool.storage import _status


class BatchErrorClassificationTests(unittest.TestCase):
    def test_registration_proxy_falls_back_from_kookeey_to_cliproxy(self):
        pool = [
            "http://user:base-JP-12345678-5m@gate.kookeey.info:1000",
            "http://user-region-JP-sid-ABCDEFGH-t-5:pass@sg.cliproxy.io:443",
            "http://user-region-JP-sid-ABCDEFGH-t-10:pass@as.zooproxy.com:443",
        ]

        def fake_probe(proxy, *_args, **_kwargs):
            return {"ok": "sg.cliproxy.io" in proxy}

        with patch("sms_tool.batch_runner.probe_proxy_with_scheme_detection", side_effect=fake_probe):
            selected = select_registration_proxy_base(pool)

        self.assertEqual(selected, pool[1])

    def test_registration_proxy_falls_back_from_cliproxy_to_zoorproxy(self):
        pool = [
            "http://user-region-JP-sid-ABCDEFGH-t-5:pass@sg.cliproxy.io:443",
            "http://user-region-JP-sid-ABCDEFGH-t-10:pass@as.zooproxy.com:443",
        ]

        def fake_probe(proxy, *_args, **_kwargs):
            return {"ok": "as.zooproxy.com" in proxy}

        with patch("sms_tool.batch_runner.probe_proxy_with_scheme_detection", side_effect=fake_probe):
            selected = select_registration_proxy_base(pool)

        self.assertEqual(selected, pool[1])

    def test_dynamic_registration_proxy_refreshes_sid_per_account(self):
        proxies = []

        def run_email(**kwargs):
            proxies.append(kwargs.get("proxy"))
            return {"success": True}

        source = "http://user-region-US-sid-ABCDEFGH-t-5:pass@proxy.example:8080"
        results = run_batch_impl(
            count=2,
            proxy=source,
            workers=1,
            run_email_func=run_email,
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(len(proxies), 2)
        self.assertTrue(all(proxy.startswith("http://user-region-US-sid-") for proxy in proxies))
        self.assertTrue(all("sid-ABCDEFGH" not in proxy for proxy in proxies))
        self.assertNotEqual(proxies[0], proxies[1])

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
