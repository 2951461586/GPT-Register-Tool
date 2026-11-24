"""Offline tests for the protocol-payment egress-country gate."""

import unittest
from dataclasses import dataclass
from unittest.mock import patch

from sms_tool.payment import egress as payment_egress


@dataclass
class _ProbeResult:
    ok: bool
    stage: str = ""
    expected_country: str = ""
    ip: str = ""
    country_code: str = ""
    error: str = ""


def _cfg(enabled: object = True, ttl: int = 600):
    return {
        "chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"},
        "proxy": {"default": "", "pool": []},
        "protocol_payments": {"egress_check": {"enabled": enabled, "cache_ttl_seconds": ttl}},
    }


class EgressGateSettingsTests(unittest.TestCase):
    @staticmethod
    def _settings(enabled: object):
        return payment_egress._gate_settings(
            {"protocol_payments": {"egress_check": {"enabled": enabled, "cache_ttl_seconds": 600}}}
        )

    def test_string_false_disables_the_gate(self):
        for spelling in ("false", "0", "no", "FALSE"):
            with self.subTest(spelling=spelling):
                enabled, *_ = self._settings(spelling)
                self.assertFalse(enabled)

    def test_absent_or_unparsable_value_keeps_the_gate_on(self):
        enabled, *_ = payment_egress._gate_settings({"protocol_payments": {"egress_check": {}}})
        self.assertTrue(enabled)
        enabled, *_ = self._settings("maybe")
        self.assertTrue(enabled)


class EgressGateTests(unittest.TestCase):
    def setUp(self):
        payment_egress.clear_cache()

    def test_mismatch_raises_retryable_error(self):
        calls = []

        def probe(proxy, expected, stage, timeout):
            calls.append((proxy, expected, stage))
            return _ProbeResult(ok=False, country_code="VN", error="country_mismatch:VN")

        options = {
            "checkout_proxy": "http://u:p-CC@gate.kookeey.info:1000",
            "stage_proxy_countries": {"checkout": "TH"},
        }
        with patch.object(payment_egress, "_default_probe", probe):
            with self.assertRaises(payment_egress.EgressCheckError) as ctx:
                payment_egress.assert_egress_countries(options, _cfg(), probe=probe)
        self.assertEqual(ctx.exception.error_code, "egress_country_mismatch")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.observed_country, "VN")
        self.assertEqual(calls, [(options["checkout_proxy"], "TH", "checkout")])

    def test_probe_failure_maps_to_probe_failed_code(self):
        def probe(proxy, expected, stage, timeout):
            return _ProbeResult(ok=False, error="proxy_probe_failed:HTTP 407")

        options = {
            "approve_proxy": "http://u:p-CC@gate.kookeey.info:1000",
            "stage_proxy_countries": {"approve": "TH"},
        }
        with self.assertRaises(payment_egress.EgressCheckError) as ctx:
            payment_egress.assert_egress_countries(options, _cfg(), probe=probe)
        self.assertEqual(ctx.exception.error_code, "egress_probe_failed")
        self.assertTrue(ctx.exception.retryable)

    def test_disabled_gate_never_probes(self):
        def probe(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("probe must not run when the gate is disabled")

        options = {
            "checkout_proxy": "http://u:p-CC@gate.kookeey.info:1000",
            "stage_proxy_countries": {"checkout": "TH"},
        }
        payment_egress.assert_egress_countries(options, _cfg(enabled=False), probe=probe)

    def test_stage_without_expectation_is_skipped(self):
        calls = []

        def probe(proxy, expected, stage, timeout):
            calls.append((proxy, expected, stage))
            return _ProbeResult(ok=True, country_code="TH")

        options = {"checkout_proxy": "http://u:p-CC@gate.kookeey.info:1000"}
        payment_egress.assert_egress_countries(options, _cfg(), probe=probe)
        self.assertEqual(calls, [])

    def test_matching_country_passes_and_is_cached(self):
        calls = []

        def probe(proxy, expected, stage, timeout):
            calls.append((proxy, expected, stage))
            return _ProbeResult(ok=True, country_code="TH")

        options = {
            "checkout_proxy": "http://u:p-CC-a@gate.kookeey.info:1000",
            "approve_proxy": "http://u:p-CC-b@gate.kookeey.info:1000",
            "stage_proxy_countries": {"checkout": "TH", "approve": "TH"},
        }
        payment_egress.assert_egress_countries(options, _cfg(), probe=probe)
        payment_egress.assert_egress_countries(options, _cfg(), probe=probe)
        self.assertEqual(len(calls), 2)

    def test_identical_stage_proxy_probed_once(self):
        calls = []

        def probe(proxy, expected, stage, timeout):
            calls.append(stage)
            return _ProbeResult(ok=True, country_code="TH")

        shared = "http://u:p-CC@gate.kookeey.info:1000"
        options = {
            "checkout_proxy": shared,
            "approve_proxy": shared,
            "stage_proxy_countries": {"checkout": "TH", "approve": "TH"},
        }
        payment_egress.assert_egress_countries(options, _cfg(), probe=probe)
        self.assertEqual(calls, ["checkout"])

    def test_stripe_init_egress_is_checked(self):
        calls = []

        def probe(proxy, expected, stage, timeout):
            calls.append((proxy, expected, stage))
            return _ProbeResult(ok=True, country_code=expected)

        options = {
            "stripe_init_proxy": "http://u:p-CC@gate.kookeey.info:1000",
            "stage_proxy_countries": {"stripe_init": "VN"},
        }
        payment_egress.assert_egress_countries(
            options,
            _cfg(),
            probe=probe,
            stages=("stripe_init",),
        )
        self.assertEqual(calls, [(options["stripe_init_proxy"], "VN", "stripe_init")])


class ProbeCacheBoundTests(unittest.TestCase):
    def setUp(self):
        payment_egress.clear_cache()

    def tearDown(self):
        payment_egress.clear_cache()

    def test_cache_stays_bounded_under_proxy_rotation(self):
        def probe(proxy, expected, stage, timeout):
            return _ProbeResult(ok=True, country_code=expected)

        with patch.object(payment_egress, "_PROBE_CACHE_LIMIT", 3):
            for index in range(10):
                payment_egress._cached_probe(probe, f"http://10.0.0.{index}:8080", "US", "checkout", 1.0, 600.0, 180.0)
        self.assertLessEqual(len(payment_egress._probe_cache), 3)

    def test_prune_drops_expired_entries_before_the_oldest_live_one(self):
        with patch.object(payment_egress, "_PROBE_CACHE_LIMIT", 2):
            payment_egress._probe_cache[("stale", "US")] = (100.0, (False, "", "boom"))
            payment_egress._probe_cache[("live", "US")] = (100.0, (True, "US", ""))
            with payment_egress._cache_lock:
                payment_egress._prune_locked(699.0, ttl=600.0, failure_cooldown=180.0)
        self.assertNotIn(("stale", "US"), payment_egress._probe_cache)
        self.assertIn(("live", "US"), payment_egress._probe_cache)


if __name__ == "__main__":
    unittest.main()
