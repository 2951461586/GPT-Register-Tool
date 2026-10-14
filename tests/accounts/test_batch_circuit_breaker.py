"""Unit tests for the batch-level environment circuit breaker (P2-2)."""

import unittest

from sms_tool.batch_circuit_breaker import (
    DEFAULT_THRESHOLD,
    ENVIRONMENT_FAILURE_CLASSES,
    BatchCircuitBreaker,
)


class TestBatchCircuitBreaker(unittest.TestCase):
    def test_closed_until_the_threshold_is_reached(self):
        breaker = BatchCircuitBreaker(3)
        self.assertFalse(breaker.record("network"))
        self.assertFalse(breaker.record("network"))
        self.assertTrue(breaker.record("network"))
        self.assertEqual(breaker.consecutive, 3)

    def test_trips_on_the_nth_consecutive_failure_for_any_threshold(self):
        for threshold in (1, 2, 5):
            with self.subTest(threshold=threshold):
                breaker = BatchCircuitBreaker(threshold)
                outcomes = [breaker.record("network") for _ in range(threshold)]
                self.assertEqual(outcomes, [False] * (threshold - 1) + [True])

    def test_a_success_resets_the_streak(self):
        breaker = BatchCircuitBreaker(3)
        breaker.record("network")
        breaker.record("network")
        breaker.record_success()
        self.assertFalse(breaker.record("network"))
        self.assertFalse(breaker.record("network"))
        self.assertTrue(breaker.record("network"))

    def test_a_per_account_verdict_resets_the_streak(self):
        # account/mailbox/rate_limit prove the environment can still reach the
        # target, so they must not count towards -- and must clear -- the streak.
        breaker = BatchCircuitBreaker(3)
        breaker.record("network")
        breaker.record("network")
        for cls in ("account", "mailbox", "rate_limit", "unknown", "", None):
            with self.subTest(cls=cls):
                breaker.record(cls)
                self.assertEqual(breaker.consecutive, 0)
                self.assertFalse(breaker.tripped)
                breaker.record("network")

    def test_environment_classes_are_exactly_the_retryable_ones(self):
        # Deliberately not a hand-maintained duplicate: the retry policy already
        # defines "the environment failed, a retry might help".
        self.assertEqual(ENVIRONMENT_FAILURE_CLASSES, {"network", "auth_state"})

    def test_auth_state_also_counts_as_environment(self):
        breaker = BatchCircuitBreaker(2)
        breaker.record("auth_state")
        self.assertTrue(breaker.record("auth_state"))

    def test_class_matching_is_case_and_space_insensitive(self):
        breaker = BatchCircuitBreaker(2)
        breaker.record(" Network ")
        self.assertTrue(breaker.record("NETWORK"))

    def test_a_tripped_breaker_never_heals_mid_batch(self):
        breaker = BatchCircuitBreaker(2)
        breaker.record("network")
        breaker.record("network")
        self.assertTrue(breaker.tripped)
        breaker.record_success()
        self.assertTrue(breaker.tripped)
        self.assertTrue(breaker.record("network"))

    def test_tripped_class_is_reported_only_while_open(self):
        breaker = BatchCircuitBreaker(2)
        self.assertEqual(breaker.tripped_class, "")
        breaker.record("auth_state")
        self.assertEqual(breaker.tripped_class, "")
        breaker.record("auth_state")
        self.assertEqual(breaker.tripped_class, "auth_state")

    def test_disabled_breaker_never_trips(self):
        breaker = BatchCircuitBreaker(1, enabled=False)
        for _ in range(10):
            self.assertFalse(breaker.record("network"))
        self.assertFalse(breaker.tripped)
        self.assertEqual(breaker.consecutive, 0)

    def test_non_positive_thresholds_clamp_to_one(self):
        for value in (0, -5):
            with self.subTest(value=value):
                breaker = BatchCircuitBreaker(value)
                self.assertEqual(breaker.threshold, 1)
                self.assertTrue(breaker.record("network"))

    def test_junk_threshold_falls_back_to_the_default(self):
        # A junk value must not silently disable the breaker.
        # (A float is not junk -- it truncates, same as the config parser.)
        for value in ("abc", None, "", []):
            with self.subTest(value=value):
                self.assertEqual(BatchCircuitBreaker(value).threshold, DEFAULT_THRESHOLD)
        self.assertEqual(DEFAULT_THRESHOLD, 3)

    def test_concurrent_records_cannot_lose_a_failure(self):
        # The breaker is shared by every worker, so the counter must be atomic.
        # A threshold above the total keeps it closed, so every increment counts.
        import threading

        breaker = BatchCircuitBreaker(1000)
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            for _ in range(10):
                breaker.record("network")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(breaker.consecutive, 80)


if __name__ == "__main__":
    unittest.main()
