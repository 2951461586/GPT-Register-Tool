"""Contract tests for the shared ``protocol_payment.v1`` result schema + redaction.

``ProtocolResultReporter`` (services/protocol-payment/common/protocol_core.py) is
the single emitter all v1 extractors (blik / ideal / twint) use. These tests lock
the output contract and the secret-redaction rules so a future edit cannot quietly
change the JSON shape the .NET manager parses, or leak a token into stdout.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "protocol-payment" / "common"))

from protocol_core import (  # noqa: E402
    ProtocolResult,
    ProtocolResultReporter,
    sanitize_payload,
    sanitize_text,
)


def _collect():
    lines = []
    return lines, lambda text: lines.append(text)


class ProtocolPaymentV1SchemaTests(unittest.TestCase):
    def test_success_emits_protocol_payment_v1_schema(self):
        lines, writer = _collect()
        reporter = ProtocolResultReporter("blik", writer=writer)
        ok = reporter.success("https://pay.example/abc/long")

        self.assertTrue(ok)
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["schema"], "protocol_payment.v1")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["payment_method"], "blik")
        self.assertEqual(payload["url"], "https://pay.example/abc/long")
        self.assertEqual(payload["link_type"], "blik_protocol")
        self.assertEqual(payload["operation"], "extract_link")

    def test_failure_emits_error_contract(self):
        lines, writer = _collect()
        reporter = ProtocolResultReporter("ideal", writer=writer)
        reporter.failure("boom", error_code="extractor_failed", error_stage="checkout")
        payload = json.loads(lines[0])
        self.assertEqual(payload["schema"], "protocol_payment.v1")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["payment_method"], "ideal")
        self.assertEqual(payload["error_code"], "extractor_failed")
        self.assertEqual(payload["error_stage"], "checkout")
        self.assertEqual(payload["error"], "boom")

    def test_failure_error_is_truncated_to_600_chars(self):
        lines, writer = _collect()
        reporter = ProtocolResultReporter("twint", writer=writer)
        long_error = "x" * 2000
        reporter.failure(long_error)
        payload = json.loads(lines[0])
        self.assertLessEqual(len(payload["error"]), 600)
        self.assertEqual(payload["error"], "x" * 600)

    def test_emitted_once_invariant(self):
        lines, writer = _collect()
        reporter = ProtocolResultReporter("blik", writer=writer)
        first = reporter.success("https://a")
        second = reporter.success("https://b")
        self.assertTrue(first)
        self.assertFalse(second)  # second call must be a no-op
        self.assertEqual(len(lines), 1)

    def test_missing_output_fallback_uses_distinct_error_code(self):
        lines, writer = _collect()
        reporter = ProtocolResultReporter("blik", writer=writer)
        reporter.ensure_terminal(1)
        payload = json.loads(lines[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "extractor_output_missing")


class RedactionTests(unittest.TestCase):
    def test_sanitize_text_masks_bearer_and_jwt_and_proxy_auth(self):
        text = (
            "auth Bearer eyJabc.def.ghi "
            "proxy http://user:secret@gw.example.com:9000 "
            "key sk_live_1234567890abcdef"
        )
        clean = sanitize_text(text)
        self.assertNotIn("eyJabc", clean)
        self.assertNotIn("secret@gw.example.com", clean)
        self.assertNotIn("sk_live_1234567890abcdef", clean)
        self.assertIn("[REDACTED]", clean)

    def test_sanitize_payload_masks_token_keys(self):
        data = {
            "access_token": "at_123",
            "nested": {"refresh_token": "rt_456"},
            "public": "visible",
        }
        clean = sanitize_payload(data)
        self.assertEqual(clean["access_token"], "[REDACTED]")
        self.assertEqual(clean["nested"]["refresh_token"], "[REDACTED]")
        self.assertEqual(clean["public"], "visible")

    def test_success_redacts_secrets_in_message_field(self):
        lines, writer = _collect()
        reporter = ProtocolResultReporter("blik", writer=writer)
        reporter.success("https://pay", message="access_token=at_live_verysecret")
        payload = json.loads(lines[0])
        self.assertIn("[REDACTED]", payload["message"])
        self.assertNotIn("at_live_verysecret", payload["message"])


if __name__ == "__main__":
    unittest.main()
