import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_link_manager as manager
from sms_tool.payment_catalog import PAYMENT_CATALOG


class PaymentLinkManagerTests(unittest.TestCase):
    def setUp(self):
        self._config_patch = patch.object(
            manager,
            "current_config_data",
            return_value={"chatgpt": {}, "protocol_payments": {}},
        )
        self._config_patch.start()
        self.addCleanup(self._config_patch.stop)

    def test_payment_manager_uses_versioned_catalog(self):
        self.assertEqual(set(manager.PAYMENT_METHODS), set(PAYMENT_CATALOG.methods))
        self.assertEqual(manager.normalize_payment_method("go-pay"), "gopay")
        self.assertEqual(manager.PAYMENT_METHODS["momo"].country, "VN")
    def test_supported_methods_include_reference_adapters(self):
        methods = {item["key"]: item for item in manager.supported_payment_methods()}
        keys = set(methods)
        self.assertEqual(keys, {
            "paypal", "gopay", "gcash", "grabpay", "upi", "ideal", "pix", "kakao",
            "blik", "twint", "direct_card", "momo", "qris", "bizum", "naver_pay",
        })
        self.assertTrue(methods["gcash"]["available"])
        self.assertTrue(all(methods[key]["adapter"] == "regional_wallet" for key in ("qris", "bizum", "naver_pay")))

    def test_aliases_are_normalized(self):
        self.assertEqual(manager.normalize_payment_method("upi_qr"), "upi")
        self.assertEqual(manager.normalize_payment_method("kakao pay"), "kakao")
        self.assertEqual(manager.normalize_payment_method("go-pay"), "gopay")
        self.assertEqual(manager.normalize_payment_method("grab pay"), "grabpay")

    def test_unknown_method_is_rejected(self):
        self.assertEqual(manager.normalize_payment_method("unsupported_wallet"), "")

    def test_native_result_has_completed_state_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.gen_pp_link.generate_pp_link", return_value={"ok": True, "url": "https://example.test/pay"}):
                    result = manager.generate_payment_link("token", payment_method="paypal")
        self.assertTrue(result["ok"])
        self.assertEqual(result["manager_state"], "completed")
        self.assertEqual([item["state"] for item in result["state_history"]], [
            "created", "validating", "preparing_proxy", "running", "extracting", "completed"
        ])

    def test_manager_selects_configured_checkout_and_approve_pools(self):
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return f"http://selected-{stage}:8080", [{"ok": True}]

        captured = {}

        def fake_generate(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "url": "https://example.test/pay"}

        config = {
            "chatgpt": {},
            "protocol_payments": {
                "enabled_methods": ["paypal"],
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
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"), \
             patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose), \
             patch("sms_tool.gen_pp_link.generate_pp_link", side_effect=fake_generate):
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

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            manager._resolve_proxy_pool_routes(
                "gopay",
                None,
                {"target_country": "ID", "checkout_country": "ID"},
                config,
            )
        self.assertEqual([item[1] for item in seen], ["ID", "JP"])

        seen.clear()
        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
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

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            with self.assertLogs("sms_tool.payment_link_manager", level="WARNING") as logs:
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
        # The enforced country is written back so wallet stage rotation uses it.
        self.assertEqual(values["stage_proxy_countries"]["approve"], "JP")
        self.assertTrue(any("US" in message and "JP" in message for message in logs.output))

    def test_gopay_approve_country_kwarg_is_coerced_and_written_back(self):
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            with self.assertLogs("sms_tool.payment_link_manager", level="WARNING"):
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
            with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
                _proxy, values = manager._resolve_proxy_pool_routes(
                    "gopay",
                    None,
                    {"target_country": "ID", "stage_proxy_countries": {"approve": approve}},
                    self._gopay_pool_config(),
                )
            self.assertEqual(values["stage_proxy_countries"]["approve"], approve)
        self.assertEqual([item[1] for item in seen], ["ID", "JP", "ID", "TR"])

        # Blank approve country keeps the existing JP default without a
        # coercion write-back adding new keys to the kwargs.
        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            _proxy, values = manager._resolve_proxy_pool_routes(
                "gopay",
                None,
                {"target_country": "ID"},
                self._gopay_pool_config(),
            )
        self.assertNotIn("stage_proxy_countries", values)
        self.assertNotIn("approve_country", values)

    def test_gopay_approve_allowlist_honors_catalog_override(self):
        from sms_tool.payment_catalog import PaymentMethodDefinition

        override = PaymentMethodDefinition(
            key="gopay",
            label="GoPay",
            registration_label="GoPay",
            country="ID",
            currency="IDR",
            adapter="wallet",
            approve_countries=("TR",),
        )
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch.object(manager, "CATALOG_METHODS", {"gopay": override}):
            with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
                with self.assertLogs("sms_tool.payment_link_manager", level="WARNING"):
                    _proxy, values = manager._resolve_proxy_pool_routes(
                        "gopay",
                        None,
                        {"target_country": "ID", "stage_proxy_countries": {"approve": "US"}},
                        self._gopay_pool_config(),
                    )
        # JP is not in the catalog override allowlist, so the first allowed
        # entry becomes the coercion target.
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
        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
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
            "link_type": "gopay_protocol",
        }
        config = {"chatgpt": {}, "protocol_payments": {}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"), \
             patch("sms_tool.wallet_provider.run_wallet_provider", return_value=adapter_result):
            with self.assertLogs("sms_tool.payment_link_manager", level="WARNING"):
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
            "link_type": "gopay_protocol",
        }
        config = {"chatgpt": {}, "protocol_payments": {}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"), \
             patch("sms_tool.wallet_provider.run_wallet_provider", return_value=adapter_result):
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
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                result = manager.generate_payment_link("token", payment_method="unknown")
        self.assertFalse(result["ok"])
        self.assertEqual(result["manager_state"], "failed")

    def test_native_failure_preserves_adapter_error_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.gen_pp_link.generate_upi_qr_link", return_value={
                    "ok": False,
                    "error": "UPI unavailable",
                    "error_code": "upi_not_available",
                }):
                    result = manager.generate_payment_link("token", payment_method="upi")
        self.assertEqual(result["error_code"], "upi_not_available")
        self.assertEqual(result["manager_state"], "failed")

    def test_blik_completion_marker_counts_as_success(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "BLIK 自动提交完成\n"
                'BLIK_RESULT:{"ok": true, "payment_method": "blik", "status": "completed", '
                '"link_type": "blik_protocol_completed", "message": "BLIK 自动提交完成"}\n'
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.payment_link_manager.subprocess.run", return_value=completed):
                    result = manager.generate_payment_link(
                        "token", payment_method="blik", seed_proxy="socks5h://127.0.0.1:1080", blik_code="123456"
                    )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["url"], "")
        self.assertEqual(result["link_type"], "blik_protocol_completed")
        self.assertEqual(result["operation"], "execute_payment")
        self.assertEqual(result["manager_state"], "completed")

    def test_protocol_v1_result_is_preferred_over_log_url_scraping(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "diagnostic https://docs.example.test/not-the-result\n"
                '{"payment_method":"ideal","ok":true,"status":"completed",'
                '"operation":"extract_link","url":"https://bank.example.test/authorize",'
                '"link_type":"ideal_protocol","schema":"protocol_payment.v1"}\n'
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"), \
             patch("sms_tool.payment_link_manager.subprocess.run", return_value=completed):
            result = manager.generate_payment_link(
                "token", payment_method="ideal", seed_proxy="socks5h://127.0.0.1:1080",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://bank.example.test/authorize")
        self.assertEqual(result["schema"], "protocol_payment.v1")

    def test_blik_completion_marker_requires_explicit_success_contract(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                'BLIK_RESULT:{"ok": false, "payment_method": "blik", "status": "completed", '
                '"link_type": "blik_protocol_completed"}\n'
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.payment_link_manager.subprocess.run", return_value=completed):
                    result = manager.generate_payment_link(
                        "token", payment_method="blik", seed_proxy="socks5h://127.0.0.1:1080", blik_code="123456"
                    )
        self.assertFalse(result["ok"])
        self.assertEqual(result["manager_state"], "failed")

    def test_blik_requires_explicit_six_digit_code_before_starting_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.payment_link_manager.subprocess.run") as run:
                    result = manager.generate_payment_link(
                        "token", payment_method="blik", seed_proxy="socks5h://127.0.0.1:1080"
                    )
        self.assertFalse(result["ok"])
        self.assertIn("explicit 6-digit code", result["error"])
        run.assert_not_called()

    def test_pix_nonzero_exit_cannot_become_success_from_stdout_json(self):
        failed = subprocess.CompletedProcess(
            args=[],
            returncode=7,
            stdout='{"long_url": "https://example.test/pay"}\n',
            stderr="fatal after output",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.payment_link_manager.subprocess.run", return_value=failed):
                    result = manager.generate_payment_link(
                        "token", payment_method="pix", seed_proxy="socks5h://127.0.0.1:1080"
                    )
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["manager_state"], "failed")

    def test_direct_card_parses_checkout_long_url(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '{"ok": true, "long_url": "https://chatgpt.com/checkout/openai_llc/oaics_test", '
                '"cs_id": "oaics_test", "amount_minor": 0, "amount_currency": "PHP", '
                '"amount_verification": "verified_zero"}\n'
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch.object(manager.payment_egress, "assert_egress_countries"):
                    with patch("sms_tool.payment_link_manager.subprocess.run", return_value=completed):
                        result = manager.generate_payment_link(
                            "token", payment_method="direct_card", checkout_proxy="socks5h://127.0.0.1:1080"
                        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://chatgpt.com/checkout/openai_llc/oaics_test")
        self.assertEqual(result["link_type"], "direct_card_protocol")
        self.assertEqual(result["manager_state"], "completed")

    def test_direct_card_requires_checkout_proxy_before_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.payment_link_manager.subprocess.run") as run:
                    result = manager.generate_payment_link("token", payment_method="direct_card")
        self.assertFalse(result["ok"])
        self.assertIn("proxy", result["error"].lower())
        run.assert_not_called()

    def test_direct_card_proxy_credentials_travel_via_env_not_argv(self):
        secret = "socks5h://user:sekret@127.0.0.1:1080"
        captured = {}

        def fake_run(command, *args, **kwargs):
            captured["command"] = list(command)
            captured["env"] = dict(kwargs.get("env") or {})
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"ok": true, "long_url": "https://chatgpt.com/checkout/openai_llc/oaics_x", "cs_id": "oaics_x", "amount_minor": 0}\n',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch.object(manager.payment_egress, "assert_egress_countries"):
                    with patch("sms_tool.payment_link_manager.subprocess.run", side_effect=fake_run):
                        result = manager.generate_payment_link(
                            "token", payment_method="direct_card", checkout_proxy=secret
                        )
        self.assertTrue(result["ok"])
        self.assertNotIn(secret, captured["command"])
        self.assertNotIn("--checkout-proxy", captured["command"])
        self.assertNotIn("--update-proxy", captured["command"])
        self.assertEqual(captured["env"]["DIRECT_CARD_CHECKOUT_PROXY"], secret)
        self.assertEqual(captured["env"]["DIRECT_CARD_UPDATE_PROXY"], secret)

    def test_pix_proxy_credentials_travel_via_env_not_argv(self):
        secret = "socks5h://user:sekret@127.0.0.1:1080"
        provider = "http://prov:pw@10.0.0.2:9000"
        captured = {}

        def fake_run(command, *args, **kwargs):
            captured["command"] = list(command)
            captured["env"] = dict(kwargs.get("env") or {})
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"long_url": "https://example.test/pix", "pix_qr_code": "000201"}\n',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.payment_link_manager.subprocess.run", side_effect=fake_run):
                    result = manager.generate_payment_link(
                        "token",
                        payment_method="pix",
                        seed_proxy=secret,
                        provider_proxy=provider,
                    )
        self.assertTrue(result["ok"])
        self.assertNotIn(secret, captured["command"])
        self.assertNotIn("--proxy", captured["command"])
        self.assertNotIn("--br-proxy", captured["command"])
        self.assertEqual(captured["env"]["PIX_PROXY"], secret)
        self.assertEqual(captured["env"]["PIX_BR_PROXY"], provider)
        self.assertNotIn(secret, " ".join(captured["command"]))

    def test_momo_passes_through_runner_qr_json(self):
        gateway = "https://payment.momo.vn/v2/gateway/pay?t=1&s=2"
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '{"ok": true, "payment_method": "momo", "url": "' + gateway + '", '
                '"qr_data": "' + gateway + '", "qr_path": "", "has_qr": true, '
                '"decision": "ready_with_qr", "link_type": "momo_protocol_qr"}\n'
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.payment_link_manager.subprocess.run", return_value=completed):
                    result = manager.generate_payment_link(
                        "token",
                        payment_method="momo",
                        checkout_proxy="socks5h://127.0.0.1:1080",
                        runtime_config={
                            "chatgpt": {},
                            "protocol_payments": {
                                "enabled_methods": ["momo"],
                                "egress_check": {"enabled": False},
                            },
                        },
                    )
        self.assertTrue(result["ok"])
        self.assertIn("payment.momo.vn", result["url"])
        self.assertEqual(result["link_type"], "momo_protocol_qr")
        self.assertEqual(result["manager_state"], "completed")

    def test_kakao_nonzero_json_contract_survives_nonzero_exit(self):
        failed = subprocess.CompletedProcess(
            args=[],
            returncode=3,
            stdout=(
                '{"ok":false,"payment_method":"kakao","decision":"nonzero_offer",'
                '"stage":"stripe_init","amount_due":29000,"currency":"KRW",'
                '"has_kakao":true,"url":"","attempts":1,"error":"nonzero"}\n'
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.payment_link_manager.subprocess.run", return_value=failed):
                    result = manager.generate_payment_link(
                        "token", payment_method="kakao", seed_proxy="socks5h://127.0.0.1:1080"
                    )
        self.assertFalse(result["ok"])
        self.assertEqual(result["decision"], "nonzero_offer")
        self.assertEqual(result["amount_due"], 29000)
        self.assertEqual(result["manager_state"], "failed")

    def test_completed_status_does_not_bypass_artifact_validation_for_other_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.gen_pp_link.generate_pp_link", return_value={"ok": True, "status": "completed"}):
                    result = manager.generate_payment_link("token", payment_method="paypal")
        self.assertFalse(result["ok"])
        self.assertIn("no link or QR data", result["error"])

    def test_explicit_empty_enabled_methods_disables_every_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(manager.CFG, {"protocol_payments": {"enabled_methods": []}}, clear=False):
                with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                    result = manager.generate_payment_link("token", payment_method="paypal")
        self.assertFalse(result["ok"])
        self.assertIn("disabled", result["error"])

    def test_labeled_log_lines_are_no_longer_scraped_for_urls(self):
        # Extractors must emit a trailing structured JSON; labeled log lines
        # (historically scraped via _RESULT_URL_RE) are deliberately ignored so
        # a diagnostic URL can never be reported as a payment link.
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "iDEAL 最终扫码/授权 URL:\n"
                "https://bank.example.test/authorize\n"
                "cleanup docs: https://docs.example.test/troubleshooting\n"
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch.object(manager.payment_egress, "assert_egress_countries"):
                    with patch("sms_tool.payment_link_manager.subprocess.run", return_value=completed):
                        result = manager.generate_payment_link(
                            "token",
                            payment_method="ideal",
                            checkout_proxy="socks5h://127.0.0.1:1080",
                        )
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("error_code"), "extractor_output_missing")
        self.assertEqual(result.get("url"), "")

    def test_already_paid_v1_failure_contract_survives_zero_exit(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '检测到 User is already paid：用户已支付，任务正常结束\n'
                '{"payment_method": "ideal", "ok": false, "status": "already_paid", '
                '"url": "", "link_type": "ideal_protocol", "error": "User is already paid", '
                '"error_code": "account_already_paid", "retryable": false, '
                '"schema": "protocol_payment.v1"}\n'
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch.object(manager.payment_egress, "assert_egress_countries"):
                    with patch("sms_tool.payment_link_manager.subprocess.run", return_value=completed):
                        result = manager.generate_payment_link(
                            "token",
                            payment_method="ideal",
                            checkout_proxy="socks5h://127.0.0.1:1080",
                        )
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("status"), "already_paid")
        self.assertEqual(result.get("error_code"), "account_already_paid")

    def test_persist_run_stores_url_presence_without_the_payment_link(self):
        approve_url = "https://www.paypal.com/agreements/approve?ba_token=BA-1AB23456CD789012E"
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs.jsonl"
            with patch.object(manager, "_state_path", return_value=runs):
                with patch("sms_tool.gen_pp_link.generate_pp_link", return_value={
                    "ok": True,
                    "url": approve_url,
                    "ba_token": "BA-1AB23456CD789012E",
                    "link_type": "paypal_ba_approve",
                }):
                    result = manager.generate_payment_link("token", payment_method="paypal")
            persisted = runs.read_text(encoding="utf-8")
        # Run history keeps artifact presence only; the provider link never lands on disk.
        self.assertNotIn(approve_url, persisted)
        self.assertNotIn("https://", persisted)
        self.assertNotIn("BA-1AB23456CD789012E", persisted)
        self.assertNotIn("BA-1AB", persisted)
        self.assertIn('"url_present": true', persisted)
        # 返回给调用方/UI 的结果仍是完整链接（脱敏只作用于持久化）
        self.assertEqual(result["url"], approve_url)

    def test_persist_run_replaces_nested_provider_and_qr_artifacts_with_presence(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs.jsonl"
            with patch.object(manager, "_state_path", return_value=runs):
                manager._persist_run({
                    "ok": True,
                    "provider_redirect_url": "https://provider.example.test/pay/secret-session",
                    "details": {
                        "fallback_url": "https://fallback.example.test/secret",
                        "qr_data": "upi://pay?pa=sensitive",
                        "qr_path": "C:/private/payment.png",
                    },
                })
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
            with patch.object(manager, "_state_path", return_value=runs):
                manager._persist_run({
                    "ok": False,
                    "raw_output_tail": "Authorization: Bearer raw-tail-secret",
                    "error": (
                        "Authorization: Bearer bearer-secret "
                        "access_token=access-secret "
                        "proxy=http://proxy-user:proxy-pass@example.test:8080"
                    ),
                })
            persisted = runs.read_text(encoding="utf-8")
        self.assertNotIn("raw_output_tail", persisted)
        for secret in ("raw-tail-secret", "bearer-secret", "access-secret", "proxy-user", "proxy-pass"):
            self.assertNotIn(secret, persisted)

    def test_persistence_failure_is_reported_without_raising(self):
        with patch("sms_tool.gen_pp_link.generate_pp_link", return_value={"ok": True, "url": "https://example.test/pay"}):
            with patch.object(manager, "_persist_run", side_effect=OSError("disk blocked")):
                result = manager.generate_payment_link("token", payment_method="paypal")
        self.assertTrue(result["ok"])
        self.assertEqual(result["manager_state"], "completed")
        self.assertIn("OSError", result["persistence_warning"])


if __name__ == "__main__":
    unittest.main()
