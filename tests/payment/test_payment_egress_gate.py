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

        self.assertEqual(
            calls,
            [(options["stripe_init_proxy"], "VN", "stripe_init")],
        )

    def test_protocol_script_adapter_blocks_on_mismatch_without_spawning(self):
        # ``_run_protocol_script`` consumes ``PaymentMethodSpec``, owned by
        # ``pay_link.base`` -- not the catalog's ``PaymentMethodDefinition``.
        from sms_tool.pay_link.base import PAYMENT_METHODS

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


class FunctionAdapterEgressTests(unittest.TestCase):
    """In-process adapters assert the same contract the extractors always did.

    ``native_upi`` is the deliberate exception: its gate runs inside the pipeline
    *after* the region retarget, because gating at the runner would probe the
    pre-retarget credential and wrongly reject a region-tagged pool.
    """

    def setUp(self):
        payment_egress.clear_cache()

    @staticmethod
    def _options():
        return {
            "checkout_proxy": "http://u:p@exit.example:8080",
            "stage_proxy_countries": {"checkout": "TH"},
        }

    def test_helper_returns_canonical_failure_on_mismatch(self):
        from sms_tool.pay_link.registry import _assert_function_adapter_egress

        def probe(proxy, expected, stage, timeout):
            return _ProbeResult(ok=False, country_code="VN", error="country_mismatch:VN")

        with patch.object(payment_egress, "_default_probe", probe):
            result = _assert_function_adapter_egress("gopay", self._options(), _cfg())

        assert result is not None
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "egress_country_mismatch")
        self.assertEqual(result["payment_method"], "gopay")
        self.assertEqual(result["error_stage"], "preparing_proxy")

    def test_helper_passes_when_the_exit_matches(self):
        from sms_tool.pay_link.registry import _assert_function_adapter_egress

        def probe(proxy, expected, stage, timeout):
            return _ProbeResult(ok=True, country_code=expected)

        with patch.object(payment_egress, "_default_probe", probe):
            self.assertIsNone(_assert_function_adapter_egress("gopay", self._options(), _cfg()))

    def test_helper_is_a_no_op_when_the_gate_is_disabled(self):
        from sms_tool.pay_link.registry import _assert_function_adapter_egress

        def probe(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("probe must not run when the gate is disabled")

        with patch.object(payment_egress, "_default_probe", probe):
            self.assertIsNone(_assert_function_adapter_egress("gopay", self._options(), _cfg(enabled=False)))

    def test_every_in_process_runner_is_gated_except_upi(self):
        import pathlib

        from sms_tool.pay_link import registry

        source = pathlib.Path(registry.__file__).read_text(encoding="utf-8")
        for runner in ("paypal_runner", "wallet_runner", "gcash_runner", "regional_wallet_runner"):
            body = source.split(f"def {runner}(")[1].split("\n    def ")[0]
            self.assertIn("_assert_function_adapter_egress", body, f"{runner} is not egress-gated")

        # Subprocess adapters gate in ``_prepare_extractor``; UPI gates post-retarget
        # inside the pipeline.  Neither may be double-gated at the runner.
        for runner in ("upi_runner", "script_runner", "direct_runner", "momo_runner"):
            body = source.split(f"def {runner}(")[1].split("\n    def ")[0]
            self.assertNotIn("_assert_function_adapter_egress", body, f"{runner} is double-gated")

        pipeline = pathlib.Path(registry.__file__).parent.parent / "upi_link" / "pipeline.py"
        self.assertIn("_upi_assert_egress_contract", pipeline.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
