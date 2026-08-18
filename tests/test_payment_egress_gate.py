"""Offline tests for the protocol-payment egress-country gate."""

import unittest
from dataclasses import dataclass
from unittest.mock import patch

from sms_tool import payment_egress
from sms_tool.payment_link_manager import _run_protocol_script


@dataclass
class _ProbeResult:
    ok: bool
    stage: str = ""
    expected_country: str = ""
    ip: str = ""
    country_code: str = ""
    error: str = ""


def _cfg(enabled=True, ttl=600):
    return {
        "chatgpt": {"auth_base_url": "https://auth.openai.com", "chat_base_url": "https://chatgpt.com"},
        "proxy": {"default": "", "pool": []},
        "protocol_payments": {"egress_check": {"enabled": enabled, "cache_ttl_seconds": ttl}},
    }


class EgressGateTests(unittest.TestCase):
    def setUp(self):
        payment_egress.clear_cache()

    def test_mismatch_raises_retryable_error_before_subprocess(self):
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
        # Distinct stage proxies probed once each; the second pass hits the cache.
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
        # Same proxy + same expectation is one cache entry across stages.
        self.assertEqual(calls, ["checkout"])

    def test_protocol_script_adapter_blocks_on_mismatch_without_spawning(self):
        from sms_tool.payment_catalog import PAYMENT_METHODS

        def probe(proxy, expected, stage, timeout):
            return _ProbeResult(ok=False, country_code="VN", error="country_mismatch:VN")

        spec = PAYMENT_METHODS["ideal"]
        with patch.object(payment_egress, "_default_probe", probe):
            with patch("sms_tool.payment_link_manager._run_extractor_subprocess") as run_sub:
                result = _run_protocol_script(
                    spec,
                    "token",
                    checkout_proxy="http://u:p-CC@gate.kookeey.info:1000",
                    stage_proxy_countries={"checkout": "TH"},
                    runtime_config=_cfg(),
                )
        run_sub.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "egress_country_mismatch")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["error_stage"], "preparing_proxy")
        self.assertEqual(result["payment_method"], "ideal")


if __name__ == "__main__":
    unittest.main()
