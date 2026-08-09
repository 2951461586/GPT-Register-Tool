import json
import unittest
from pathlib import Path

from sms_tool.gcash_provider import (
    GCashProviderError,
    GCashTransportRequest,
    run_gcash_provider,
)


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "gcash_provider" / "custom_flow.json").read_text(encoding="utf-8")
)


class FixtureTransport:
    def __init__(self):
        self.calls: list[tuple[str, GCashTransportRequest]] = []

    def _response(self, name, request):
        self.calls.append((name, request))
        return FIXTURE["responses"][name]

    def create_checkout(self, request):
        return self._response("checkout", request)

    def update_checkout(self, request):
        return self._response("update", request)

    def update_taxes(self, request):
        return self._response("taxes", request)

    def resolve_checkout(self, request):
        return self._response("resolve", request)

    def probe_custom_payment(self, request):
        return self._response("custom_capability", request)

    def confirm_custom_payment(self, request):
        return self._response("confirm", request)

    def start_custom_payment(self, request):
        return self._response("start", request)


class GCashProviderTests(unittest.TestCase):
    def test_probe_stops_before_payment_side_effects(self):
        transport = FixtureTransport()

        result = run_gcash_provider(
            "fixture-access-token",
            transport,
            probe_only=True,
            custom_payment_method_type_id=FIXTURE["custom_payment_method_type_id"],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["operation"], "payment_method_capability_probe")
        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(result["amount"], 0)
        self.assertEqual(
            [name for name, _ in transport.calls],
            ["checkout", "update", "taxes", "resolve", "custom_capability"],
        )
        checkout_request = transport.calls[0][1]
        self.assertTrue(checkout_request.payload["check_card_proxy"])
        device_ids = {request.auth_context.get("device_id") for _, request in transport.calls}
        self.assertEqual(len(device_ids), 1)
        self.assertTrue(next(iter(device_ids)))

    def test_full_flow_uses_custom_payment_method_contract(self):
        transport = FixtureTransport()

        result = run_gcash_provider(
            "fixture-access-token",
            transport,
            custom_payment_method_type_id=FIXTURE["custom_payment_method_type_id"],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "gcash_adyen_redirect")
        self.assertIn("checkoutshopper-live.adyen.com", result["url"])
        self.assertEqual(
            [name for name, _ in transport.calls],
            ["checkout", "update", "taxes", "resolve", "custom_capability", "confirm", "start"],
        )
        calls = {name: request for name, request in transport.calls}
        self.assertEqual(calls["confirm"].payload["type"], "custom_payment_method")
        self.assertEqual(
            calls["confirm"].payload["selected_payment_method_type"],
            FIXTURE["custom_payment_method_type_id"],
        )
        self.assertEqual(
            calls["start"].payload["custom_payment_method_type_id"],
            FIXTURE["custom_payment_method_type_id"],
        )

    def test_confirm_409_is_a_known_failure_not_an_unknown_retry(self):
        class ConflictTransport(FixtureTransport):
            def confirm_custom_payment(self, request):
                self.calls.append(("confirm", request))
                raise GCashProviderError(
                    "GCash checkout was not confirmed",
                    error_code="gcash_checkout_not_confirmed",
                    error_stage="confirm",
                    retryable=False,
                    status="failed",
                )

        result = run_gcash_provider(
            "fixture-access-token",
            ConflictTransport(),
            custom_payment_method_type_id=FIXTURE["custom_payment_method_type_id"],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "gcash_checkout_not_confirmed")
        self.assertFalse(result["retryable"])

    def test_redirect_host_allowlist_rejects_untrusted_url(self):
        class BadRedirectTransport(FixtureTransport):
            def start_custom_payment(self, request):
                self.calls.append(("start", request))
                return {"status": "requires_action", "next_action": {"url": "https://attacker.example/pay"}}

        result = run_gcash_provider(
            "fixture-access-token",
            BadRedirectTransport(),
            custom_payment_method_type_id=FIXTURE["custom_payment_method_type_id"],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "gcash_redirect_host_not_allowed")

    def test_request_repr_does_not_expose_access_token(self):
        transport = FixtureTransport()
        run_gcash_provider(
            "fixture-access-token",
            transport,
            probe_only=True,
            custom_payment_method_type_id=FIXTURE["custom_payment_method_type_id"],
        )
        self.assertNotIn("fixture-access-token", repr(transport.calls[0][1]))

    def test_capability_prefers_checkout_type_over_configured_fallback(self):
        transport = FixtureTransport()
        result = run_gcash_provider(
            "fixture-access-token",
            transport,
            probe_only=True,
            custom_payment_method_type_id="cpmt_configured_fallback",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(
            result["custom_payment_method_type_id"],
            "cpmt_fixture_gcash",
        )


if __name__ == "__main__":
    unittest.main()
