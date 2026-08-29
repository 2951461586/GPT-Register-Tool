import threading
import unittest
from unittest.mock import patch

from sms_tool.batch_runner import run_batch_impl, select_registration_proxy_base, select_registration_proxy_pool
from sms_tool.storage import _status


class BatchErrorClassificationTests(unittest.TestCase):
    def test_retryable_network_failure_gets_fresh_proxy_and_second_attempt(self):
        calls = []

        def fake_run_email(**kwargs):
            calls.append(kwargs["proxy"])
            if len(calls) == 1:
                return {"success": False, "error": "sentinel_extract_failed", "failure_class": "network"}
            return {"success": True, "email": "retry@example.com"}

        with patch("sms_tool.batch_runner.refresh_proxy_sid", side_effect=["proxy-attempt-1", "proxy-attempt-2"]):
            results = run_batch_impl(
                count=1,
                proxy="proxy-base",
                workers=1,
                max_attempts=2,
                retry_delay_seconds=0,
                run_email_func=fake_run_email,
            )

        self.assertEqual(calls, ["proxy-attempt-1", "proxy-attempt-2"])
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["registration_attempts"], 2)

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

    def test_registration_proxy_pool_keeps_all_healthy_static_exits(self):
        pool = [
            "http://user:pass@static-a.example:8080",
            "http://user:pass@static-b.example:8080",
            "http://user:pass@static-c.example:8080",
        ]

        def fake_probe(proxy, *_args, **_kwargs):
            return {"ok": "static-b" not in proxy}

        with patch("sms_tool.batch_runner.probe_proxy_with_scheme_detection", side_effect=fake_probe):
            selected = select_registration_proxy_pool(pool)

        self.assertEqual(selected, [pool[0], pool[2]])

    def test_account_proxy_egress_is_pinned_across_retries(self):
        # Audit #4: rotating the egress on every retry looks like proxy churn
        # to registrars and is a ban trigger.  Each account is now pinned to a
        # stable proxy (account_proxy_index = i % len(pool)); a retry keeps the
        # same egress and only refreshes the sticky session id.
        pool = ["http://static-a.example:8080", "http://static-b.example:8080"]
        calls = []

        def run_email(**kwargs):
            calls.append(kwargs["proxy"])
            if len(calls) == 1:
                return {"success": False, "error": "proxy connection timed out", "failure_class": "network"}
            return {"success": True}

        with patch("sms_tool.batch_runner.select_registration_proxy_pool", return_value=pool), \
             patch("sms_tool.batch_runner.refresh_proxy_sid", side_effect=lambda value: value + "-sid"), \
             patch("sms_tool.batch_runner.CFG", {"email_registration": {}}):
            results = run_batch_impl(
                count=2,
                proxy_pool=pool,
                workers=1,
                max_attempts=2,
                retry_delay_seconds=0,
                run_email_func=run_email,
            )

        # Account 0 is pinned to pool[0] for BOTH attempts; account 1 to pool[1].
        # No retry rotates to a different pool member.
        self.assertEqual(calls, [pool[0] + "-sid", pool[0] + "-sid", pool[1] + "-sid"])
        self.assertTrue(all(result["success"] for result in results))

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

        with patch("sms_tool.batch_runner.CFG", {"email_registration": {}}):
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

    def test_mailbox_timeout_is_not_marked_dropped(self):
        results = run_batch_impl(
            count=1,
            workers=1,
            run_email_func=lambda **_: {
                "success": False,
                "error": "email_otp_poll_timeout",
            },
        )

        self.assertEqual(results[0]["failure_class"], "mailbox")
        self.assertFalse(results[0]["dropped"])

    def test_invalid_auth_state_is_not_marked_dropped(self):
        results = run_batch_impl(
            count=1,
            workers=1,
            run_email_func=lambda **_: {
                "success": False,
                "error": "create_account_failed:invalid_auth_step",
            },
        )

        self.assertEqual(results[0]["failure_class"], "auth_state")
        self.assertFalse(results[0]["dropped"])

    def test_rate_limit_is_not_retried_or_marked_dropped(self):
        calls = []

        def run_email(**_):
            calls.append(1)
            return {
                "success": False,
                "error": "registration_rate_limited:retry_after=300s",
                "failure_class": "rate_limit",
            }

        results = run_batch_impl(
            count=1,
            workers=1,
            max_attempts=2,
            retry_delay_seconds=0,
            run_email_func=run_email,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(results[0]["failure_class"], "rate_limit")
        self.assertFalse(results[0]["dropped"])

    def test_storage_keeps_network_failures_separate_from_dead_accounts(self):
        status = _status(
            {"success": False, "failure_class": "network", "error": "proxy timeout"},
            {},
            "",
            has_refresh_token=False,
        )

        self.assertEqual(status, "network_failed")

    def test_storage_keeps_mailbox_failures_separate_from_dead_accounts(self):
        status = _status(
            {"success": False, "failure_class": "mailbox", "error": "email_otp_poll_timeout"},
            {},
            "",
            has_refresh_token=False,
        )

        self.assertEqual(status, "mailbox_failed")

    def test_storage_keeps_auth_state_failures_separate_from_dead_accounts(self):
        status = _status(
            {"success": False, "failure_class": "auth_state", "error": "invalid_auth_step"},
            {},
            "",
            has_refresh_token=False,
        )

        self.assertEqual(status, "auth_state_failed")

    def test_storage_keeps_rate_limits_separate_from_dead_accounts(self):
        status = _status(
            {"success": False, "failure_class": "rate_limit", "error": "rate_limit_exceeded"},
            {},
            "",
            has_refresh_token=False,
        )

        self.assertEqual(status, "rate_limited")


if __name__ == "__main__":
    unittest.main()
