import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool.payment import link_manager as manager
from sms_tool.payment.catalog import PAYMENT_CATALOG
from sms_tool.payment.routing import PaymentRoutePlan


class PaymentLinkManagerTests(unittest.TestCase):
    def setUp(self):
        self._config_patch = patch(
            "sms_tool.pay_link.base.current_config_data",
            return_value={
                "chatgpt": {},
                "protocol_payments": {
                    "extract_link_server": {
                        "enabled": True,
                        "mode": "only",
                        "api_base": "https://paylink.example.test",
                        "cdk": "cdk-test",
                    }
                },
            },
        )
        self._config_patch.start()
        self.addCleanup(self._config_patch.stop)

    def test_payment_manager_uses_versioned_catalog(self):
        self.assertEqual(set(manager.PAYMENT_METHODS), set(PAYMENT_CATALOG.methods))
        self.assertEqual(manager.normalize_payment_method("go-pay"), "gopay")
        self.assertEqual(manager.PAYMENT_METHODS["momo"].country, "VN")

    def test_probe_mode_reuses_checkout_observation_without_running_link_adapter(self):
        from sms_tool.pay_link.core import probe_payment_method

        observation = {
            "ok": True,
            "payment_method_types": ["card", "upi"],
            "checkout_kind": "stripe",
            "evidence_sources": ["stripe_init", "stripe_elements"],
        }
        with (
            patch("sms_tool.payment.capability.payment_method_capability_probe", return_value=observation) as probe,
            patch("sms_tool.pay_link.core.PAYMENT_ADAPTERS.execute_mapping") as executor,
        ):
            result = probe_payment_method(
                "at",
                "direct_card",
                payment_route_plan=PaymentRoutePlan.empty("direct_card"),
                runtime_config={"chatgpt": {}, "protocol_payments": {}},
            )
        self.assertEqual(result, observation)
        probe.assert_called_once()
        executor.assert_not_called()

    def test_supported_methods_use_extract_server_adapter(self):
        methods = {item["key"]: item for item in manager.supported_payment_methods()}
        keys = set(methods)
        self.assertEqual(
            keys,
            {
                "paypal",
                "gopay",
                "gcash",
                "grabpay",
                "upi",
                "ideal",
                "pix",
                "kakao",
                "blik",
                "twint",
                "direct_card",
                "momo",
                "qris",
                "bizum",
                "naver_pay",
                "team_trial",
            },
        )
        self.assertTrue(
            all(
                item["adapter"] == ("team_trial_direct" if key == "team_trial" else "extract_server")
                and item["available"]
                for key, item in methods.items()
            )
        )

    def test_aliases_are_normalized(self):
        self.assertEqual(manager.normalize_payment_method("upi_qr"), "upi")
        self.assertEqual(manager.normalize_payment_method("kakao pay"), "kakao")
        self.assertEqual(manager.normalize_payment_method("go-pay"), "gopay")
        self.assertEqual(manager.normalize_payment_method("grab pay"), "grabpay")

    def test_unknown_method_is_rejected(self):
        self.assertEqual(manager.normalize_payment_method("unsupported_wallet"), "")

    def test_extract_server_result_has_completed_state_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch(
                    "sms_tool.pay_link.registry.extract_link_via_server",
                    return_value={"ok": True, "url": "https://example.test/pay"},
                ):
                    result = manager.generate_payment_link("token", payment_method="paypal")
        self.assertTrue(result["ok"])
        self.assertEqual(result["manager_state"], "completed")
        self.assertEqual(
            [item["state"] for item in result["state_history"]],
            ["created", "validating", "preparing_proxy", "running", "extracting", "completed"],
        )

    def test_manager_selects_configured_checkout_and_approve_pools(self):
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return f"http://selected-{stage}:8080", [{"ok": True}]

        captured = {}

        def fake_extract(method, access_token, **kwargs):
            captured["method"] = method
            captured["proxy"] = kwargs.get("proxy")
            captured.update(kwargs.get("options") or {})
            return {"ok": True, "url": "https://example.test/pay"}

        config = {
            "chatgpt": {},
            "protocol_payments": {
                "enabled_methods": ["paypal"],
                "extract_link_server": {
                    "enabled": True,
                    "mode": "only",
                    "api_base": "https://paylink.example.test",
                    "cdk": "cdk-test",
                },
                "methods": {
                    "paypal": {
                        "checkout_proxy": "http://legacy-checkout:8080",
                        "approve_proxy": "http://legacy-approve:8080",
                        "checkout_proxy_pool": "http://checkout-a:8080,http://checkout-b:8080",
                        "approve_proxy_pool": ["http://approve-a:8080", "http://approve-b:8080"],
                        "stage_proxy_countries": {"checkout": "JP", "approve": "GB"},
                    },
                },
            },
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"),
            patch("sms_tool.paypal.proxy.select_proxy_from_pool", side_effect=choose),
            patch("sms_tool.pay_link.registry.extract_link_via_server", side_effect=fake_extract),
        ):
            result = manager.generate_payment_link(
                "token",
                payment_method="paypal",
                runtime_config=config,
            )

        self.assertTrue(result["ok"])
        self.assertEqual([item[2] for item in seen], ["checkout", "approve"])
        self.assertEqual(seen[0][1], "JP")
        self.assertEqual(seen[1][1], "GB")
        self.assertEqual(captured["checkout_proxy"], "http://selected-checkout:8080")
        self.assertEqual(captured["approve_proxy"], "http://selected-approve:8080")

    def test_gopay_manager_defaults_approve_pool_to_jp_and_honors_override(self):
        config = {
            "chatgpt": {},
            "protocol_payments": {
                "methods": {
                    "gopay": {
                        "checkout_proxy_pool": ["http://checkout-a:8080"],
                        "approve_proxy_pool": ["http://approve-a:8080"],
                    },
                },
            },
        }
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.paypal.proxy.select_proxy_from_pool", side_effect=choose):
            manager._resolve_proxy_pool_routes(
                "gopay",
                None,
                {"target_country": "ID", "checkout_country": "ID"},
                config,
            )
        self.assertEqual([item[1] for item in seen], ["ID", "JP"])

        seen.clear()
        with patch("sms_tool.paypal.proxy.select_proxy_from_pool", side_effect=choose):
            manager._resolve_proxy_pool_routes(
                "gopay",
                None,
                {
                    "target_country": "ID",
                    "checkout_country": "ID",
                    "stage_proxy_countries": {"approve": "TR"},
                },
                config,
            )
        self.assertEqual([item[1] for item in seen], ["ID", "TR"])

    def _gopay_pool_config(self):
        return {
            "chatgpt": {},
            "protocol_payments": {
                "methods": {
                    "gopay": {
                        "checkout_proxy_pool": ["http://checkout-a:8080"],
                        "approve_proxy_pool": ["http://approve-a:8080"],
                    },
                },
            },
        }

    def test_gopay_approve_country_outside_allowlist_is_coerced_to_jp(self):
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.paypal.proxy.select_proxy_from_pool", side_effect=choose):
            with self.assertLogs("sms_tool.payment.link_manager", level="WARNING") as logs:
                _proxy, values = manager._resolve_proxy_pool_routes(
                    "gopay",
                    None,
                    {
                        "target_country": "ID",
                        "checkout_country": "ID",
                        "stage_proxy_countries": {"approve": "US"},
                    },
                    self._gopay_pool_config(),
                )
        self.assertEqual([item[1] for item in seen], ["ID", "JP"])
        self.assertEqual(values["stage_proxy_countries"]["approve"], "JP")
        self.assertTrue(any("US" in message and "JP" in message for message in logs.output))

    def test_gopay_approve_country_kwarg_is_coerced_and_written_back(self):
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.paypal.proxy.select_proxy_from_pool", side_effect=choose):
            with self.assertLogs("sms_tool.payment.link_manager", level="WARNING"):
                _proxy, values = manager._resolve_proxy_pool_routes(
                    "gopay",
                    None,
                    {"target_country": "ID", "approve_country": "US"},
                    self._gopay_pool_config(),
                )
        self.assertEqual([item[1] for item in seen], ["ID", "JP"])
        self.assertEqual(values["approve_country"], "JP")
        self.assertEqual(values["stage_proxy_countries"]["approve"], "JP")

    def test_gopay_approve_country_jp_tr_and_blank_are_not_coerced(self):
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        for approve in ("JP", "TR"):
            with patch("sms_tool.paypal.proxy.select_proxy_from_pool", side_effect=choose):
                _proxy, values = manager._resolve_proxy_pool_routes(
                    "gopay",
                    None,
                    {"target_country": "ID", "stage_proxy_countries": {"approve": approve}},
                    self._gopay_pool_config(),
                )
            self.assertEqual(values["stage_proxy_countries"]["approve"], approve)
        self.assertEqual([item[1] for item in seen], ["ID", "JP", "ID", "TR"])

        with patch("sms_tool.paypal.proxy.select_proxy_from_pool", side_effect=choose):
            _proxy, values = manager._resolve_proxy_pool_routes(
                "gopay",
                None,
                {"target_country": "ID"},
                self._gopay_pool_config(),
            )
        self.assertNotIn("stage_proxy_countries", values)
        self.assertNotIn("approve_country", values)

    def test_gopay_approve_allowlist_honors_catalog_override(self):
        from sms_tool.payment.catalog import PaymentMethodDefinition

        override = PaymentMethodDefinition(
            key="gopay",
            label="GoPay",
            registration_label="GoPay",
            country="ID",
            currency="IDR",
            adapter="extract_server",
            approve_countries=("TR",),
        )
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.pay_link.registry.CATALOG_METHODS", {"gopay": override}):
            with patch("sms_tool.paypal.proxy.select_proxy_from_pool", side_effect=choose):
                with self.assertLogs("sms_tool.payment.link_manager", level="WARNING"):
                    _proxy, values = manager._resolve_proxy_pool_routes(
                        "gopay",
                        None,
                        {"target_country": "ID", "stage_proxy_countries": {"approve": "US"}},
                        self._gopay_pool_config(),
                    )
        self.assertEqual([item[1] for item in seen], ["ID", "TR"])
        self.assertEqual(values["stage_proxy_countries"]["approve"], "TR")

    def test_non_gopay_approve_country_is_not_coerced(self):
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        config = {
            "chatgpt": {},
            "protocol_payments": {
                "methods": {
                    "grabpay": {
                        "checkout_proxy_pool": ["http://checkout-a:8080"],
                        "approve_proxy_pool": ["http://approve-a:8080"],
                    },
                },
            },
        }
        with patch("sms_tool.paypal.proxy.select_proxy_from_pool", side_effect=choose):
            _proxy, values = manager._resolve_proxy_pool_routes(
                "grabpay",
                None,
                {"target_country": "PH", "stage_proxy_countries": {"approve": "US"}},
                config,
            )
        self.assertEqual([item[1] for item in seen], ["PH", "US"])
        self.assertEqual(values["stage_proxy_countries"]["approve"], "US")

    def test_generate_payment_link_records_gopay_approve_coercion(self):
        adapter_result = {
            "ok": True,
            "status": "completed",
            "operation": "extract_link",
            "url": "https://app.midtrans.com/snap/v4/redirection/fixture",
            "link_type": "gopay_extract_server",
        }
        config = {
            "chatgpt": {},
            "protocol_payments": {
                "extract_link_server": {
                    "enabled": True,
                    "mode": "only",
                    "api_base": "https://paylink.example.test",
                    "cdk": "cdk-test",
                }
            },
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"),
            patch("sms_tool.pay_link.registry.extract_link_via_server", return_value=adapter_result),
        ):
            with self.assertLogs("sms_tool.payment.link_manager", level="WARNING"):
                result = manager.generate_payment_link(
                    "token",
                    payment_method="gopay",
                    runtime_config=config,
                    stage_proxy_countries={"approve": "US"},
                )
        self.assertTrue(result["ok"])
        self.assertEqual(result["approve_country"], "JP")
        self.assertEqual(result["approve_country_original"], "US")
        self.assertTrue(result["approve_country_coerced"])

    def test_generate_payment_link_without_coercion_has_no_coercion_fields(self):
        adapter_result = {
            "ok": True,
            "status": "completed",
            "operation": "extract_link",
            "url": "https://app.midtrans.com/snap/v4/redirection/fixture",
            "link_type": "gopay_extract_server",
        }
        config = {
            "chatgpt": {},
            "protocol_payments": {
                "extract_link_server": {
                    "enabled": True,
                    "mode": "only",
                    "api_base": "https://paylink.example.test",
                    "cdk": "cdk-test",
                }
            },
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"),
            patch("sms_tool.pay_link.registry.extract_link_via_server", return_value=adapter_result),
        ):
            result = manager.generate_payment_link(
                "token",
                payment_method="gopay",
                runtime_config=config,
                stage_proxy_countries={"approve": "TR"},
            )
        self.assertTrue(result["ok"])
        self.assertNotIn("approve_country_coerced", result)
        self.assertNotIn("approve_country_original", result)

    def test_unsupported_method_returns_failed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"):
                result = manager.generate_payment_link("token", payment_method="unknown")
        self.assertFalse(result["ok"])
        self.assertEqual(result["manager_state"], "failed")

    def test_server_failure_preserves_adapter_error_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch(
                    "sms_tool.pay_link.registry.extract_link_via_server",
                    return_value={
                        "ok": False,
                        "error": "UPI unavailable",
                        "error_code": "upi_not_available",
                    },
                ):
                    result = manager.generate_payment_link("token", payment_method="upi")
        self.assertEqual(result["error_code"], "upi_not_available")
        self.assertEqual(result["manager_state"], "failed")

    def test_blik_requires_explicit_six_digit_code_before_calling_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.pay_link.registry.extract_link_via_server") as remote:
                    result = manager.generate_payment_link("token", payment_method="blik")
        self.assertFalse(result["ok"])
        self.assertIn("explicit 6-digit code", result["error"])
        remote.assert_not_called()

    def test_completed_status_does_not_bypass_artifact_validation_for_other_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch(
                    "sms_tool.pay_link.registry.extract_link_via_server",
                    return_value={"ok": True, "status": "completed"},
                ):
                    result = manager.generate_payment_link("token", payment_method="paypal")
        self.assertFalse(result["ok"])
        self.assertIn("no link or QR data", result["error"])

    def test_explicit_empty_enabled_methods_disables_every_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "sms_tool.pay_link.base.current_config_data",
                return_value={"chatgpt": {}, "protocol_payments": {"enabled_methods": []}},
            ):
                with patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"):
                    result = manager.generate_payment_link("token", payment_method="paypal")
        self.assertFalse(result["ok"])
        self.assertIn("disabled", result["error"])

    def test_persist_run_stores_url_presence_without_the_payment_link(self):
        approve_url = "https://www.paypal.com/agreements/approve?ba_token=BA-1AB23456CD789012E"
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs.jsonl"
            with patch("sms_tool.pay_link.persistence._state_path", return_value=runs):
                with patch(
                    "sms_tool.pay_link.registry.extract_link_via_server",
                    return_value={
                        "ok": True,
                        "url": approve_url,
                        "ba_token": "BA-1AB23456CD789012E",
                        "link_type": "paypal_extract_server",
                    },
                ):
                    result = manager.generate_payment_link("token", payment_method="paypal")
            persisted = runs.read_text(encoding="utf-8")
        self.assertNotIn(approve_url, persisted)
        self.assertNotIn("https://", persisted)
        self.assertNotIn("BA-1AB23456CD789012E", persisted)
        self.assertNotIn("BA-1AB", persisted)
        self.assertIn('"url_present": true', persisted)
        self.assertEqual(result["url"], approve_url)

    def test_persist_run_replaces_nested_provider_and_qr_artifacts_with_presence(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs.jsonl"
            with patch("sms_tool.pay_link.persistence._state_path", return_value=runs):
                manager._persist_run(
                    {
                        "ok": True,
                        "provider_redirect_url": "https://provider.example.test/pay/secret-session",
                        "details": {
                            "fallback_url": "https://fallback.example.test/secret",
                            "qr_data": "upi://pay?pa=sensitive",
                            "qr_path": "C:/private/payment.png",
                        },
                    }
                )
            record = runs.read_text(encoding="utf-8")

        for secret in ("provider.example.test", "fallback.example.test", "upi://", "payment.png"):
            self.assertNotIn(secret, record)
        self.assertIn('"provider_redirect_url_present": true', record)
        self.assertIn('"fallback_url_present": true', record)
        self.assertIn('"qr_data_present": true', record)
        self.assertIn('"qr_path_present": true', record)

    def test_persist_run_drops_raw_tail_and_redacts_embedded_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs.jsonl"
            with patch("sms_tool.pay_link.persistence._state_path", return_value=runs):
                manager._persist_run(
                    {
                        "ok": False,
                        "raw_output_tail": "Authorization: Bearer raw-tail-secret",
                        "error": (
                            "Authorization: Bearer bearer-secret "
                            "access_token=access-secret "
                            "proxy=http://proxy-user:proxy-pass@example.test:8080"
                        ),
                    }
                )
            persisted = runs.read_text(encoding="utf-8")
        self.assertNotIn("raw_output_tail", persisted)
        for secret in ("raw-tail-secret", "bearer-secret", "access-secret", "proxy-user", "proxy-pass"):
            self.assertNotIn(secret, persisted)

    def test_persistence_failure_is_reported_without_raising(self):
        with patch(
            "sms_tool.pay_link.registry.extract_link_via_server",
            return_value={"ok": True, "url": "https://example.test/pay"},
        ):
            with patch("sms_tool.pay_link.persistence._persist_run", side_effect=OSError("disk blocked")):
                result = manager.generate_payment_link("token", payment_method="paypal")
        self.assertTrue(result["ok"])
        self.assertEqual(result["manager_state"], "completed")
        self.assertIn("OSError", result["persistence_warning"])

    def _run_with_journal_finish(self, finish_error: BaseException) -> list[tuple[str, str]]:
        calls: list[tuple[str, str]] = []

        class _FakeOperation:
            operation_id = "op-fixture"
            idempotency_key_hash = "hash-fixture"

            def checkpoint(self, *_args, **_kwargs):
                pass

            def finish(self, _result):
                raise finish_error

            def fail_unknown(self, stage, error_code):
                calls.append((stage, error_code))

        class _FakeStore:
            @classmethod
            def from_config(cls, _source):
                return cls()

            def begin(self, **_kwargs):
                return _FakeOperation()

        adapter_result = {
            "ok": True,
            "status": "completed",
            "operation": "extract_link",
            "url": "https://app.midtrans.com/snap/v4/redirection/fixture",
            "link_type": "gopay_extract_server",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("sms_tool.pay_link.core.PaymentOperationStore", _FakeStore),
            patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"),
            patch("sms_tool.pay_link.registry.extract_link_via_server", return_value=adapter_result),
        ):
            try:
                manager.generate_payment_link(
                    "token",
                    payment_method="gopay",
                    runtime_config={
                        "chatgpt": {},
                        "protocol_payments": {
                            "extract_link_server": {
                                "enabled": True,
                                "mode": "only",
                                "api_base": "https://paylink.example.test",
                                "cdk": "cdk-test",
                            }
                        },
                    },
                )
            except finish_error.__class__:
                pass
        return calls

    def test_journal_failure_marks_the_durable_operation_unknown(self):
        calls = self._run_with_journal_finish(OSError("journal write failed"))
        self.assertEqual(calls, [("executor", "payment_executor_aborted")])

    def test_system_exit_is_not_converted_into_a_journal_failure(self):
        calls = self._run_with_journal_finish(SystemExit(3))
        self.assertEqual(calls, [])


class FacadeSurfaceTests(unittest.TestCase):
    def test_every_consumed_facade_name_is_declared_in_all(self):
        root = Path(__file__).resolve().parents[2]
        declared = set(manager.__all__)
        consumed: dict[str, set[str]] = {}
        for path in (root / "sms_tool").rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if "payment_link_manager" not in node.module:
                    continue
                for alias in node.names:
                    if alias.name != "*":
                        consumed.setdefault(alias.name, set()).add(str(path.relative_to(root)))
        undeclared = {name: sorted(srcs) for name, srcs in consumed.items() if name not in declared}
        self.assertEqual(undeclared, {})

    def test_parse_proxy_pool_is_declared_and_reachable(self):
        from sms_tool import pay_link

        self.assertIn("parse_proxy_pool", pay_link.__all__)
        self.assertIn("parse_proxy_pool", manager.__all__)
        self.assertTrue(callable(manager.parse_proxy_pool))


if __name__ == "__main__":
    unittest.main()
