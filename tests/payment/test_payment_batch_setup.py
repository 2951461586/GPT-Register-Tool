"""Stage-pool precedence for payment batches.

``apply_configured_stage_pools`` decides *which* checkout/approve pool a batch
runs on. Three sources compete and the order is a contract, not an
implementation detail: a pool already on the kwargs (an explicit caller
decision) must win over ``protocol_payments.methods.<method>``, which must win
over the legacy ``<method>`` block. The 2026-10-22 coverage pass found the
"caller already set a pool" branch was the one line in this module nothing
exercised, which is exactly the branch that silently rewrites an explicit
choice if it regresses.
"""

from __future__ import annotations

import unittest

from sms_tool.payment.batch_setup import apply_configured_stage_pools

CHECKOUT_POOL = "http://u:p@1.2.3.4:8080"
APPROVE_POOL = "http://u:p@5.6.7.8:9090"
CALLER_POOL = "http://u:p@9.9.9.9:1111"
LEGACY_POOL = "http://u:p@2.2.2.2:2222"


def _kwargs(**extra):
    return {"payment_method": "paypal", **extra}


class CallerDecisionWinsTests(unittest.TestCase):
    def test_existing_pool_on_the_kwargs_is_never_overwritten(self) -> None:
        kwargs = _kwargs(checkout_proxy_pool=CALLER_POOL, checkout_proxy="http://u:p@9.9.9.9:1111")
        result = apply_configured_stage_pools(
            kwargs,
            protocol_config={"methods": {"paypal": {"checkout_proxy_pool": CHECKOUT_POOL}}},
            method="paypal",
        )
        self.assertEqual(result["checkout_proxy_pool"], CALLER_POOL)
        # An explicitly supplied pool keeps its paired single-proxy field: the
        # pop below only fires when this function is the one that set the pool.
        self.assertIn("checkout_proxy", result)

    def test_existing_approve_pool_does_not_block_the_checkout_pool(self) -> None:
        """The two stages are independent; a decision on one must not skip the other."""
        kwargs = _kwargs(approve_proxy_pool=CALLER_POOL)
        result = apply_configured_stage_pools(
            kwargs,
            protocol_config={"methods": {"paypal": {"checkout_proxy_pool": CHECKOUT_POOL}}},
            method="paypal",
        )
        self.assertEqual(result["approve_proxy_pool"], CALLER_POOL)
        self.assertEqual(result["checkout_proxy_pool"], CHECKOUT_POOL)


class PrecedenceTests(unittest.TestCase):
    def test_method_config_beats_the_legacy_block(self) -> None:
        result = apply_configured_stage_pools(
            _kwargs(),
            protocol_config={"methods": {"paypal": {"checkout_proxy_pool": CHECKOUT_POOL}}},
            legacy_config={"paypal": {"checkout_proxy_pool": LEGACY_POOL}},
            method="paypal",
        )
        self.assertEqual(result["checkout_proxy_pool"], CHECKOUT_POOL)

    def test_legacy_block_is_the_fallback(self) -> None:
        result = apply_configured_stage_pools(
            _kwargs(),
            protocol_config={"methods": {"paypal": {}}},
            legacy_config={"paypal": {"approve_proxy_pool": LEGACY_POOL}},
            method="paypal",
        )
        self.assertEqual(result["approve_proxy_pool"], LEGACY_POOL)

    def test_a_pool_set_here_drops_the_paired_single_proxy(self) -> None:
        """``*_proxy_pool`` and ``*_proxy`` are exclusive; leaving both lets an adapter pick either."""
        result = apply_configured_stage_pools(
            _kwargs(checkout_proxy="http://u:p@9.9.9.9:1111"),
            protocol_config={"methods": {"paypal": {"checkout_proxy_pool": CHECKOUT_POOL}}},
            method="paypal",
        )
        self.assertEqual(result["checkout_proxy_pool"], CHECKOUT_POOL)
        self.assertNotIn("checkout_proxy", result)

    def test_an_unparsable_configured_pool_is_not_applied(self) -> None:
        result = apply_configured_stage_pools(
            _kwargs(),
            protocol_config={"methods": {"paypal": {"checkout_proxy_pool": "not-a-proxy"}}},
            method="paypal",
        )
        self.assertNotIn("checkout_proxy_pool", result)


class ShortCircuitTests(unittest.TestCase):
    def test_an_explicit_transport_disables_pool_injection(self) -> None:
        result = apply_configured_stage_pools(
            _kwargs(transport="direct"),
            protocol_config={"methods": {"paypal": {"checkout_proxy_pool": CHECKOUT_POOL}}},
            method="paypal",
        )
        self.assertNotIn("checkout_proxy_pool", result)

    def test_an_explicit_proxy_argument_disables_pool_injection(self) -> None:
        result = apply_configured_stage_pools(
            _kwargs(),
            protocol_config={"methods": {"paypal": {"checkout_proxy_pool": CHECKOUT_POOL}}},
            method="paypal",
            proxy=CALLER_POOL,
        )
        self.assertNotIn("checkout_proxy_pool", result)

    def test_transport_none_is_not_an_explicit_transport(self) -> None:
        result = apply_configured_stage_pools(
            _kwargs(transport=None),
            protocol_config={"methods": {"paypal": {"checkout_proxy_pool": CHECKOUT_POOL}}},
            method="paypal",
        )
        self.assertEqual(result["checkout_proxy_pool"], CHECKOUT_POOL)


class ShapeToleranceTests(unittest.TestCase):
    """Config is operator-edited JSON; a wrong shape must degrade, not raise."""

    def test_non_mapping_configs_are_ignored(self) -> None:
        for protocol_config in ({}, {"methods": None}, {"methods": []}, {"methods": {"paypal": "nope"}}):
            with self.subTest(protocol_config=protocol_config):
                result = apply_configured_stage_pools(
                    _kwargs(),
                    protocol_config=protocol_config,
                    legacy_config="nope",  # type: ignore[arg-type]
                    method="paypal",
                )
                self.assertNotIn("checkout_proxy_pool", result)

    def test_input_mapping_is_not_mutated(self) -> None:
        kwargs = _kwargs()
        apply_configured_stage_pools(
            kwargs,
            protocol_config={"methods": {"paypal": {"checkout_proxy_pool": CHECKOUT_POOL}}},
            method="paypal",
        )
        self.assertEqual(kwargs, {"payment_method": "paypal"})


if __name__ == "__main__":
    unittest.main()
