import unittest
from unittest.mock import patch

from sms_tool.payment_routing import PaymentRoutePlanner, method_payment_config


class PaymentRoutePlannerTests(unittest.TestCase):
    def test_runtime_country_override_wins_over_configured_route_country(self):
        config = {
            "protocol_payments": {
                "proxy_pools": {"configured": ["http://configured.example:8080"]},
                "methods": {
                    "paypal": {
                        "stage_routes": {
                            "checkout": {"pool": "configured", "country": "US"},
                            "approve": {"pool": "configured", "country": "US"},
                        },
                        "stage_proxy_countries": {"checkout": "US", "approve": "US"},
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
                options={
                    "target_country": "GB",
                    "checkout_country": "GB",
                    "stage_proxy_countries": {"checkout": "GB", "approve": "GB"},
                },
            )

        self.assertEqual(
            {route.expected_country for route in plan.routes.values()},
            {"GB"},
        )

    def test_canonical_country_map_does_not_retain_stale_legacy_provider_countries(self):
        config = {
            "paypal": {
                "target_country": "JP",
                "checkout_country": "JP",
                "stage_proxies": {"provider": "http://jp.example:8080"},
                "stage_proxy_countries": {
                    "checkout": "JP",
                    "provider": "JP",
                    "stripe_init": "JP",
                    "payment_method": "JP",
                    "confirm": "JP",
                    "approve": "JP",
                },
            },
            "protocol_payments": {
                "proxy_pools": {"us_checkout": ["http://us.example:8080"]},
                "methods": {
                    "paypal": {
                        "stage_routes": {
                            "checkout": {"pool": "us_checkout", "country": "US"},
                            "approve": {"pool": "us_checkout", "country": "US"},
                        },
                        "stage_proxy_countries": {"checkout": "US", "approve": "US"},
                    }
                },
            },
        }

        merged = method_payment_config(config, "paypal")
        self.assertEqual(merged["stage_proxy_countries"], {"checkout": "US", "approve": "US"})
        self.assertEqual(merged["stage_proxies"], {})

        with patch(
            "sms_tool.paypal_proxy.select_proxy_from_pool",
            side_effect=lambda pool, *_args, **_kwargs: (pool[0], []),
        ):
            plan = PaymentRoutePlanner(config).plan(
                "paypal",
                options={
                    "target_country": "US",
                    "checkout_country": "US",
                    "stage_proxy_countries": {"checkout": "US", "approve": "US"},
                },
            )

        self.assertEqual(plan.routes["stripe_init"].expected_country, "US")
        self.assertEqual(plan.routes["payment_method"].expected_country, "US")
        self.assertEqual(plan.routes["confirm"].expected_country, "US")

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

    def test_automatic_country_ignores_legacy_manual_country_config(self):
        config = {
            "protocol_payments": {
                "proxy_pools": {
                    "checkout": ["http://checkout.example:8080"],
                    "approve": ["http://approve.example:8080"],
                },
                "methods": {
                    "gopay": {
                        "stage_proxy_countries": {"checkout": "DE", "approve": "TR"},
                        "stage_routes": {
                            "checkout": {"pool": "checkout", "country": "DE"},
                            "approve": {"pool": "approve", "country": "TR"},
                        },
                    }
                },
            }
        }
        seen = []

        def select(pool, country, stage, **_kwargs):
            seen.append((stage, country))
            return pool[0], []

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=select):
            plan = PaymentRoutePlanner(config).plan("gopay", options={"auto_proxy_country": True})

        self.assertEqual(seen[0], ("checkout", "ID"))
        self.assertEqual(plan.routes["approve"].expected_country, "JP")

    def test_ipwo_bare_pool_is_canonicalized_and_retargeted_to_stage_country(self):
        config = {
            "protocol_payments": {
                "proxy_pools": {
                    "checkout": [
                        "us.ipwo.net:7878:account_custom_zone_US:password"
                    ]
                },
                "methods": {
                    "paypal": {
                        "stage_routes": {
                            "checkout": {"pool": "checkout", "country": "JP"}
                        }
                    }
                },
            }
        }

        with patch(
            "sms_tool.paypal_proxy.select_proxy_from_pool",
            side_effect=lambda pool, *_args, **_kwargs: (pool[0], []),
        ):
            plan = PaymentRoutePlanner(config).plan("paypal")

        self.assertTrue(plan.checkout_proxy.startswith("http://"))
        self.assertIn("custom_zone_JP", plan.checkout_proxy)

    def test_paypal_approve_accepts_general_country_list_region(self):
        plan = PaymentRoutePlanner({}).plan(
            "paypal",
            options={
                "checkout_country": "US",
                "approve_country": "TR",
                "stage_proxy_countries": {"checkout": "US", "approve": "TR"},
                "checkout_proxy": "http://checkout.example:8080",
                "approve_proxy": "http://approve.example:8080",
            },
            select_proxies=False,
        )

        self.assertEqual(plan.routes["approve"].expected_country, "TR")


if __name__ == "__main__":
    unittest.main()
