"""Offline tests for the UPI lane's payment egress gate.

``native_upi`` is an in-process adapter, so it never passed through
``pay_link.adapters._prepare_extractor`` -- the only egress-country guard for
the UPI lane used to be the hand-run probe documented in ``PROXY_GUIDE.md``.
:func:`sms_tool.upi_link.pipeline.generate_upi_qr_link` now asserts the shared
gate on the three proxies it retargeted, before Stage 1 creates a Checkout
session.

``generate_upi_qr_link`` wraps its whole body in a catch-all ``except
Exception`` that returns a failure dict, so a passing gate is proved by the
sentinel raised at the *first stage-1 call* surfacing in that dict -- a raising
``assertRaises`` would be swallowed.
"""

import unittest
from dataclasses import dataclass
from unittest.mock import patch

from sms_tool import payment_egress
from sms_tool.upi_link import pipeline as upi_pipeline

_SENTINEL = "reach-stage-1"


class _StopAfterGate(RuntimeError):
    """Raised at the first stage-1 call so the pipeline stops without network."""


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
        "upi": {
            "checkout_country": "IN",
            "payment_country": "IN",
            "target_country": "IN",
            "billing_regions": ["IN"],
        },
        "protocol_payments": {"egress_check": {"enabled": enabled, "cache_ttl_seconds": ttl}},
    }


def _run(**kwargs):
    """Call the pipeline with stage 1 stubbed to raise ``_StopAfterGate``."""
    with patch.object(upi_pipeline, "_upi_new_chatgpt_session", side_effect=_StopAfterGate(_SENTINEL)):
        return upi_pipeline.generate_upi_qr_link(access_token="token", **kwargs)


class UpiEgressGateTests(unittest.TestCase):
    def setUp(self):
        payment_egress.clear_cache()

    def test_wrong_country_returns_canonical_failure_before_checkout(self):
        calls = []

        def probe(proxy, expected, stage, timeout):
            calls.append((proxy, expected, stage))
            return _ProbeResult(ok=False, country_code="VN", error="country_mismatch:VN")

        with patch.object(payment_egress, "_default_probe", probe):
            result = _run(proxy="http://u:p@exit.example:8080", runtime_config=_cfg())

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "egress_country_mismatch")
        self.assertEqual(result["error_stage"], "preparing_proxy")
        self.assertEqual(result["payment_method"], "upi")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["egress"]["expected_country"], "IN")
        self.assertEqual(result["egress"]["observed_country"], "VN")
        # One probe: all three stages share the same retargeted proxy + country.
        self.assertEqual(calls, [("http://u:p@exit.example:8080", "IN", "checkout")])
        # The gate rejected before stage 1, so the sentinel must be absent.
        self.assertNotIn(_SENTINEL, result.get("error", ""))

    def test_all_three_stage_proxies_are_gated(self):
        calls = []

        def probe(proxy, expected, stage, timeout):
            calls.append(stage)
            return _ProbeResult(ok=True, country_code=expected)

        with patch.object(payment_egress, "_default_probe", probe):
            result = _run(
                checkout_proxy="http://u:p@a.example:8080",
                provider_proxy="http://u:p@b.example:8080",
                approve_proxy="http://u:p@c.example:8080",
                runtime_config=_cfg(),
            )
        # stripe_init is not in the gate's default stage set, so its presence
        # here proves the UPI caller opted it in explicitly.
        self.assertEqual(calls, ["checkout", "stripe_init", "approve"])
        self.assertIn(_SENTINEL, result["error"])

    def test_matching_country_passes_the_gate(self):
        def probe(proxy, expected, stage, timeout):
            return _ProbeResult(ok=True, country_code=expected)

        with patch.object(payment_egress, "_default_probe", probe):
            result = _run(proxy="http://u:p@exit.example:8080", runtime_config=_cfg())

        self.assertIn(_SENTINEL, result["error"])

    def test_disabled_gate_does_not_probe(self):
        def probe(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("probe must not run when the gate is disabled")

        with patch.object(payment_egress, "_default_probe", probe):
            result = _run(proxy="http://u:p@exit.example:8080", runtime_config=_cfg(enabled=False))

        self.assertIn(_SENTINEL, result["error"])

    def test_retargeted_proxy_is_what_gets_probed(self):
        """A region-tagged credential is probed *after* the retarget.

        Gating the pre-retarget proxy would reject a pool the retarget is about
        to make correct -- the regression this placement exists to avoid.
        """
        probed = []

        def probe(proxy, expected, stage, timeout):
            probed.append(proxy)
            return _ProbeResult(ok=True, country_code=expected)

        region_tagged = "http://user-region-JP-session-abc:pass@exit.example:8080"
        with patch.object(payment_egress, "_default_probe", probe):
            result = _run(proxy=region_tagged, runtime_config=_cfg())

        self.assertIn(_SENTINEL, result["error"])
        self.assertEqual(probed, ["http://user-region-IN-session-abc:pass@exit.example:8080"])


if __name__ == "__main__":
    unittest.main()
