"""Tests for services/protocol-payment/common/stripe_flow.py.

The four URL/entity builders are pure and asserted directly. The parameterized
``resolve_confirm_payload`` is driven through injected fakes to pin its control
flow (direct redirect / approval path / poll fallback) without standing up a
real extractor.
"""

import sys
import unittest
from pathlib import Path

# Load as part of the ``common`` package so ``from . import endpoints`` resolves.
_COMMON_DIR = (
    Path(__file__).resolve().parents[1] / "services" / "protocol-payment"
)
if str(_COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(_COMMON_DIR))

import common.endpoints  # noqa: E402,F401  (registers the package)
from common import stripe_flow as FLOW  # noqa: E402


def _nl(value):
    return str(value or "").strip().upper() or "NL"


class UrlBuilderTests(unittest.TestCase):
    def test_processor_entity_explicit_wins(self):
        self.assertEqual(
            FLOW.processor_entity_for_country("NL", "openai_x", normalize_country=_nl), "openai_x"
        )

    def test_processor_entity_us_vs_other(self):
        self.assertEqual(FLOW.processor_entity_for_country("us", normalize_country=_nl), "openai_llc")
        self.assertEqual(FLOW.processor_entity_for_country("NL", normalize_country=_nl), "openai_ie")

    def test_to_openai_pay_url_rewrites_host(self):
        self.assertEqual(
            FLOW.to_openai_pay_url("https://checkout.stripe.com/c/pay/cs_1?a=b"),
            "https://pay.openai.com/c/pay/cs_1?a=b",
        )
        self.assertEqual(FLOW.to_openai_pay_url(""), "")
        self.assertEqual(FLOW.to_openai_pay_url("https://other.example/x"), "https://other.example/x")

    def test_checkout_long_url_embeds_return_url(self):
        url = FLOW.stripe_checkout_long_url("cs_9", "NL", "", normalize_country=_nl)
        self.assertIn("checkout.stripe.com/c/pay/cs_9", url)
        self.assertIn("return_url=", url)
        self.assertIn("openai_ie", url)

    def test_confirm_return_url_sets_success_param(self):
        url = FLOW.stripe_confirm_return_url(
            "cs_1", {"billing_country": "NL", "processor_entity": ""},
            "https://pay.openai.com/c/pay/cs_1",
            normalize_country=_nl, default_country="NL",
        )
        self.assertIn("success_return_url=", url)


class ResolveConfirmPayloadTests(unittest.TestCase):
    def _run(self, payload, redirect_from_intent=""):
        calls = {"approved": 0, "polled": 0}
        logs = []

        def approve_with_retry(*a, **k):
            calls["approved"] += 1
            return "approve-proxy"

        def poll_payment_page(*a, **k):
            calls["polled"] += 1
            return "polled-url", ["qr-poll"]

        result = FLOW.resolve_confirm_payload(
            None, payload, {}, "pk", {}, "pm_1",
            "at", "did", "st", "cp", "pp", [],
            provider_label="iDEAL", redirect_label="扫码/授权 URL", no_redirect_note="redirect/QR",
            raise_if_setup_intent_blocked=lambda *a, **k: None,
            extract_redirect_url=lambda p: p.get("redirect", ""),
            stripe_payload_intent_redirect_url=lambda *a, **k: redirect_from_intent,
            extract_qr_candidates=lambda p: list(p.get("qr", [])),
            find_submission_attempt=lambda p: dict(p.get("submission", {})),
            approve_proxy_candidates=lambda *a: ["a", "b"],
            approve_with_retry=approve_with_retry,
            poll_payment_page=poll_payment_page,
            log=lambda *a, **k: logs.append(a[0] if a else ""),
        )
        return result, calls, logs

    def test_direct_redirect_short_circuits(self):
        (url, qr, approve), calls, _ = self._run({"redirect": "https://bank/pay"})
        self.assertEqual(url, "https://bank/pay")
        self.assertEqual(approve, "")
        self.assertEqual(calls, {"approved": 0, "polled": 0})

    def test_requires_approval_approves_then_polls(self):
        (url, qr, approve), calls, _ = self._run({"submission": {"state": "requires_approval"}})
        self.assertEqual(url, "polled-url")
        self.assertEqual(approve, "approve-proxy")
        self.assertEqual(calls, {"approved": 1, "polled": 1})
        self.assertIn("qr-poll", qr)

    def test_no_redirect_no_qr_falls_back_to_poll(self):
        (url, qr, approve), calls, _ = self._run({})
        self.assertEqual(url, "polled-url")
        self.assertEqual(approve, "")
        self.assertEqual(calls["polled"], 1)

    def test_qr_candidates_deduplicated(self):
        (url, qr, _), _, _ = self._run({"qr": ["a", "a", "b"], "redirect": "https://x"})
        self.assertEqual(qr, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
