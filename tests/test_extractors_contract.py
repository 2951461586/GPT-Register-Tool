"""Contract tests for the protocol-payment extractor result shapes.

These lock, per extractor, the import surface and the payment-method key the
manager keys off, plus the success-result contract. Every protocol extractor
(blik/ideal/twint/pix/momo/kakao/direct_card) emits the shared
``protocol_payment.v1`` schema; the manager parses that one shape. No network is
touched -- only pure helpers and the shared reporter are exercised.
"""

import ast
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
# ``common`` is only importable once the protocol root is on sys.path (the
# extractors do the same at runtime); the ignore is for the static checker.
if str(PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_ROOT))

from common.protocol_core import ProtocolResultReporter, run_extractor_entrypoint  # type: ignore[import-not-found]  # noqa: E402

#: Every protocol extractor that the manager launches as a subprocess, by the
#: short name used in the parity ratchet and the file that owns its ``__main__``.
_EXTRACTOR_ENTRYPOINTS = {
    "blik": "blik/blik_qr_extract.py",
    "ideal": "ideal/ideal_qr_extract.py",
    "twint": "twint/twint_extract.py",
    "pix": "pix/run_pix.py",
    "momo": "momo/run_momo.py",
    "kakao": "kakao/kakao_extract.py",
    "direct_card": "direct_card/direct_card_extract.py",
}


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
        cls.runner = _import_extractor("pix", "run_pix")

    def test_imports_cleanly(self):
        self.assertEqual(self.mod.PIX_BOOTSTRAP_COUNTRY, "BR")

    def test_access_token_extraction_helper(self):
        # pix_core.find_access_token is pix's real unit contract for pulling the
        # ChatGPT access token out of a nested session payload.
        self.assertEqual(self.mod.core.find_access_token({"access_token": "abc123"}), "abc123")
        self.assertEqual(self.mod.core.find_access_token({"data": {"token": "nested_tok"}}), "nested_tok")

    def test_runner_emits_the_shared_v1_contract(self):
        # run_pix.py wraps generate_opll_pix_long_link's summary in the shared
        # reporter; the manager parses the schema, not pix's private shape.
        self.assertEqual(_resolve_method(self.runner._RESULT_REPORTER), "pix")
        self.assertEqual(self.runner._RESULT_REPORTER._link_type, "pix_protocol")


class MomoRunnerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = _import_extractor("momo", "run_momo")

    def test_runner_emits_the_shared_v1_contract(self):
        self.assertEqual(_resolve_method(self.runner._RESULT_REPORTER), "momo")
        self.assertEqual(self.runner._RESULT_REPORTER._link_type, "momo_protocol_qr")


class KakaoExtractorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _import_extractor("kakao", "kakao_extract")

    def test_result_contract_emits_schema_and_artifacts(self):
        # print_kakao_result emits the shared terminal contract; the whole
        # kakao contract (decision/stage/methods/...) rides as artifacts.
        self.mod._KAKAO_RESULT_REPORTER._emitted = False
        captured = []
        original_writer = self.mod._KAKAO_RESULT_REPORTER._writer
        self.mod._KAKAO_RESULT_REPORTER._writer = captured.append
        try:
            self.mod.print_kakao_result(
                {
                    "ok": True,
                    "payment_method": "kakao",
                    "url": "https://kakao.test/pay",
                    "provider_redirect_url": "https://kakao.test/pay",
                    "link_type": "kakao_protocol_redirect",
                    "decision": "ready",
                    "stage": "approve",
                    "methods": ["kakao_pay"],
                }
            )
        finally:
            self.mod._KAKAO_RESULT_REPORTER._writer = original_writer
        payload = json.loads(captured[0])
        self.assertEqual(payload["schema"], "protocol_payment.v1")
        self.assertEqual(payload["payment_method"], "kakao")
        self.assertEqual(payload["link_type"], "kakao_protocol_redirect")
        self.assertEqual(payload["url"], "https://kakao.test/pay")
        self.assertEqual(payload["methods"], ["kakao_pay"])

    def test_result_contract_failure_carries_decision(self):
        self.mod._KAKAO_RESULT_REPORTER._emitted = False
        captured = []
        original_writer = self.mod._KAKAO_RESULT_REPORTER._writer
        self.mod._KAKAO_RESULT_REPORTER._writer = captured.append
        try:
            self.mod.print_kakao_result(
                {
                    "ok": False,
                    "payment_method": "kakao",
                    "url": "",
                    "link_type": "kakao_protocol",
                    "decision": "kakao_not_enabled",
                    "stage": "stripe_init",
                    "error": "kakao pay is not enabled for this account",
                }
            )
        finally:
            self.mod._KAKAO_RESULT_REPORTER._writer = original_writer
        payload = json.loads(captured[0])
        self.assertEqual(payload["schema"], "protocol_payment.v1")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "kakao_not_enabled")
        self.assertEqual(payload["error_stage"], "stripe_init")


class DirectCardExtractorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _import_extractor("direct_card", "direct_card_extract")

    def test_imports_cleanly(self):
        self.assertTrue(hasattr(self.mod, "print_json"))

    def _capture(self, payload):
        # ``_RESULT_REPORTER`` is an emitted-once singleton (the extractor must
        # print exactly one terminal result); reset the guard so this test can
        # exercise two independent emissions in one process.
        self.mod._RESULT_REPORTER._emitted = False
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.mod.print_json(payload, pretty=False)
        return json.loads(buffer.getvalue())

    def test_print_json_emits_the_shared_v1_contract(self):
        payload = self._capture(
            {
                "ok": True,
                "long_url": "https://chatgpt.com/checkout/openai_llc/oaics_x",
                "amount_minor": 0,
                "error_type": "",
                "error": "",
            }
        )
        self.assertEqual(payload["schema"], "protocol_payment.v1")
        self.assertEqual(payload["payment_method"], "direct_card")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["url"], "https://chatgpt.com/checkout/openai_llc/oaics_x")
        self.assertEqual(payload["link_type"], "direct_card_protocol")
        # the whole payload travels as artifacts
        self.assertEqual(payload["amount_minor"], 0)

    def test_print_json_redacts_error_text(self):
        # Formerly a KNOWN GAP: print_json emitted the raw payload, leaking a
        # credential embedded in an error message. It now emits the shared
        # contract, whose sanitizer redacts it.
        self.mod._RESULT_REPORTER._emitted = False
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.mod.print_json(
                {"ok": False, "error_type": "Auth", "error": "access_token=at_plain_secret"},
                pretty=False,
            )
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["schema"], "protocol_payment.v1")
        self.assertFalse(payload["ok"])
        self.assertIn("[REDACTED]", payload["error"])
        self.assertNotIn("at_plain_secret", buffer.getvalue())


class EntrypointTerminalContractTests(unittest.TestCase):
    """Every protocol extractor must emit exactly one terminal result.

    :class:`ProtocolResultReporter` enforces "at most one";
    :func:`run_extractor_entrypoint` owns "at least one".  The first three tests
    exercise the guard directly; the last pins every extractor's ``__main__`` to
    the guard so the drift that once let pix / kakao / direct_card exit with no
    ``protocol_payment.v1`` line (and made the manager degrade every failure to
    a generic ``extractor_output_missing``) cannot come back.
    """

    @staticmethod
    def _guard(main, error_code="test_entrypoint_exception"):
        captured = []
        reporter = ProtocolResultReporter("test", writer=captured.append)
        exit_code = run_extractor_entrypoint(reporter, main, error_code=error_code)
        return exit_code, captured

    def test_normal_return_without_a_result_emits_a_missing_output_failure(self):
        exit_code, captured = self._guard(lambda: 5)
        self.assertEqual(exit_code, 5)
        self.assertEqual(len(captured), 1)
        payload = json.loads(captured[0])
        self.assertEqual(payload["schema"], "protocol_payment.v1")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "extractor_output_missing")

    def test_a_raised_exception_emits_one_terminal_failure(self):
        def boom():
            raise ValueError("kaboom")

        exit_code, captured = self._guard(boom, error_code="pix_runner_exception")
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(captured), 1)
        payload = json.loads(captured[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "pix_runner_exception")
        self.assertEqual(payload["error_stage"], "entrypoint")
        self.assertIn("ValueError: kaboom", payload["error"])

    def test_an_already_emitted_result_is_never_replaced(self):
        captured = []
        reporter = ProtocolResultReporter("test", writer=captured.append)

        def main():
            reporter.success("https://pay.example/ok")
            return 0

        self.assertEqual(run_extractor_entrypoint(reporter, main), 0)
        self.assertEqual(len(captured), 1)
        self.assertTrue(json.loads(captured[0])["ok"])

    def test_system_exit_and_keyboard_interrupt_propagate_without_a_verdict(self):
        def raise_system_exit():
            raise SystemExit(7)

        def raise_keyboard_interrupt():
            raise KeyboardInterrupt()

        for expected, main in ((SystemExit, raise_system_exit), (KeyboardInterrupt, raise_keyboard_interrupt)):
            with self.subTest(signal=expected.__name__):
                captured = []
                reporter = ProtocolResultReporter("test", writer=captured.append)
                with self.assertRaises(expected):
                    run_extractor_entrypoint(reporter, main)
                self.assertEqual(captured, [])

    def test_every_extractor_main_block_routes_through_the_guard(self):
        for name, relative in _EXTRACTOR_ENTRYPOINTS.items():
            with self.subTest(extractor=name):
                tree = ast.parse((PROTOCOL_ROOT / relative).read_text(encoding="utf-8"))
                guarded = False
                for node in tree.body:
                    if not isinstance(node, ast.If) or "__name__" not in ast.dump(node.test):
                        continue
                    guarded = any(
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id == "run_extractor_entrypoint"
                        for child in ast.walk(node)
                    )
                self.assertTrue(guarded, f"{relative} __main__ does not use run_extractor_entrypoint")


if __name__ == "__main__":
    unittest.main()
