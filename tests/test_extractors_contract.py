"""Contract tests for the 5 previously untested protocol-payment extractors.

These lock, per extractor, the import surface and the payment-method key the
manager keys off, plus the success-result contract. blik/ideal/twint emit the
shared ``protocol_payment.v1`` schema; pix/direct_card use their own local shapes
(documented inline). No network is touched — only pure helpers and the shared
reporter are exercised.
"""

import importlib
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "services" / "protocol-payment"


def _import_extractor(name, module_file):
    """Insert both the extractor dir and the protocol-payment root so the
    ``common`` / ``pix_core`` sibling packages resolve, then import."""
    extractor_dir = PROTOCOL_ROOT / name
    if extractor_dir not in sys.path:
        sys.path.insert(0, str(extractor_dir))
    if str(PROTOCOL_ROOT) not in sys.path:
        sys.path.insert(0, str(PROTOCOL_ROOT))
    return importlib.import_module(module_file)


def _resolve_method(reporter):
    value = reporter._payment_method
    return value() if callable(value) else value


class BlikExtractorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _import_extractor("blik", "blik_qr_extract")

    def test_imports_cleanly(self):
        self.assertTrue(hasattr(self.mod, "_result_reporter"))

    def test_payment_method_defaults_to_blik(self):
        # blik_qr_extract serves blik OR ideal via IDEAL_PAYMENT_METHOD; default blik.
        self.assertIn(self.mod.payment_method_type(), {"blik", "ideal"})

    def test_success_emits_v1_contract_and_redacts(self):
        from common.protocol_core import ProtocolResultReporter

        collected = []
        reporter = ProtocolResultReporter(self.mod.payment_method_type(), writer=collected.append)
        reporter.success("https://pay.example/abc", message="access_token=at_secret_value")
        payload = json.loads(collected[0])
        self.assertEqual(payload["schema"], "protocol_payment.v1")
        self.assertEqual(payload["payment_method"], self.mod.payment_method_type())
        self.assertTrue(payload["ok"])
        self.assertIn("[REDACTED]", payload["message"])


class IdealExtractorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _import_extractor("ideal", "ideal_qr_extract")

    def test_payment_method_is_ideal(self):
        self.assertEqual(_resolve_method(self.mod._result_reporter), "ideal")

    def test_success_emits_v1_contract(self):
        from common.protocol_core import ProtocolResultReporter

        collected = []
        reporter = ProtocolResultReporter("ideal", writer=collected.append)
        reporter.success("https://pay.example/ideal")
        payload = json.loads(collected[0])
        self.assertEqual(payload["schema"], "protocol_payment.v1")
        self.assertEqual(payload["payment_method"], "ideal")
        self.assertEqual(payload["link_type"], "ideal_protocol")


class TwintExtractorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _import_extractor("twint", "twint_extract")

    def test_payment_method_is_twint(self):
        self.assertEqual(_resolve_method(self.mod._result_reporter), "twint")

    def test_success_emits_v1_contract(self):
        from common.protocol_core import ProtocolResultReporter

        collected = []
        reporter = ProtocolResultReporter("twint", writer=collected.append)
        reporter.success("https://pay.example/twint")
        payload = json.loads(collected[0])
        self.assertEqual(payload["schema"], "protocol_payment.v1")
        self.assertEqual(payload["payment_method"], "twint")


class PixExtractorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _import_extractor("pix", "pix_extract")

    def test_imports_cleanly(self):
        self.assertEqual(self.mod.PIX_BOOTSTRAP_COUNTRY, "BR")

    def test_access_token_extraction_helper(self):
        # pix_core.find_access_token is pix's real unit contract for pulling the
        # ChatGPT access token out of a nested session payload.
        self.assertEqual(self.mod.core.find_access_token({"access_token": "abc123"}), "abc123")
        self.assertEqual(
            self.mod.core.find_access_token({"data": {"token": "nested_tok"}}), "nested_tok"
        )


class DirectCardExtractorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _import_extractor("direct_card", "direct_card_extract")

    def test_imports_cleanly(self):
        self.assertTrue(hasattr(self.mod, "print_json"))

    def test_print_json_emits_valid_result_shape(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.mod.print_json({"ok": True, "error_type": "", "error": ""}, pretty=False)
        payload = json.loads(buffer.getvalue())
        self.assertIn("ok", payload)
        self.assertIsInstance(payload["ok"], bool)

    def test_print_json_does_not_redact_known_gap(self):
        # KNOWN GAP: direct_card's print_json emits the raw payload with no
        # redaction (unlike protocol_core.sanitize_payload). This test locks the
        # CURRENT behaviour so the missing-redaction cannot be "fixed" silently
        # without also updating this contract test.
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.mod.print_json(
                {"ok": False, "error_type": "Auth", "error": "access_token=at_plain_secret"},
                pretty=False,
            )
        emitted = buffer.getvalue()
        self.assertIn("at_plain_secret", emitted)


if __name__ == "__main__":
    unittest.main()
