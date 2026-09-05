import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_link_manager as manager

CONFIG = "sms_tool.pay_link.base.current_config_data"


def _overlay(**overrides):
    """Reproduce what ``patch.dict(manager.CFG, overrides, clear=False)`` did.

    ``CFG`` was a module-level dict in ``pay_link/base.py`` that no production
    code ever read - it existed purely as a patch target, and ``_config_data()``
    merged it over the real config. With CFG gone the merge has to happen here,
    at ``current_config_data``, the seam the production code actually reads.
    Merging (not replacing) matters: these tests override one nested branch such
    as ``protocol_payments.methods.gopay`` and rely on the rest of the real
    config still being visible.  Do NOT try to "clean up" the merged config by
    emptying ``protocol_payments.proxy_pools``: ``stage_routes`` reference those
    pools **by name**, and ``validate_config`` rejects a config whose routes
    point at unknown pools.

    Consequence to keep in mind: the merged config carries the real proxy pool,
    so any test that neither injects a ``transport`` nor passes an explicit
    ``proxy`` will make the route planner probe the real paid endpoints.
    ``tests/conftest.py::_no_real_proxy_probe`` turns that into an immediate
    failure instead of a weather-dependent flake -- see the comment there.
    """
    from sms_tool.pay_link.base import current_config_data

    def merge(base, extra):
        merged = dict(base) if isinstance(base, Mapping) else {}
        for key, value in extra.items():
            current = merged.get(key)
            if isinstance(value, Mapping) and isinstance(current, Mapping):
                merged[key] = merge(current, value)
            else:
                merged[key] = value
        return merged

    return patch(CONFIG, return_value=merge(current_config_data(), overrides))


class WalletManagerIntegrationTests(unittest.TestCase):
    def test_gopay_defaults_to_zero_due_promotion_with_injected_transport(self):
        class ProbeTransport:
            def __init__(self):
                self.calls = []

            def create_checkout(self, request):
                self.calls.append(("checkout", request))
                return {
                    "checkout_session_id": "cs_test_gopay_manager_fixture",
                    "processor_entity": "openai_ie",
                    "publishable_key": "pk_test_gopay_manager_fixture",
                }

            def update_checkout(self, request):
                self.calls.append(("promotion", request))
                return {"success": True}

            def stripe_init(self, request):
                self.calls.append(("stripe_init", request))
                return {
                    "currency": "idr",
                    "total_summary": {"due": 0},
                    "payment_method_types": ["gopay"],
                }

        transport = ProbeTransport()
        config = {
            "protocol_payments": {
                "methods": {
                    "gopay": {
                        "promotion_proxy": "http://promotion-th.test:80",
                        "stage_proxy_countries": {"promotion": "TH", "approve": "ID"},
                    }
                }
            }
        }
        with _overlay(**config):
            result = manager._run_wallet_adapter(
                manager.PAYMENT_METHODS["gopay"],
                "fixture-access-token",
                transport=transport,
                probe_only=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual([name for name, _ in transport.calls], ["checkout", "promotion", "stripe_init"])
        promotion_context = transport.calls[1][1].transport_context
        self.assertEqual(promotion_context["promotion_proxy"], "http://promotion-th.test:80")
        self.assertEqual(promotion_context["stage_proxy_countries"]["promotion"], "TH")
        self.assertTrue(promotion_context["rotate_proxy_sessions"])

    def test_wallet_adapter_result_uses_common_manager_contract(self):
        adapter_result = {
            "ok": True,
            "status": "completed",
            "operation": "extract_link",
            "url": "https://app.midtrans.com/snap/v4/redirection/fixture",
            "provider_redirect_url": "https://app.midtrans.com/snap/v4/redirection/fixture",
            "link_type": "gopay_protocol",
        }
        # An injected transport is what keeps this test off the network: it sets
        # `ignore_configured_routes`, so the planner ignores the real
        # `stage_routes` / named pools and ends up with an empty pool, which it
        # never probes.  Without it the merged real config hands the planner the
        # live paid proxy pool (us.ipwo.net) and the test probes it for real --
        # it only "passed" before because those probes usually succeed.
        #
        # Note `proxy="..."` does NOT work here: it binds to the `default_proxy`
        # parameter, not to `options`, so it neither marks the route explicit nor
        # empties the pool -- it just becomes the single candidate and gets
        # probed.
        #
        # The transport itself is unused: registry._run_wallet_adapter is mocked.
        with tempfile.TemporaryDirectory() as tmp, \
             _overlay(protocol_payments={"enabled_methods": ["gopay"]}), \
             patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"), \
             patch("sms_tool.pay_link.registry._run_wallet_adapter", return_value=adapter_result) as adapter:
            result = manager.generate_payment_link(
                "token",
                payment_method="gopay",
                transport=object(),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["manager_state"], "completed")
        self.assertEqual(result["error_stage"], "")
        adapter.assert_called_once()

    def test_public_gopay_probe_updates_nonzero_checkout_before_classification(self):
        class PromotionAwareProbeTransport:
            def __init__(self):
                self.amount = 290_000
                self.calls = []

            def create_checkout(self, request):
                self.calls.append(("checkout", request, self.amount))
                return {
                    "checkout_session_id": "cs_test_gopay_public_probe",
                    "processor_entity": "openai_ie",
                    "publishable_key": "pk_test_gopay_public_probe",
                }

            def update_checkout(self, request):
                self.calls.append(("promotion", request, self.amount))
                self.amount = 0
                return {"success": True, "total_summary": {"due": self.amount}}

            def stripe_init(self, request):
                self.calls.append(("stripe_init", request, self.amount))
                return {
                    "currency": "idr",
                    "total_summary": {"due": self.amount},
                    "payment_method_types": ["gopay"],
                }

        transport = PromotionAwareProbeTransport()
        config = {
            "protocol_payments": {
                "enabled_methods": ["gopay"],
                "methods": {
                    "gopay": {
                        "checkout_proxy": "http://id-checkout.test:80",
                        "promotion_proxy": "http://th-promotion.test:80",
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp, \
             _overlay(**config), \
             patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"), \
             patch("sms_tool.payment_capability.payment_method_capability_probe") as generic_probe:
            result = manager.generate_payment_link(
                "token",
                payment_method="gopay",
                probe_only=True,
                transport=transport,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["manager_state"], "completed")
        self.assertEqual(result["operation"], "payment_method_capability_probe")
        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["amount"], 0)
        self.assertEqual(
            [(name, amount) for name, _request, amount in transport.calls],
            [("checkout", 290_000), ("promotion", 290_000), ("stripe_init", 0)],
        )
        promotion_context = transport.calls[1][1].transport_context
        self.assertEqual(promotion_context["checkout_proxy"], "http://id-checkout.test:80")
        self.assertEqual(promotion_context["promotion_proxy"], "http://th-promotion.test:80")
        generic_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
