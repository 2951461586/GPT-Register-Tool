import unittest
from unittest.mock import patch

from sms_tool.payment_routing import PaymentRoutePlanner


class PaymentRoutePlannerTests(unittest.TestCase):
    def test_named_pools_are_selected_once_and_reused_by_stage(self):
        config = {
            "protocol_payments": {
                "proxy_pools": {
                    "checkout": ["http://user:secret@checkout.example:8080"],
                    "approve": ["http://user:secret@approve.example:8080"],
                },
                "methods": {
                    "gopay": {
                        "stage_routes": {
                            "checkout": {"pool": "checkout", "country": "ID"},
                            "promotion": {"pool": "approve", "country": "TH"},
                            "approve": {"pool": "approve", "country": "JP"},
                        },
                    },
                },
            },
        }
        seen = []

        def select(pool, country, stage, **_kwargs):
            seen.append((list(pool), country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=select), patch(
            "sms_tool.paypal_proxy.rotate_proxy_session", side_effect=lambda proxy, country: f"{proxy}/{country}"
        ):
            plan = PaymentRoutePlanner(config).plan("gopay")

        self.assertEqual([item[2] for item in seen], ["checkout", "approve"])
        self.assertEqual(plan.proxy_for("promotion"), "http://user:secret@approve.example:8080/TH")
        self.assertEqual(plan.proxy_for("approve"), "http://user:secret@approve.example:8080/JP")
        self.assertNotIn("secret", str(plan.public_dict()))

    def test_explicit_shared_proxy_bypasses_configured_pools(self):
        config = {
            "protocol_payments": {
                "methods": {"gopay": {"checkout_proxy_pool": ["http://pool.example:8080"]}}
            }
        }
        with patch("sms_tool.paypal_proxy.select_proxy_from_pool") as select:
            plan = PaymentRoutePlanner(config).plan(
                "gopay", options={"proxy": "http://explicit.example:8080"}
            )
        select.assert_not_called()
        self.assertEqual(plan.checkout_proxy, "http://explicit.example:8080")

    def test_explicit_stage_pool_bypasses_configured_named_route(self):
        config = {
            "protocol_payments": {
                "proxy_pools": {"configured": ["http://configured.example:8080"]},
                "methods": {
                    "paypal": {
                        "stage_routes": {
                            "checkout": {"pool": "configured", "country": "US"},
                        }
                    }
                },
            }
        }
        with patch(
            "sms_tool.paypal_proxy.select_proxy_from_pool",
            side_effect=lambda pool, *_args, **_kwargs: (pool[0], []),
        ):
            plan = PaymentRoutePlanner(config).plan(
                "paypal",
                options={"checkout_proxy_pool": ["http://operator.example:8080"]},
            )

        self.assertEqual(plan.checkout_proxy, "http://operator.example:8080")

    def test_explicit_stage_proxy_bypasses_configured_named_route(self):
        config = {
            "protocol_payments": {
                "proxy_pools": {"configured": ["http://configured.example:8080"]},
                "methods": {
                    "direct_card": {
                        "stage_routes": {
                            "checkout": {"pool": "configured", "country": "US"},
                        }
                    }
                },
            }
        }
        with patch("sms_tool.paypal_proxy.select_proxy_from_pool") as select:
            plan = PaymentRoutePlanner(config).plan(
                "direct_card",
                options={"checkout_proxy": "http://operator.example:8080"},
            )

        select.assert_not_called()
        self.assertEqual(plan.checkout_proxy, "http://operator.example:8080")


if __name__ == "__main__":
    unittest.main()
