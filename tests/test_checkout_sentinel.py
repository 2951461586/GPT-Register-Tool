"""Checkout-create Sentinel wiring and the read-only payment-methods signal.

The create gate needs the ``chatgpt_checkout`` token *pair*; a lone main token
is answered with ``400 unusual activity`` (reference project + UPI lane). These
tests pin the wiring at the shared ``_checkout_post`` seam and the read-only
signal that survives a refused create.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

from sms_tool import capability_browser as cb
from sms_tool import gen_pp_link, paypal_extract
from sms_tool import payment_capability as pc
from sms_tool.checkout_contract import CheckoutRequestContract, CheckoutSessionContract
from sms_tool.payment_capability import (
    CapabilityProbeError,
    ChatGPTStripeCapabilityTransport,
    payment_method_capability_probe,
)


SENTINEL_PAIR = {
    "OpenAI-Sentinel-Token": "sentinel-token-fixture",
    "OpenAI-Sentinel-SO-Token": "sentinel-so-fixture",
}
CREATE_URL = "https://chatgpt.com/backend-api/payments/checkout"


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# Shared transport seam: paypal_extract._checkout_post
# ---------------------------------------------------------------------------
def test_checkout_create_attaches_sentinel_pair():
    seen = {}

    def fake_post(url, **kwargs):
        seen["headers"] = kwargs["headers"]
        return _Resp(200, {"checkout_session_id": "cs_x"})

    with (
        patch.object(paypal_extract.curl_requests, "post", side_effect=fake_post),
        patch("sms_tool.sentinel.checkout_sentinel_headers", return_value=dict(SENTINEL_PAIR)) as mint,
    ):
        paypal_extract._checkout_post(
            CREATE_URL, {"plan_name": "chatgptplusplan"}, "at", "oai-did=device-1", "http://exit.test:80", 20
        )

    assert seen["headers"]["OpenAI-Sentinel-Token"] == "sentinel-token-fixture"
    assert seen["headers"]["OpenAI-Sentinel-SO-Token"] == "sentinel-so-fixture"
    assert mint.call_args.kwargs["device_id"] == "device-1"


def test_checkout_update_does_not_attach_create_sentinel():
    def fake_post(url, **kwargs):
        return _Resp(200, {"success": True})

    with (
        patch.object(paypal_extract.curl_requests, "post", side_effect=fake_post),
        patch("sms_tool.sentinel.checkout_sentinel_headers", return_value=dict(SENTINEL_PAIR)) as mint,
    ):
        paypal_extract._checkout_post(
            "https://chatgpt.com/backend-api/payments/checkout/update",
            {"plan_name": "chatgptplusplan"},
            "at",
            "oai-did=device-1",
            "",
            20,
        )

    mint.assert_not_called()


def test_checkout_create_without_device_identity_skips_mint():
    def fake_post(url, **kwargs):
        return _Resp(200, {"checkout_session_id": "cs_x"})

    with (
        patch.object(paypal_extract.curl_requests, "post", side_effect=fake_post),
        patch("sms_tool.sentinel.checkout_sentinel_headers", return_value=dict(SENTINEL_PAIR)) as mint,
    ):
        paypal_extract._checkout_post(CREATE_URL, {}, "at", "session=cookie-only", "", 20)

    mint.assert_not_called()


def test_checkout_device_id_prefers_explicit_header_over_cookie():
    assert paypal_extract._checkout_device_id("oai-did=cookie-id", {"OAI-Device-Id": "header-id"}) == "header-id"
    assert paypal_extract._checkout_device_id("oai-did=cookie-id", None) == "cookie-id"
    assert paypal_extract._checkout_device_id("session=no-did", None) == ""


def test_checkout_sentinel_mint_failure_is_advisory():
    def fake_post(url, **kwargs):
        assert "OpenAI-Sentinel-Token" not in kwargs["headers"]
        return _Resp(200, {"checkout_session_id": "cs_x"})

    with (
        patch.object(paypal_extract.curl_requests, "post", side_effect=fake_post),
        patch("sms_tool.sentinel.checkout_sentinel_headers", side_effect=RuntimeError("no node")),
    ):
        response = paypal_extract._checkout_post(CREATE_URL, {}, "at", "oai-did=device-1", "", 20)

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Capability transport reaches the same seam
# ---------------------------------------------------------------------------
def test_capability_transport_attaches_sentinel_pair_to_create():
    seen = {}

    def fake_post(url, **kwargs):
        seen["headers"] = kwargs["headers"]
        return _Resp(
            200,
            {"checkout_session_id": "cs_x", "processor_entity": "openai_ie", "publishable_key": "pk_live_x"},
        )

    contract = CheckoutRequestContract.for_payment_method("direct_card", billing_country="IN", currency="INR")
    with (
        patch.object(paypal_extract.curl_requests, "post", side_effect=fake_post),
        patch("sms_tool.sentinel.checkout_sentinel_headers", return_value=dict(SENTINEL_PAIR)) as mint,
    ):
        ChatGPTStripeCapabilityTransport().create_checkout(
            contract,
            access_token="at",
            auth_context={"device_id": "device-1"},
            proxy="http://exit.test:80",
            timeout=20,
        )

    # The capability transport reaches the shared `paypal_extract._checkout_post`
    # seam (via the `gen_pp_link` re-export), so the pair is attached there.
    assert seen["headers"]["OpenAI-Sentinel-Token"] == "sentinel-token-fixture"
    assert seen["headers"]["OpenAI-Sentinel-SO-Token"] == "sentinel-so-fixture"
    assert mint.call_args.kwargs["device_id"] == "device-1"


# ---------------------------------------------------------------------------
# Read-only payment-methods signal (independent of Checkout create)
# ---------------------------------------------------------------------------
def test_read_payment_methods_signal_parses_payload():
    response = _Resp(200, {"one_click_trial_eligible": True, "payment_method_types": ["paypal", "card"]})
    with patch.object(gen_pp_link, "_checkout_get", return_value=response) as get:
        signal = pc._read_payment_methods_signal(
            access_token="at", auth_context={"device_id": "device-1"}, proxy="http://exit.test:80", timeout=20
        )

    assert signal == {"one_click_trial_eligible": True, "payment_method_types": ["paypal", "card"]}
    assert get.call_args.kwargs["extra_headers"]["OAI-Device-Id"] == "device-1"


def test_read_payment_methods_signal_degrades_to_empty():
    with patch.object(gen_pp_link, "_checkout_get", side_effect=RuntimeError("blocked")):
        assert pc._read_payment_methods_signal(access_token="at", auth_context={}, proxy="", timeout=20) == {}
    with patch.object(gen_pp_link, "_checkout_get", return_value=_Resp(403, {"detail": "blocked"})):
        assert pc._read_payment_methods_signal(access_token="at", auth_context={}, proxy="", timeout=20) == {}


def test_signal_is_attached_to_successful_probe():
    signal = {"one_click_trial_eligible": True, "payment_method_types": ["paypal"]}

    class FakeTransport:
        def create_checkout(self, contract, **kwargs):
            return CheckoutSessionContract("cs_fixture", "openai_ie", "pk_live_fixture")

        def stripe_init(self, contract, checkout, **kwargs):
            return {"currency": "usd", "payment_method_types": ["paypal"], "total_summary": {"due": 0}}

        def payment_methods_signal(self, **kwargs):
            return dict(signal)

    result = payment_method_capability_probe(
        "at", "paypal", transport=cast(Any, FakeTransport()), billing_country="US", currency="USD", require_zero=True
    )

    assert result["ok"] is True
    assert result["one_click_trial_eligible"] is True
    assert result["payment_methods_signal"] == signal


def test_signal_survives_a_refused_checkout():
    class FakeTransport:
        def create_checkout(self, contract, **kwargs):
            raise CapabilityProbeError(
                "blocked",
                error_code="checkout_risk_blocked",
                error_stage="checkout_create",
                retryable=False,
                status="failed",
            )

        def stripe_init(self, contract, checkout, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("stripe init must not run after a refused create")

        def payment_methods_signal(self, **kwargs):
            return {"one_click_trial_eligible": True}

    result = payment_method_capability_probe(
        "at", "direct_card", transport=cast(Any, FakeTransport()), billing_country="IN", currency="INR"
    )

    assert result["ok"] is False
    assert result["error_code"] == "checkout_risk_blocked"
    assert result["payment_methods_signal"] == {"one_click_trial_eligible": True}


def test_signal_absent_when_transport_has_no_seam():
    class FakeTransport:
        def create_checkout(self, contract, **kwargs):
            return CheckoutSessionContract("cs_fixture", "openai_ie", "pk_live_fixture")

        def stripe_init(self, contract, checkout, **kwargs):
            return {"currency": "inr", "payment_method_types": ["upi"], "total_summary": {"due": 0}}

    result = payment_method_capability_probe(
        "at",
        "upi",
        transport=cast(Any, FakeTransport()),
        billing_country="IN",
        currency="INR",
        require_zero=True,
    )

    assert "payment_methods_signal" not in result


# ---------------------------------------------------------------------------
# Browser / external fallback hook (item 4)
# ---------------------------------------------------------------------------
def test_fallback_transport_runs_for_a_risk_block():
    class Primary:
        def create_checkout(self, contract, **kwargs):
            raise CapabilityProbeError(
                "blocked",
                error_code="checkout_risk_blocked",
                error_stage="checkout_create",
                retryable=False,
                status="failed",
            )

    class Fallback:
        def __init__(self):
            self.used = False

        def create_checkout(self, contract, **kwargs):
            self.used = True
            return CheckoutSessionContract("cs_fixture", "openai_ie", "pk_live_fixture")

        def stripe_init(self, contract, checkout, **kwargs):
            return {"currency": "inr", "payment_method_types": ["upi"], "total_summary": {"due": 0}}

        def payment_methods_signal(self, **kwargs):
            return {}

    fallback = Fallback()
    result = payment_method_capability_probe(
        "at",
        "upi",
        transport=cast(Any, Primary()),
        fallback_transport=cast(Any, fallback),
        billing_country="IN",
        currency="INR",
        require_zero=True,
    )

    assert fallback.used is True
    assert result["ok"] is True
    assert result["fallback_used"] is True


def test_fallback_transport_is_not_used_for_a_deterministic_error():
    class Primary:
        def create_checkout(self, contract, **kwargs):
            raise CapabilityProbeError(
                "bad payload",
                error_code="checkout_request_rejected",
                error_stage="checkout_create",
                retryable=False,
                status="failed",
            )

    class Fallback:
        def create_checkout(self, contract, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("fallback must not run for a deterministic error")

    result = payment_method_capability_probe(
        "at",
        "upi",
        transport=cast(Any, Primary()),
        fallback_transport=cast(Any, Fallback()),
        billing_country="IN",
        currency="INR",
    )

    assert result["error_code"] == "checkout_request_rejected"
    assert result["fallback_used"] is False


def test_browser_fallback_transport_is_none_without_a_browser():
    with patch.object(cb, "browser_available", return_value=False):
        assert cb.browser_fallback_transport() is None


def test_browser_transport_delegates_later_stages_to_protocol():
    transport = cb.BrowserCapabilityTransport()
    with patch.object(cb.ChatGPTStripeCapabilityTransport, "stripe_init", return_value={"delegated": True}) as mint:
        assert transport.stripe_init(object(), object(), proxy="", timeout=5) == {"delegated": True}
    mint.assert_called_once()


def test_browser_cookies_keep_oai_did():
    cookies = cb._cookies_from_header("session=abc; oai-did=cookie-did", "device-1")
    names = {cookie["name"] for cookie in cookies}
    assert names == {"session", "oai-did"}
    assert next(c["value"] for c in cookies if c["name"] == "oai-did") == "cookie-did"
