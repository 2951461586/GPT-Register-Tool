import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_batch


class PaymentCapabilityBatchTests(unittest.TestCase):
    def setUp(self):
        config = patch.object(payment_batch, "CFG", {})
        config.start()
        self.addCleanup(config.stop)

    def test_matrix_checkout_country_is_forwarded_to_capability_probe(self):
        auth = {
            "ok": True,
            "access_token": "secret",
            "auth_context": {"registration_country": "ID"},
            "probed": 1,
        }
        capability = {
            "ok": True,
            "status": "completed",
            "classification": "eligible",
            "eligible": True,
            "conclusive": True,
            "decision": "payment_method_available",
        }
        matrix = {"cells": [{
            "name": "id-gopay",
            "payment_method": "gopay",
            "registration_country": "ID",
            "checkout_country": "ID",
            "provider_country": "ID",
        }]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "normalize_payment_method", return_value="gopay"), \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "probe_payment_method", return_value=capability) as probe, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "probe.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="gopay", probe_only=True, matrix=matrix,
            )
        self.assertEqual(probe.call_args.kwargs["checkout_country"], "ID")
        self.assertEqual(report["matrix"][0]["eligible"], 1)

    def test_gopay_canary_uses_promotion_update_before_zero_due_decision(self):
        class PromotionAwareTransport:
            def __init__(self):
                self.amount = 290_000
                self.calls = []

            def create_checkout(self, request):
                self.calls.append(("checkout", request, self.amount))
                return {
                    "checkout_session_id": "cs_test_gopay_batch_probe",
                    "processor_entity": "openai_ie",
                    "publishable_key": "pk_test_gopay_batch_probe",
                }

            def update_checkout(self, request):
                self.calls.append(("promotion", request, self.amount))
                self.amount = 0
                return {"success": True}

            def stripe_init(self, request):
                self.calls.append(("stripe_init", request, self.amount))
                return {
                    "currency": "idr",
                    "total_summary": {"due": self.amount},
                    "payment_method_types": ["gopay"],
                }

        transport = PromotionAwareTransport()
        auth = {
            "ok": True,
            "access_token": "secret",
            "auth_context": {"registration_country": "ID"},
            "probed": 1,
        }
        checkout_proxy = "http://id-checkout.test:80"
        promotion_proxy = "http://th-promotion.test:80"
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "load_account_seed", return_value=({"registration_country": "ID"}, "")), \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "_record_canary_state", return_value={"paused": False}), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "gopay-canary.json"), \
             patch("sms_tool.payment_capability.payment_method_capability_probe") as generic_probe:
            report = payment_batch.run_payment_batch(
                ["a@example.com"],
                payment_method="gopay",
                proxy=checkout_proxy,
                payment_kwargs={
                    "checkout_proxy": checkout_proxy,
                    "promotion_proxy": promotion_proxy,
                    "transport": transport,
                },
                probe_only=True,
                canary=1,
                retries=0,
            )

        self.assertEqual(
            [(name, amount) for name, _request, amount in transport.calls],
            [("checkout", 290_000), ("promotion", 290_000), ("stripe_init", 0)],
        )
        promotion_context = transport.calls[1][1].transport_context
        self.assertEqual(promotion_context["checkout_proxy"], checkout_proxy)
        self.assertEqual(promotion_context["promotion_proxy"], promotion_proxy)
        self.assertTrue(report["results"][0]["eligible"])
        self.assertEqual(report["results"][0]["decision"], "payment_method_available")
        self.assertEqual(report["results"][0]["amount"], 0)
        generic_probe.assert_not_called()

    def test_unknown_capability_canary_pauses_profile(self):
        report = {
            "probe_only": True,
            "results": [{
                "capability_probed": True,
                "classification": "unknown",
                "decision": "stripe_init_failed",
                "retryable": True,
            }],
            "counts": {},
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "_canary_state_path", return_value=Path(tmp) / "state.json"):
            state = payment_batch._record_canary_state("gopay", report)
        self.assertTrue(state["paused"])
        self.assertEqual(state["capability_probed"], 1)

    def test_conclusive_unavailable_method_does_not_pause_profile(self):
        report = {
            "probe_only": True,
            "results": [{
                "capability_probed": True,
                "classification": "ineligible",
                "conclusive": True,
                "decision": "payment_method_unavailable",
            }],
            "counts": {},
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "_canary_state_path", return_value=Path(tmp) / "state.json"):
            state = payment_batch._record_canary_state("gopay", report)
        self.assertFalse(state["paused"])
        self.assertEqual(state["completed"], 1)


if __name__ == "__main__":
    unittest.main()
