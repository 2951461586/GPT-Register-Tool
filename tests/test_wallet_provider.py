import json
import unittest
from pathlib import Path

from sms_tool.wallet_provider import (
    WALLET_METHODS,
    WalletCancelledError,
    WalletTransportRequest,
    redact_sensitive_text,
    run_wallet_provider,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "wallet_provider"


def load_fixture(method: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{method}.json").read_text(encoding="utf-8"))


def assert_subset(testcase: unittest.TestCase, expected: dict, actual: dict) -> None:
    for key, value in expected.items():
        testcase.assertIn(key, actual)
        if isinstance(value, dict):
            testcase.assertIsInstance(actual[key], dict)
            assert_subset(testcase, value, actual[key])
        else:
            testcase.assertEqual(actual[key], value)


class FixtureTransport:
    def __init__(self, fixture: dict):
        self.responses = fixture["responses"]
        self.calls: list[tuple[str, WalletTransportRequest]] = []

    def _record(self, name: str, request: WalletTransportRequest):
        self.calls.append((name, request))
        response = self.responses[name]
        if isinstance(response, list):
            index = sum(1 for call_name, _ in self.calls if call_name == name) - 1
            return response[min(index, len(response) - 1)]
        return response

    def create_checkout(self, request):
        return self._record("checkout", request)

    def stripe_init(self, request):
        return self._record("stripe_init", request)

    def create_payment_method(self, request):
        return self._record("payment_method", request)

    def confirm_payment(self, request):
        return self._record("confirm", request)

    def approve_checkout(self, request):
        return self._record("approve", request)

    def poll_payment(self, request):
        return self._record("poll", request)

    def follow_redirect(self, request):
        return self._record("follow", request)


class WalletProviderContractTests(unittest.TestCase):
    def test_method_specs_use_shared_profiles(self):
        expected = {
            "gopay": ("ID", "IDR", "id"),
            "gcash": ("PH", "PHP", "en-PH"),
            "grabpay": ("PH", "PHP", "en-PH"),
        }
        self.assertEqual(set(WALLET_METHODS), set(expected))
        for method, values in expected.items():
            spec = WALLET_METHODS[method]
            self.assertEqual((spec.country, spec.currency, spec.locale), values)

    def test_probe_only_matches_checkout_and_stripe_init_fixtures(self):
        for method in WALLET_METHODS:
            with self.subTest(method=method):
                fixture = load_fixture(method)
                transport = FixtureTransport(fixture)
                result = run_wallet_provider(
                    method,
                    "fixture-access-token",
                    transport,
                    probe_only=True,
                    sleep=lambda _: None,
                )

                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], "probe_complete")
                self.assertEqual(result["operation"], "probe")
                self.assertEqual(result["capability"]["classification"], "eligible")
                self.assertTrue(result["capability"]["conclusive"])
                self.assertTrue(result["capability"]["supported"])
                self.assertEqual(result["capability"]["amount_minor"], 0)
                self.assertEqual(result["capability"]["currency"], fixture["profile"]["currency"])
                self.assertEqual([name for name, _ in transport.calls], ["checkout", "stripe_init"])
                assert_subset(self, fixture["expected"]["checkout"], transport.calls[0][1].payload)
                assert_subset(self, fixture["expected"]["stripe_init_subset"], transport.calls[1][1].payload)

    def test_full_flow_matches_wallet_request_contract_fixtures(self):
        expected_stages = [
            "checkout",
            "stripe_init",
            "payment_method",
            "confirm",
            "approve",
            "poll",
            "follow",
        ]
        for method in WALLET_METHODS:
            with self.subTest(method=method):
                fixture = load_fixture(method)
                transport = FixtureTransport(fixture)
                result = run_wallet_provider(
                    method,
                    "fixture-access-token",
                    transport,
                    sleep=lambda _: None,
                )

                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["provider_redirect_url"], fixture["responses"]["follow"]["final_url"])
                self.assertEqual([name for name, _ in transport.calls], expected_stages)
                calls = {name: request for name, request in transport.calls}
                assert_subset(self, fixture["expected"]["payment_method_subset"], calls["payment_method"].payload)
                assert_subset(self, fixture["expected"]["confirm_subset"], calls["confirm"].payload)
                self.assertEqual(
                    calls["approve"].payload,
                    {
                        "checkout_session_id": fixture["responses"]["checkout"]["checkout_session_id"],
                        "processor_entity": fixture["responses"]["checkout"]["processor_entity"],
                    },
                )
                self.assertEqual(
                    calls["follow"].redirect_url,
                    fixture["responses"]["poll"]["next_action"]["redirect_to_url"]["url"]
                    if "next_action" in fixture["responses"]["poll"]
                    else fixture["responses"]["poll"]["payment_intent"]["next_action"]["redirect_to_url"]["url"],
                )

    def test_ineligible_probe_is_conclusive_but_does_not_execute_payment(self):
        fixture = load_fixture("gcash")
        fixture["responses"]["stripe_init"] = {
            "total_summary": {"due": 0},
            "currency": "php",
            "payment_method_types": ["card", "grabpay"],
        }
        transport = FixtureTransport(fixture)

        result = run_wallet_provider("gcash", "fixture-access-token", transport, probe_only=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["capability"]["classification"], "ineligible")
        self.assertFalse(result["capability"]["supported"])
        self.assertEqual([name for name, _ in transport.calls], ["checkout", "stripe_init"])

    def test_full_flow_stops_when_wallet_is_conclusively_unavailable(self):
        fixture = load_fixture("gcash")
        fixture["responses"]["stripe_init"] = {
            "total_summary": {"due": 0},
            "currency": "php",
            "payment_method_types": ["card", "grabpay"],
        }
        transport = FixtureTransport(fixture)

        result = run_wallet_provider("gcash", "fixture-access-token", transport)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "wallet_method_unavailable")
        self.assertEqual(result["error_stage"], "stripe_init")
        self.assertFalse(result["retryable"])
        self.assertEqual([name for name, _ in transport.calls], ["checkout", "stripe_init"])

    def test_full_flow_stops_when_capability_evidence_is_inconclusive(self):
        fixture = load_fixture("gopay")
        fixture["responses"]["stripe_init"] = {
            "total_summary": {"due": 0},
            "currency": "idr",
        }
        transport = FixtureTransport(fixture)

        result = run_wallet_provider("gopay", "fixture-access-token", transport)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "wallet_capability_unknown")
        self.assertTrue(result["requires_reconciliation"])
        self.assertFalse(result["retryable"])
        self.assertEqual([name for name, _ in transport.calls], ["checkout", "stripe_init"])


class WalletProviderFailureTests(unittest.TestCase):
    def test_transport_timeout_is_typed_and_redacted(self):
        fixture = load_fixture("gopay")

        class TimeoutTransport(FixtureTransport):
            def stripe_init(self, request):
                self.calls.append(("stripe_init", request))
                raise TimeoutError(
                    "access_token=very-secret-token publishable_key=pk_test_very_secret_key_123456"
                )

        result = run_wallet_provider("gopay", "very-secret-token", TimeoutTransport(fixture), probe_only=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["error_stage"], "stripe_init")
        self.assertTrue(result["retryable"])
        self.assertNotIn("very-secret-token", result["error"])
        self.assertNotIn("pk_test_very_secret_key_123456", result["error"])

    def test_uncertain_post_confirm_transport_failure_is_unknown(self):
        fixture = load_fixture("grabpay")

        class BrokenConfirmTransport(FixtureTransport):
            def confirm_payment(self, request):
                self.calls.append(("confirm", request))
                raise ConnectionError("connection closed after request upload")

        result = run_wallet_provider("grabpay", "fixture-access-token", BrokenConfirmTransport(fixture))

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_stage"], "confirm")
        self.assertFalse(result["retryable"])
        self.assertTrue(result["requires_reconciliation"])

    def test_post_confirm_timeout_is_unknown_and_not_safe_to_retry(self):
        fixture = load_fixture("grabpay")

        class TimedOutConfirmTransport(FixtureTransport):
            def confirm_payment(self, request):
                self.calls.append(("confirm", request))
                raise TimeoutError("confirm response was not received")

        result = run_wallet_provider("grabpay", "fixture-access-token", TimedOutConfirmTransport(fixture))

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_stage"], "confirm")
        self.assertFalse(result["retryable"])
        self.assertTrue(result["requires_reconciliation"])

    def test_transport_cancellation_preserves_cancelled_terminal_state(self):
        fixture = load_fixture("gcash")

        class CancelledTransport(FixtureTransport):
            def approve_checkout(self, request):
                self.calls.append(("approve", request))
                raise WalletCancelledError("operator cancelled", error_stage="approve")

        result = run_wallet_provider("gcash", "fixture-access-token", CancelledTransport(fixture))

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error_stage"], "approve")
        self.assertFalse(result["retryable"])

    def test_request_repr_and_redactor_do_not_expose_credentials(self):
        fixture = load_fixture("gopay")
        transport = FixtureTransport(fixture)
        run_wallet_provider("gopay", "fixture-access-token", transport, probe_only=True)

        rendered = repr(transport.calls[0][1])
        self.assertNotIn("fixture-access-token", rendered)
        self.assertNotIn("pk_test_fixture", repr(transport.calls[1][1]))
        redacted = redact_sensitive_text(
            "Authorization: Bearer bearer-secret proxy=http://user:pass@example.test:8080 "
            "client_secret=pi_secret_value"
        )
        for secret in ("bearer-secret", "user", "pass", "pi_secret_value"):
            self.assertNotIn(secret, redacted)


if __name__ == "__main__":
    unittest.main()
