import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path


CORE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services" / "protocol-payment" / "common" / "protocol_core.py"
)
SPEC = importlib.util.spec_from_file_location("protocol_payment_core", CORE_PATH)
CORE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = CORE
SPEC.loader.exec_module(CORE)


class ProtocolPaymentCommonTests(unittest.TestCase):
    def test_result_envelope_is_versioned_single_line_json(self):
        result = CORE.ProtocolResult(
            payment_method="ideal",
            ok=True,
            status="completed",
            url="https://example.test/authorize",
        )
        serialized = result.to_json()
        self.assertNotIn("\n", serialized)
        self.assertIn('"schema":"protocol_payment.v1"', serialized)

    def test_amount_and_nested_submission_parsers(self):
        payload = {
            "invoice": {"amount_due": 1250},
            "nested": [{"submission_attempt": {"status": "pending"}}],
        }
        self.assertEqual(CORE.amount_from_payload(payload), 1250)
        self.assertEqual(CORE.find_submission_attempt(payload), {"status": "pending"})

    def test_redirect_extraction_uses_adapter_allowlist(self):
        payload = {"next_action": {"redirect_to_url": {"url": "https://bank.test/pay"}}}
        allowed = lambda value, _action: str(value).startswith("https://bank.test/")
        denied = lambda _value, _action: False
        self.assertEqual(CORE.extract_redirect_url(payload, allowed), "https://bank.test/pay")
        self.assertEqual(CORE.extract_redirect_url(payload, denied), "")

    def test_environment_parsers_are_bounded(self):
        previous = os.environ.get("PROTOCOL_TEST_INT")
        os.environ["PROTOCOL_TEST_INT"] = "-4"
        try:
            self.assertEqual(CORE.env_int("PROTOCOL_TEST_INT", 5, minimum=1), 1)
        finally:
            if previous is None:
                os.environ.pop("PROTOCOL_TEST_INT", None)
            else:
                os.environ["PROTOCOL_TEST_INT"] = previous

    def test_sanitize_text_matches_shared_policy_rules(self):
        cases = (
            ('Bearer abc123', '[REDACTED]'),
            ('https://bank.test/pay', 'https://bank.test/pay'),  # plain URLs survive
        )
        for raw, expected in cases:
            self.assertIn(expected, CORE.sanitize_text(raw))
        # credential-bearing URLs, stripe keys and rt_ refresh tokens collapse
        self.assertEqual(
            CORE.sanitize_text('http://user:sekret-pw@gate.kookeey.info:1000'),
            'http://[REDACTED]@gate.kookeey.info:1000',
        )
        self.assertEqual(CORE.sanitize_text('key sk_live_ABCDEFGHIJ1234'), 'key [REDACTED]')
        self.assertEqual(CORE.sanitize_text('token rt_abcdefgh12345678'), 'token [REDACTED]')
        self.assertIn('password=[REDACTED]', CORE.sanitize_text('password=hunter2'))
        self.assertIn('authorization: [REDACTED]', CORE.sanitize_text('authorization: Basic dXNlcg=='))

    def test_sanitize_payload_redacts_secret_keys_but_not_safe_suffixes(self):
        payload = {
            "access_token": "eyJabc.def.ghi",
            "card_number": "4111111111111111",
            "card_holder": "Alice",  # not a secret key; must survive key-based redaction
            "note": "Bearer xyz",
        }
        cleaned = CORE.sanitize_payload(payload)
        self.assertEqual(cleaned["access_token"], "[REDACTED]")
        self.assertEqual(cleaned["card_number"], "[REDACTED]")
        self.assertEqual(cleaned["card_holder"], "Alice")
        self.assertIn("[REDACTED]", cleaned["note"])

    def test_result_reporter_emits_one_terminal_contract(self):
        lines = []
        reporter = CORE.ProtocolResultReporter("ideal", writer=lines.append)

        self.assertTrue(reporter.success("https://bank.test/authorize"))
        self.assertFalse(reporter.failure("must not replace success"))

        self.assertTrue(reporter.emitted)
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["payment_method"], "ideal")
        self.assertEqual(payload["link_type"], "ideal_protocol")

    def test_result_reporter_handles_already_paid_and_terminal_fallback(self):
        already_paid_lines = []
        reporter = CORE.ProtocolResultReporter("twint", writer=already_paid_lines.append)
        reporter.already_paid()
        payload = json.loads(already_paid_lines[0])
        self.assertEqual(payload["status"], "already_paid")
        self.assertEqual(payload["error_code"], "account_already_paid")
        self.assertFalse(payload["retryable"])

        fallback_lines = []
        fallback = CORE.ProtocolResultReporter("blik", writer=fallback_lines.append)
        self.assertTrue(fallback.ensure_terminal(7))
        self.assertFalse(fallback.ensure_terminal(7))
        payload = json.loads(fallback_lines[0])
        self.assertEqual(payload["error_code"], "extractor_output_missing")
        self.assertIn("exited 7", payload["error"])

    def test_result_reporter_supports_dynamic_method_and_prefixed_completion(self):
        lines = []
        method = ["blik"]
        reporter = CORE.ProtocolResultReporter(lambda: method[0], writer=lines.append)
        reporter.success(
            "",
            operation="execute_payment",
            link_type="blik_protocol_completed",
            message="completed",
            side_effect_started=True,
            prefix="BLIK_RESULT:",
        )

        self.assertTrue(lines[0].startswith("BLIK_RESULT:"))
        payload = json.loads(lines[0].removeprefix("BLIK_RESULT:"))
        self.assertEqual(payload["operation"], "execute_payment")
        self.assertTrue(payload["side_effect_started"])


    def test_command_id_correlates_with_the_launching_cli_task(self):
        # 协议支付终端报告此前没有任何相关性字段，桌面侧只能靠进程级环境猜。
        # 子进程继承 SMS_TOOL_COMMAND_ID，报告应携带同一 ID。
        import unittest.mock as mock

        result = CORE.ProtocolResult(payment_method="ideal", ok=True, status="completed")
        self.assertEqual(result.command_id, "")

        with mock.patch.dict(os.environ, {"SMS_TOOL_COMMAND_ID": "cmd-123"}):
            correlated = CORE.ProtocolResult(payment_method="ideal", ok=True, status="completed")
            payload = json.loads(correlated.to_json())
        self.assertEqual(payload["command_id"], "cmd-123")

    def test_redaction_parity_with_sms_tool_sanitizer(self):
        """双端脱敏一致：protocol_core 的手搓规则集与 sms_tool.sanitizer
        （policy 驱动）对同一批秘密形状都要打码。规则漂移在此显形，而不是
        在某个子进程漏报密时。"""
        from sms_tool.sanitizer import sanitize_text as sms_sanitize_text

        battery = [
            "access_token=eyJhbGciOiJSUzI1NiJ9.payload.signature-value",
            "refresh_token: rt_abcdefgh12345678",
            "Authorization: Bearer abcdef123456",
            "password=hunter2-secret",
            "http://user:sekret-pw@gate.kookeey.info:1000",
            "key sk_live_ABCDEFGHIJ1234",
            "token BA-ABCD1234EFGH5678",
        ]
        for raw in battery:
            self.assertIn("[REDACTED]", CORE.sanitize_text(raw), raw)
            self.assertIn("[REDACTED]", sms_sanitize_text(raw), raw)

    def test_payload_keys_are_policy_driven_not_hardcoded(self):
        # policy sensitive_keys/sensitive_key_fragments 里的键必须打码——旧手搓
        # 片段表不含 api_key/license_key/session_id，它们此前会漏。证明读取的
        # 是 policy 而不是内置回退表。
        cleaned = CORE.sanitize_payload({
            "api_key": "secret-value",
            "license_key": "lic-value",
            "session_id": "sess-value",
            "cs_id": "cs-value",
            "error_count": 3,
            "note": "safe text",
        })
        self.assertEqual(cleaned["api_key"], "[REDACTED]")
        self.assertEqual(cleaned["license_key"], "[REDACTED]")
        self.assertEqual(cleaned["session_id"], "[REDACTED]")
        self.assertEqual(cleaned["cs_id"], "[REDACTED]")
        self.assertEqual(cleaned["error_count"], 3)

    def test_safe_key_paths_keep_proxy_affinity_session_id(self):
        cleaned = CORE.sanitize_payload({
            "proxy_affinity": {"session_id": "sid-value", "country": "VN"},
            "session_id": "top-level-must-die",
        })
        self.assertEqual(cleaned["proxy_affinity"]["session_id"], "sid-value")
        self.assertEqual(cleaned["session_id"], "[REDACTED]")

    def test_sanitize_log_text_masks_account_emails(self):
        masked = CORE.sanitize_log_text("failed for alice@example.com twice")
        self.assertNotIn("alice@example.com", masked)
        self.assertIn("***@example.com", masked)

    def test_redaction_survives_missing_policy_file(self):
        # policy 缺失必须退回内置 LEGACY 规则：提取器绝不漏报密。
        import unittest.mock as mock

        with mock.patch.object(CORE, "_POLICY_LOADED", True), \
             mock.patch.object(CORE, "_POLICY_CACHE", None):
            self.assertIn("[REDACTED]", CORE.sanitize_text("password=hunter2"))
            self.assertEqual(CORE.sanitize_payload({"access_token": "x"})["access_token"], "[REDACTED]")

if __name__ == "__main__":
    unittest.main()
