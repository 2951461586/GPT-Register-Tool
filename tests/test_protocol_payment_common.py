import importlib.util
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


if __name__ == "__main__":
    unittest.main()
