"""Offline unit tests for the external Paylink Extraction Server (提链服务器) adapter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import requests

from sms_tool.pay_link.extract_server import (
    ExtractLinkServerConfig,
    extract_link_via_server,
    resolve_extract_server_config,
)
from sms_tool.payment import link_manager as manager


class ExtractServerConfigTests(unittest.TestCase):
    def test_defaults_to_fail_closed_only_mode_when_unconfigured(self):
        cfg = resolve_extract_server_config({"chatgpt": {}, "protocol_payments": {}}, environ={})
        self.assertEqual(cfg.mode, "only")
        self.assertTrue(cfg.active)
        self.assertFalse(cfg.configured)
        self.assertTrue(cfg.applies_to("pix"))

    def test_explicit_off_mode_disables_server(self):
        cfg = resolve_extract_server_config(
            {"chatgpt": {}, "protocol_payments": {"extract_link_server": {"enabled": False, "mode": "off"}}},
            environ={},
        )
        self.assertEqual(cfg.mode, "off")
        self.assertFalse(cfg.active)
        self.assertFalse(cfg.applies_to("pix"))

    def test_env_base_and_cdk_configure_server(self):
        cfg = resolve_extract_server_config(
            {"chatgpt": {}, "protocol_payments": {}},
            environ={
                "EXTRACT_LINK_API_BASE": "https://paylink.example.test/",
                "EXTRACT_LINK_CDK": "cdk-demo-token",
                "EXTRACT_LINK_TYPE": "pix",
                "EXTRACT_LINK_WORKERS": "6",
                "EXTRACT_LINK_TIMEOUT": "45",
            },
        )
        self.assertEqual(cfg.mode, "only")
        self.assertTrue(cfg.active)
        self.assertTrue(cfg.configured)
        self.assertEqual(cfg.api_base, "https://paylink.example.test")
        self.assertEqual(cfg.request_url(), "https://paylink.example.test/api/v1/extract")
        self.assertEqual(cfg.default_type, "pix")
        self.assertEqual(cfg.workers, 6)
        self.assertEqual(cfg.timeout_seconds, 45)
        self.assertTrue(cfg.applies_to("pix"))

    def test_config_method_filter_and_mode_aliases(self):
        cfg = resolve_extract_server_config(
            {
                "chatgpt": {},
                "protocol_payments": {
                    "extract_link_server": {
                        "enabled": True,
                        "mode": "server",
                        "api_base": "https://paylink.example.test",
                        "cdk": "cdk-secret",
                        "endpoint": "v2/extract",
                        "methods": ["pix", "paypal"],
                        "pass_proxy": False,
                        "verify_tls": False,
                    }
                },
            },
            environ={},
        )
        self.assertEqual(cfg.mode, "only")
        self.assertEqual(cfg.request_url(), "https://paylink.example.test/v2/extract")
        self.assertTrue(cfg.applies_to("pix"))
        self.assertTrue(cfg.applies_to("paypal"))
        self.assertFalse(cfg.applies_to("momo"))
        self.assertFalse(cfg.pass_proxy)
        self.assertFalse(cfg.verify_tls)


class ExtractServerClientTests(unittest.TestCase):
    def test_security_gate_fails_closed_when_missing_config(self):
        cfg = ExtractLinkServerConfig(mode="only", api_base="", cdk="")
        result = extract_link_via_server("pix", "eyJ.tok.sig", config=cfg)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "extract_server_not_configured")
        self.assertEqual(result["status_code"], 501)
        self.assertFalse(result["retryable"])

    def test_rejects_non_http_api_base(self):
        cfg = ExtractLinkServerConfig(mode="only", api_base="file:///etc/passwd", cdk="cdk-1")
        result = extract_link_via_server("pix", "tok", config=cfg)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "extract_server_invalid_url")

    def test_parses_nested_data_and_extracted_links_shapes(self):
        cfg = ExtractLinkServerConfig(mode="only", api_base="https://paylink.example.test", cdk="cdk-test")
        captured: dict[str, Any] = {}

        def transport(url, payload, headers, timeout, verify_tls):
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            captured["timeout"] = timeout
            captured["verify_tls"] = verify_tls
            return 200, {
                "success": True,
                "data": {
                    "extracted_links": [{"pay_url": "https://checkout.example.test/pix/123", "qr_data": "00020101"}],
                    "currency": "BRL",
                    "order_id": "ord_1",
                },
            }

        result = extract_link_via_server(
            "pix",
            "at-sample",
            proxy="http://u:p@proxy.example.test:8080",
            auth_context={"email": "user@example.test"},
            config=cfg,
            transport=transport,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://checkout.example.test/pix/123")
        self.assertEqual(result["qr_data"], "00020101")
        self.assertEqual(result["source"], "extract_link_server")
        self.assertEqual(result["order_id"], "ord_1")
        self.assertEqual(captured["url"], "https://paylink.example.test/api/v1/extract")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer cdk-test")
        self.assertEqual(captured["headers"]["X-CDK-Token"], "cdk-test")
        self.assertEqual(captured["payload"]["email"], "user@example.test")

    def test_http_errors_and_secret_scrubbing(self):
        cdk = "CDK_SUPER_SECRET_999"
        tok = "AT_SUPER_SECRET_888"
        cfg = ExtractLinkServerConfig(mode="only", api_base="https://paylink.example.test", cdk=cdk)

        for status_code, expected_code, expected_retryable in (
            (401, "extract_server_auth_failed", False),
            (429, "extract_server_rate_limited", True),
            (501, "extract_server_not_configured", False),
            (503, "extract_server_unavailable", True),
        ):
            with self.subTest(status_code=status_code):
                res = extract_link_via_server(
                    "paypal",
                    tok,
                    config=cfg,
                    transport=lambda *_: (status_code, {"error": f"bad cdk={cdk} tok={tok}"}),
                )
                self.assertFalse(res["ok"])
                self.assertEqual(res["error_code"], expected_code)
                self.assertEqual(res["retryable"], expected_retryable)
                self.assertNotIn(cdk, res["error"])
                self.assertNotIn(tok, res["error"])

    def test_timeout_and_transport_exceptions_are_classified_and_scrubbed(self):
        cdk = "CDK_SENSITIVE_XYZ"
        tok = "TOKEN_SENSITIVE_ABC"
        cfg = ExtractLinkServerConfig(mode="only", api_base="https://paylink.example.test", cdk=cdk, timeout_seconds=12)

        with patch("sms_tool.pay_link.extract_server.requests.post", side_effect=requests.Timeout(f"timeout {cdk}")):
            res = extract_link_via_server("paypal", tok, config=cfg)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "timed_out")
        self.assertEqual(res["error_code"], "extract_server_timeout")
        self.assertTrue(res["retryable"])
        self.assertNotIn(cdk, res["error"])

        with patch(
            "sms_tool.pay_link.extract_server.requests.post",
            side_effect=requests.ConnectionError(f"conn refused {tok}"),
        ):
            res2 = extract_link_via_server("paypal", tok, config=cfg)
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error_code"], "extract_server_transport_error")
        self.assertTrue(res2["retryable"])
        self.assertNotIn(tok, res2["error"])


class ExtractServerManagerRoutingTests(unittest.TestCase):
    def test_manager_routes_all_methods_through_extract_server(self):
        cfg = {
            "chatgpt": {},
            "protocol_payments": {
                "extract_link_server": {
                    "enabled": True,
                    "mode": "only",
                    "api_base": "https://paylink.example.test",
                    "cdk": "cdk-route-test",
                }
            },
        }
        for method, url in (
            ("paypal", "https://www.paypal.com/agreements/approve?ba_token=BA-TEST"),
            ("upi", "https://pay.openai.com/c/pay/cs_test_upi"),
            ("pix", "https://checkout.example.test/pix/123"),
            ("direct_card", "https://chatgpt.com/checkout/openai_llc/oaics_test"),
        ):
            with (
                self.subTest(method=method),
                tempfile.TemporaryDirectory() as tmp,
                patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"),
                patch(
                    "sms_tool.pay_link.registry.extract_link_via_server",
                    return_value={
                        "ok": True,
                        "status": "completed",
                        "payment_method": method,
                        "source": "extract_link_server",
                        "url": url,
                        "link_type": f"{method}_extract_server",
                    },
                ) as remote_exec,
            ):
                result = manager.generate_payment_link("at-token", payment_method=method, runtime_config=cfg)
            self.assertTrue(result["ok"])
            self.assertEqual(result["source"], "extract_link_server")
            self.assertEqual(result["url"], url)
            self.assertEqual(result["manager_state"], "completed")
            remote_exec.assert_called_once()

    def test_unconfigured_server_fails_closed(self):
        cfg = {
            "chatgpt": {},
            "protocol_payments": {
                "extract_link_server": {
                    "enabled": True,
                    "mode": "only",
                    "api_base": "",
                    "cdk": "",
                }
            },
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"),
        ):
            result = manager.generate_payment_link("at-token", payment_method="paypal", runtime_config=cfg)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "extract_server_not_configured")
        self.assertEqual(result["manager_state"], "failed")

    def test_disabled_server_reports_disabled(self):
        cfg = {
            "chatgpt": {},
            "protocol_payments": {
                "extract_link_server": {
                    "enabled": False,
                    "mode": "off",
                    "api_base": "",
                    "cdk": "",
                }
            },
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"),
        ):
            result = manager.generate_payment_link("at-token", payment_method="upi", runtime_config=cfg)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "extract_server_disabled")
        self.assertEqual(result["manager_state"], "failed")


if __name__ == "__main__":
    unittest.main()
