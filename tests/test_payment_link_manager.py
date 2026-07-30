import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_link_manager as manager


class PaymentLinkManagerTests(unittest.TestCase):
    def test_supported_methods_include_reference_adapters(self):
        keys = {item["key"] for item in manager.supported_payment_methods()}
        self.assertEqual(keys, {"paypal", "gopay", "upi", "ideal", "pix", "kakao", "blik", "twint", "direct_card", "momo"})

    def test_aliases_are_normalized(self):
        self.assertEqual(manager.normalize_payment_method("go-pay"), "gopay")
        self.assertEqual(manager.normalize_payment_method("upi_qr"), "upi")
        self.assertEqual(manager.normalize_payment_method("kakao pay"), "kakao")

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

    def test_gopay_uses_project_payment_service(self):
        response = {"success": True, "checkoutUrl": "https://pay.openai.com/c/pay/cs_test", "flowId": "flow-1"}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manager, "_state_path", return_value=Path(tmp) / "runs.jsonl"):
                with patch("sms_tool.grpcurl_client.call_grpcurl", return_value=response) as rpc:
                    result = manager.generate_payment_link("token", payment_method="gopay", phone="+628123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["flow_id"], "flow-1")
        self.assertEqual(rpc.call_args.args[0], "StartGoPay")

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
                        "token", payment_method="momo", checkout_proxy="socks5h://127.0.0.1:1080"
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

    def test_labeled_payment_url_wins_over_later_diagnostic_url(self):
        output = (
            "iDEAL 最终扫码/授权 URL:\n"
            "https://bank.example.test/authorize\n"
            "cleanup docs: https://docs.example.test/troubleshooting\n"
        )
        self.assertEqual(manager._last_payment_url(output), "https://bank.example.test/authorize")

    def test_persist_run_masks_ba_token_in_persisted_url(self):
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
        # 落盘记录不得含完整 BA token，但保留前缀便于诊断
        self.assertNotIn("BA-1AB23456CD789012E", persisted)
        self.assertIn("ba_token=BA-1AB", persisted)
        # 返回给调用方/UI 的结果仍是完整链接（脱敏只作用于持久化）
        self.assertEqual(result["url"], approve_url)

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
