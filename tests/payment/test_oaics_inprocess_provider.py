"""Unit tests for OAICS in-process payment provider and webDeploymentAttestation wire support."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import MagicMock, patch

from sms_tool.pay_link.oaics_provider import (
    OAICS_METHOD_PROFILES,
    ChatGPTOAICSTransport,
    OAICSProviderError,
    OAICSTransportRequest,
    inspect_oaics_state,
    run_oaics_provider,
)
from sms_tool.payment import link_manager as manager
from sms_tool.payment import wire as payment_wire
from sms_tool.payment.checkout_contract import CheckoutRequestContract


class _FakeOAICSTransport:
    """In-memory OAICS transport for deterministic offline testing."""

    def __init__(
        self,
        *,
        checkout: Mapping[str, Any] | None = None,
        update: Mapping[str, Any] | None = None,
        taxes: Mapping[str, Any] | None = None,
        resolve: Mapping[str, Any] | None = None,
        payment_method: Mapping[str, Any] | None = None,
        confirm: Mapping[str, Any] | None = None,
        approve: Mapping[str, Any] | None = None,
        start: Mapping[str, Any] | None = None,
        fail_stage: str = "",
        fail_error: OAICSProviderError | None = None,
    ) -> None:
        self.calls: list[OAICSTransportRequest] = []
        self._responses: dict[str, Mapping[str, Any]] = {
            "checkout": checkout
            or {
                "checkout_session_id": "oaics_test_001",
                "processor_entity": "openai_llc",
                "publishable_key": "pk_live_test_oaics",
            },
            "update": update or {"amount_due": 0, "currency": "EUR"},
            "taxes": taxes or {"ok": True},
            "resolve": resolve or {"amount_due": 0, "currency": "EUR", "payment_method_types": ["card", "ideal"]},
            "payment_method": payment_method or {"id": "pm_test_12345"},
            "confirm": confirm or {"status": "requires_action"},
            "approve": approve or {"redirect_url": "https://pay.openai.com/authorize/ideal_ok"},
            "start": start or {"redirect_url": "https://payment.momo.vn/v2/gateway/pay?t=1&s=2"},
        }
        self._fail_stage = fail_stage
        self._fail_error = fail_error

    def _call(self, request: OAICSTransportRequest) -> Mapping[str, Any]:
        self.calls.append(request)
        if request.stage == self._fail_stage:
            raise self._fail_error or OAICSProviderError(
                f"{request.stage} failed",
                error_code=f"{request.method}_{request.stage}_failed",
                error_stage=request.stage,
                retryable=request.stage not in {"confirm", "approve", "start"},
                status="unknown" if request.stage in {"confirm", "approve", "start"} else "failed",
            )
        return dict(self._responses[request.stage])

    create_checkout = _call
    update_checkout = _call
    update_taxes = _call
    resolve_checkout = _call
    create_payment_method = _call
    confirm_checkout = _call
    approve_checkout = _call
    start_custom_payment = _call


class WebDeploymentAttestationWireTests(unittest.TestCase):
    def test_extract_web_deployment_attestation_handles_json_and_escaped_html(self):
        self.assertEqual(
            payment_wire.extract_web_deployment_attestation('{"webDeploymentAttestation":"att.plain_123"}'),
            "att.plain_123",
        )
        self.assertEqual(
            payment_wire.extract_web_deployment_attestation(r"{\"webDeploymentAttestation\":\"att.escaped_456\"}"),
            "att.escaped_456",
        )
        self.assertEqual(payment_wire.extract_web_deployment_attestation("<html>no token</html>"), "")

    def test_env_override_takes_precedence_and_skips_html_fetch(self):
        with (
            patch.dict(os.environ, {"OAI_WEB_DEPLOYMENT_ATTESTATION": "att.env_override"}, clear=False),
            patch.object(payment_wire, "curl_requests") as mock_curl,
        ):
            token = payment_wire.fetch_web_deployment_attestation(allow_html_fetch=True)
        self.assertEqual(token, "att.env_override")
        mock_curl.get.assert_not_called()

    def test_default_fetch_skips_html_page_when_opt_in_unset(self):
        env_clear = {
            "OAI_WEB_DEPLOYMENT_ATTESTATION": "",
            "MIN_OAICS_ATTESTATION": "",
            "MIN_GCASH_ATTESTATION": "",
            "GPT_ATTESTATION_FETCH": "",
            "TURB_ATTESTATION_FETCH": "",
        }
        with (
            patch.dict(os.environ, env_clear, clear=False),
            patch.object(payment_wire, "curl_requests") as mock_curl,
        ):
            token = payment_wire.fetch_web_deployment_attestation()
        self.assertEqual(token, "")
        mock_curl.get.assert_not_called()

    def test_opt_in_html_fetch_extracts_token(self):
        fake_resp = MagicMock(status_code=200, text='window.__data = {"webDeploymentAttestation":"att.html_789"};')
        mock_curl = MagicMock()
        mock_curl.get.return_value = fake_resp
        env_clear = {
            "OAI_WEB_DEPLOYMENT_ATTESTATION": "",
            "MIN_OAICS_ATTESTATION": "",
            "MIN_GCASH_ATTESTATION": "",
            "GPT_ATTESTATION_FETCH": "1",
        }
        with (
            patch.dict(os.environ, env_clear, clear=False),
            patch.object(payment_wire, "curl_requests", mock_curl),
        ):
            token = payment_wire.fetch_web_deployment_attestation(proxy="http://127.0.0.1:8080")
        self.assertEqual(token, "att.html_789")
        mock_curl.get.assert_called_once()

    def test_checkout_post_attaches_attestation_header_for_checkout_urls(self):
        mock_curl = MagicMock()
        mock_curl.post.return_value = MagicMock(status_code=200)
        with (
            patch.dict(os.environ, {"OAI_WEB_DEPLOYMENT_ATTESTATION": "att.checkout_hdr"}, clear=False),
            patch.object(payment_wire, "curl_requests", mock_curl),
            patch.object(payment_wire, "_checkout_sentinel_headers", return_value={}),
        ):
            payment_wire._checkout_post(
                "https://chatgpt.com/backend-api/payments/checkout",
                {"plan_name": "chatgptplusplan"},
                "tok",
                "oai-did=dev-1",
                "",
                15,
            )
        sent_headers = mock_curl.post.call_args.kwargs["headers"]
        self.assertEqual(sent_headers.get("oai-web-deployment-attestation"), "att.checkout_hdr")


class OAICSInProcessProviderTests(unittest.TestCase):
    def test_custom_cpmt_flow_for_momo_extracts_redirect_and_qr(self):
        gateway = "https://payment.momo.vn/v2/gateway/pay?t=1&s=2"
        transport = _FakeOAICSTransport(
            resolve={
                "amount_due": 0,
                "currency": "VND",
                "custom_payment_methods": [{"id": "cpmt_momo_vn", "display_name": "MoMo"}],
            },
            start={"redirect_url": gateway, "qr_data": gateway},
        )
        result = run_oaics_provider("momo", "tok_momo", transport)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["payment_method"], "momo")
        self.assertEqual(result["url"], gateway)
        self.assertEqual(result["qr_data"], gateway)
        self.assertEqual(result["session_type"], "oaics")
        self.assertEqual(
            [c.stage for c in transport.calls],
            ["checkout", "update", "taxes", "resolve", "confirm", "start"],
        )
        start_req = transport.calls[-1]
        self.assertEqual(start_req.payload["custom_payment_method_type_id"], "cpmt_momo_vn")

    def test_stripe_pm_flow_for_ideal_runs_pm_confirm_and_approve(self):
        transport = _FakeOAICSTransport(
            resolve={"amount_due": 0, "currency": "EUR", "payment_method_types": ["card", "ideal"]},
            confirm={"status": "requires_approval"},
            approve={"next_action": {"redirect_to_url": {"url": "https://checkout.stripe.com/ideal/redirect"}}},
        )
        result = run_oaics_provider("ideal", "tok_ideal", transport)
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://checkout.stripe.com/ideal/redirect")
        self.assertEqual(result["link_type"], "ideal_oaics_redirect")
        self.assertEqual(
            [c.stage for c in transport.calls],
            ["checkout", "update", "taxes", "resolve", "payment_method", "confirm", "approve"],
        )

    def test_pix_qr_data_without_redirect_url_succeeds(self):
        emv = "00020126580014br.gov.bcb.pix0136test-pix-key5204000053039865802BR"
        transport = _FakeOAICSTransport(
            resolve={"amount_due": 0, "currency": "BRL", "payment_method_types": ["card", "pix"]},
            confirm={"next_action": {"pix_display_qr_code": {"data": emv}}},
        )
        result = run_oaics_provider("pix", "tok_pix", transport)
        self.assertTrue(result["ok"])
        self.assertEqual(result["qr_data"], emv)
        self.assertEqual(result["link_type"], "pix_oaics_qr")
        self.assertEqual(
            [c.stage for c in transport.calls],
            ["checkout", "update", "taxes", "resolve", "payment_method", "confirm"],
        )

    def test_blik_requires_six_digit_code_and_accepts_completed_without_url(self):
        transport = _FakeOAICSTransport()
        missing = run_oaics_provider("blik", "tok_blik", transport, blik_code="")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error_code"], "blik_code_required")
        self.assertEqual(transport.calls, [])

        ok_transport = _FakeOAICSTransport(
            resolve={"amount_due": 0, "currency": "PLN", "payment_method_types": ["card", "blik"]},
            confirm={"status": "succeeded"},
        )
        completed = run_oaics_provider("blik", "tok_blik", ok_transport, blik_code="123456")
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["operation"], "execute_payment")
        self.assertEqual(completed["url"], "")

    def test_probe_only_does_not_mutate_payment_state(self):
        transport = _FakeOAICSTransport(
            resolve={"amount_due": 0, "currency": "CHF", "payment_method_types": ["card", "twint"]},
        )
        result = run_oaics_provider("twint", "tok_twint", transport, probe_only=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(result["decision"], "payment_method_available")
        self.assertEqual(result["operation"], "payment_method_capability_probe")
        self.assertEqual([c.stage for c in transport.calls], ["checkout", "update", "taxes", "resolve"])

    def test_nonzero_offer_and_unavailable_method_stop_before_side_effects(self):
        nonzero_transport = _FakeOAICSTransport(
            update={"amount_due": 29000, "currency": "KRW"},
            resolve={"amount_due": 29000, "currency": "KRW", "payment_method_types": ["card", "kakao_pay"]},
        )
        nonzero = run_oaics_provider("kakao", "tok_kakao", nonzero_transport)
        self.assertFalse(nonzero["ok"])
        self.assertEqual(nonzero["error_code"], "nonzero_offer")
        self.assertEqual([c.stage for c in nonzero_transport.calls], ["checkout", "update", "taxes", "resolve"])

        missing_transport = _FakeOAICSTransport(
            resolve={"amount_due": 0, "currency": "KRW", "payment_method_types": ["card"]},
        )
        missing = run_oaics_provider("kakao", "tok_kakao", missing_transport)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error_code"], "payment_method_unavailable")

    def test_side_effect_failure_returns_unknown_and_non_retryable(self):
        transport = _FakeOAICSTransport(
            resolve={"amount_due": 0, "currency": "EUR", "payment_method_types": ["card", "ideal"]},
            fail_stage="approve",
        )
        result = run_oaics_provider("ideal", "tok_ideal", transport)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertIs(result["retryable"], False)
        self.assertEqual(result["error_stage"], "approve")

    def test_untrusted_redirect_host_is_refused(self):
        transport = _FakeOAICSTransport(
            resolve={"amount_due": 0, "currency": "CHF", "payment_method_types": ["card", "twint"]},
            confirm={"redirect_url": "https://evil.example.invalid/phish"},
        )
        result = run_oaics_provider("twint", "tok_twint", transport)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "twint_redirect_host_not_allowed")

    def test_inspect_oaics_state_recognizes_all_six_profiles(self):
        self.assertEqual(set(OAICS_METHOD_PROFILES), {"ideal", "pix", "kakao", "blik", "twint", "momo"})
        cap = inspect_oaics_state(
            OAICS_METHOD_PROFILES["kakao"],
            {
                "amount_due": 0,
                "currency": "KRW",
                "custom_payment_methods": [{"id": "cpmt_kakao_kr", "name": "Kakao Pay"}],
            },
        )
        self.assertTrue(cap.method_available)
        self.assertEqual(cap.custom_payment_method_type_id, "cpmt_kakao_kr")
        self.assertTrue(cap.is_zero_due)

    def test_generate_payment_link_dispatches_migrated_method_inprocess_without_subprocess(self):
        transport = _FakeOAICSTransport(
            resolve={"amount_due": 0, "currency": "EUR", "payment_method_types": ["card", "ideal"]},
            confirm={"redirect_url": "https://checkout.stripe.com/ideal/ok"},
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("sms_tool.pay_link.base.current_config_data", return_value={"chatgpt": {}, "protocol_payments": {}}),
            patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"),
            patch.object(manager.payment_egress, "assert_egress_countries"),
            patch("sms_tool.payment.link_manager.subprocess.run") as subproc,
        ):
            result = manager.generate_payment_link(
                "tok_ideal",
                payment_method="ideal",
                checkout_proxy="socks5h://127.0.0.1:1080",
                transport=transport,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://checkout.stripe.com/ideal/ok")
        self.assertEqual(result["session_type"], "oaics")
        self.assertEqual(result["manager_state"], "completed")
        subproc.assert_not_called()

    def test_transport_attaches_attestation_and_approval_sentinel(self):
        transport = ChatGPTOAICSTransport(timeout=20)
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {"redirect_url": "https://checkout.stripe.com/ideal/ok"}
        req = OAICSTransportRequest(
            stage="approve",
            method="ideal",
            contract=CheckoutRequestContract.for_payment_method("ideal"),
            checkout_session_id="oaics_test_99",
            processor_entity="openai_llc",
            access_token="tok_approve",
            payload={"checkout_session_id": "oaics_test_99"},
            auth_context={"device_id": "dev-oaics-1", "cookie_header": "oai-did=dev-oaics-1"},
            transport_context={"web_deployment_attestation": "att_explicit_ctx"},
        )
        issued = MagicMock(token="sent_approval_tok", so_token="sent_so_tok")
        with (
            patch("sms_tool.pay_link.oaics_provider.issue_sentinel_token", return_value=issued) as mock_issue,
            patch.object(payment_wire, "_checkout_post", return_value=fake_resp) as mock_post,
        ):
            data = transport.approve_checkout(req)
        self.assertEqual(data["redirect_url"], "https://checkout.stripe.com/ideal/ok")
        mock_issue.assert_called_once()
        self.assertEqual(mock_issue.call_args.kwargs["flow"], "checkout_session_approval")
        sent_extra = mock_post.call_args.kwargs["extra_headers"]
        self.assertEqual(sent_extra["openai-sentinel-token"], "sent_approval_tok")
        self.assertEqual(sent_extra["openai-sentinel-so-token"], "sent_so_tok")
        self.assertEqual(sent_extra["oai-web-deployment-attestation"], "att_explicit_ctx")


if __name__ == "__main__":
    unittest.main()
