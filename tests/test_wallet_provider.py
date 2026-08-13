import json
import unittest
from pathlib import Path

from sms_tool.wallet_provider import (
    WALLET_METHODS,
    WalletCancelledError,
    WalletProviderError,
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

    def update_checkout(self, request):
        self.calls.append(("promotion", request))
        return self.responses.get("promotion", {"success": True})

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
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["operation"], "payment_method_capability_probe")
                self.assertEqual(result["classification"], "eligible")
                self.assertTrue(result["eligible"])
                self.assertTrue(result["conclusive"])
                self.assertEqual(result["capability"]["classification"], "eligible")
                self.assertTrue(result["capability"]["conclusive"])
                self.assertTrue(result["capability"]["supported"])
                self.assertEqual(result["capability"]["amount_minor"], 0)
                self.assertEqual(result["capability"]["currency"], fixture["profile"]["currency"])
                self.assertTrue(result["checkout_session_id_present"])
                self.assertNotIn("checkout_session_id_hint", result)
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

    def test_require_zero_updates_checkout_before_stripe_init(self):
        fixture = load_fixture("gopay")
        transport = FixtureTransport(fixture)

        result = run_wallet_provider(
            "gopay",
            "fixture-access-token",
            transport,
            probe_only=True,
            require_zero=True,
            transport_context={"promotion_proxy": "http://th-promotion.test:80"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            [name for name, _ in transport.calls],
            ["checkout", "promotion", "stripe_init"],
        )
        promotion = transport.calls[1][1]
        self.assertEqual(promotion.stage, "promotion")
        self.assertEqual(
            promotion.payload["checkout_session_id"],
            fixture["responses"]["checkout"]["checkout_session_id"],
        )
        self.assertEqual(promotion.payload["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")

    def test_configured_promotion_rejection_stops_before_stripe_init(self):
        fixture = load_fixture("gopay")
        fixture["responses"]["promotion"] = {"success": False}
        transport = FixtureTransport(fixture)

        result = run_wallet_provider(
            "gopay",
            "fixture-access-token",
            transport,
            probe_only=True,
            promotion_update=True,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "wallet_promotion_rejected")
        self.assertEqual(result["error_stage"], "promotion")
        self.assertEqual([name for name, _ in transport.calls], ["checkout", "promotion"])

    def test_grabpay_zero_due_does_not_use_gopay_promotion_stage(self):
        fixture = load_fixture("grabpay")
        transport = FixtureTransport(fixture)

        result = run_wallet_provider(
            "grabpay",
            "fixture-access-token",
            transport,
            probe_only=True,
            require_zero=True,
            transport_context={"promotion_proxy": "http://promotion-th.test:80"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual([name for name, _ in transport.calls], ["checkout", "stripe_init"])

    def test_approve_and_poll_retries_receive_incrementing_attempts(self):
        fixture = load_fixture("gopay")
        final_poll = fixture["responses"]["poll"]
        fixture["responses"]["approve"] = [
            {"result": "pending"},
            {"result": "approved"},
        ]
        fixture["responses"]["poll"] = [
            {"status": "requires_action"},
            final_poll,
        ]
        transport = FixtureTransport(fixture)

        result = run_wallet_provider(
            "gopay",
            "fixture-access-token",
            transport,
            sleep=lambda _: None,
        )

        self.assertTrue(result["ok"])
        approve_attempts = [request.attempt for name, request in transport.calls if name == "approve"]
        poll_attempts = [request.attempt for name, request in transport.calls if name == "poll"]
        self.assertEqual(approve_attempts, [1, 2])
        self.assertEqual(poll_attempts, [1, 2])

    def test_ineligible_probe_is_conclusive_but_does_not_execute_payment(self):
        fixture = load_fixture("grabpay")
        fixture["responses"]["stripe_init"] = {
            "total_summary": {"due": 0},
            "currency": "php",
            "payment_method_types": ["card", "gopay"],
        }
        transport = FixtureTransport(fixture)

        result = run_wallet_provider("grabpay", "fixture-access-token", transport, probe_only=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["capability"]["classification"], "ineligible")
        self.assertFalse(result["capability"]["supported"])
        self.assertEqual([name for name, _ in transport.calls], ["checkout", "stripe_init"])

    def test_full_flow_stops_when_wallet_is_conclusively_unavailable(self):
        fixture = load_fixture("grabpay")
        fixture["responses"]["stripe_init"] = {
            "total_summary": {"due": 0},
            "currency": "php",
            "payment_method_types": ["card", "gopay"],
        }
        transport = FixtureTransport(fixture)

        result = run_wallet_provider("grabpay", "fixture-access-token", transport)

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

    def test_gopay_probe_fails_closed_on_wrong_currency(self):
        fixture = load_fixture("gopay")
        fixture["responses"]["stripe_init"]["currency"] = "usd"
        transport = FixtureTransport(fixture)

        result = run_wallet_provider("gopay", "fixture-access-token", transport, probe_only=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "checkout_currency_mismatch")
        self.assertFalse(result["eligible"])
        self.assertEqual([name for name, _ in transport.calls], ["checkout", "stripe_init"])

    def test_gopay_full_flow_rejects_missing_currency_before_side_effects(self):
        fixture = load_fixture("gopay")
        fixture["responses"]["stripe_init"].pop("currency", None)
        transport = FixtureTransport(fixture)

        result = run_wallet_provider("gopay", "fixture-access-token", transport)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "wallet_checkout_currency_unknown")
        self.assertEqual(result["error_stage"], "stripe_init")
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

    def test_post_approval_poll_exception_is_unknown_and_requires_reconciliation(self):
        fixture = load_fixture("grabpay")

        class BrokenPollTransport(FixtureTransport):
            def poll_payment(self, request):
                self.calls.append(("poll", request))
                raise ValueError("poll response could not be decoded")

        result = run_wallet_provider("grabpay", "fixture-access-token", BrokenPollTransport(fixture))

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_stage"], "poll")
        self.assertFalse(result["retryable"])
        self.assertTrue(result["side_effect_started"])
        self.assertTrue(result["requires_reconciliation"])

    def test_post_approval_redirect_exception_is_unknown_and_requires_reconciliation(self):
        fixture = load_fixture("gopay")

        class BrokenRedirectTransport(FixtureTransport):
            def follow_redirect(self, request):
                self.calls.append(("follow", request))
                raise WalletProviderError(
                    "redirect response was malformed",
                    error_code="wallet_redirect_invalid",
                    error_stage="follow_redirect",
                )

        result = run_wallet_provider("gopay", "fixture-access-token", BrokenRedirectTransport(fixture))

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_stage"], "follow_redirect")
        self.assertFalse(result["retryable"])
        self.assertTrue(result["side_effect_started"])
        self.assertTrue(result["requires_reconciliation"])

    def test_transport_cancellation_preserves_cancelled_terminal_state(self):
        fixture = load_fixture("gopay")

        class CancelledTransport(FixtureTransport):
            def approve_checkout(self, request):
                self.calls.append(("approve", request))
                raise WalletCancelledError("operator cancelled", error_stage="approve")

        result = run_wallet_provider("gopay", "fixture-access-token", CancelledTransport(fixture))

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
            "client_secret=pi_secret_value cs_live_session_identifier_123456 "
            "pi_test_payment_intent_identifier_123456"
        )
        for secret in (
            "bearer-secret", "user", "pass", "pi_secret_value",
            "cs_live_session_identifier_123456", "pi_test_payment_intent_identifier_123456",
        ):
            self.assertNotIn(secret, redacted)


if __name__ == "__main__":
    unittest.main()
