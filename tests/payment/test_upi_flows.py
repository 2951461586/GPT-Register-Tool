"""Behaviour tests for ``sms_tool/upi_link/flows.py`` (measured 46% at 2026-10-23).

Why this exists
---------------
The two flows in this module are the *payment result* builders: one goes through
the server-advertised UPI custom method (``cpmt``), the other through the
OAICS/deferred rail (``confirmation_tokens`` -> ChatGPT confirm -> PaymentIntent).
Every branch here decides what the operator sees as a link, or which
``error_code`` they get, so a wrong branch is a wrong payment outcome -- and the
module was the thinnest-covered part of the payment path.

What is stubbed and why
-----------------------
Only the *outside* is stubbed:

* ``_upi_sentinel_headers`` -- the Sentinel mint is advisory and needs a live
  session; the flows must still run with an empty header set, which is itself one
  of the properties pinned below.
* ``_upi_hydrate_qr_data`` / ``_write_qr_png`` -- hydration fetches a page and the
  PNG writer needs the optional ``qrcode`` extra. Their contracts have their own
  seams (``extract.py`` / ``session.py``); here the flow logic is under test.

The HTTP layer is a recorder, so every assertion is about what the flow *did*
(which URL, which body, which status mapping), not about the transport.
"""

from __future__ import annotations

from typing import Any

import pytest

from sms_tool.upi_link import flows as f


class _Resp:
    def __init__(self, status: int = 200, body: Any = None, text: str = "{}") -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text
        self.headers: dict[str, str] = {}
        self.url = ""

    def json(self) -> Any:
        return self._body


class _Recorder:
    """Session double: records calls and replays queued responses per URL suffix."""

    def __init__(self, responses: dict[str, list[_Resp]] | None = None) -> None:
        self.responses = {key: list(value) for key, value in (responses or {}).items()}
        self.calls: list[tuple[str, str, Any]] = []
        self.headers: dict[str, str] = {}

    def _take(self, url: str) -> _Resp:
        for suffix, queue in self.responses.items():
            if url.endswith(suffix) or suffix in url:
                if not queue:
                    raise AssertionError(f"no queued response left for {url}")
                return queue.pop(0)
        raise AssertionError(f"unexpected request: {url}")

    def post(self, url: str, **kwargs: Any) -> _Resp:
        self.calls.append(("POST", url, kwargs))
        return self._take(url)

    def get(self, url: str, **kwargs: Any) -> _Resp:
        self.calls.append(("GET", url, kwargs))
        return self._take(url)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Every test runs with an empty Sentinel header set and no real QR IO."""
    monkeypatch.setattr(f, "_upi_sentinel_headers", lambda *args, **kwargs: {})
    monkeypatch.setattr(f, "_upi_hydrate_qr_data", lambda data, proxy_url, fingerprint=None: dict(data))
    monkeypatch.setattr(f, "_write_qr_png", lambda data, path="": f"/tmp/{'qr'}.png" if data else "")
    monkeypatch.setattr(f.time, "sleep", lambda seconds: None)


def _cpmt(recorder: _Recorder, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "access_token": "at-test",
        "cs_id": "cs_live_1",
        "cpm_id": "cpmt_upi_1",
        "processor_entity": "openai",
        "amount": 19900,
        "payment_currency": "inr",
        "target_country": "IN",
        "checkout_country": "IN",
        "payment_country": "IN",
        "device_id": "did-1",
        "checkout_proxy": "http://checkout:1",
        "qr_path": "",
    }
    kwargs.update(overrides)
    return f._upi_run_cpmt_flow(recorder, **kwargs)


# ────────────────────────────── cpmt rail ──────────────────────────────


def test_cpmt_confirm_failure_is_a_checkout_error_with_a_classified_code():
    recorder = _Recorder({"checkout/confirm": [_Resp(400, text="card_declined")]})
    result = _cpmt(recorder)

    assert result["ok"] is False
    assert result["error_code"], "a failed confirm must carry a classified error code"
    assert "cpmt confirm failed" in result["error"]
    assert result["payment_method"] == "upi"
    assert result["cs_id"] == "cs_live_1"
    assert result["cpmt_id"] == "cpmt_upi_1"
    assert [call[0] for call in recorder.calls] == ["POST"], "a failed confirm must not start the method"


def test_cpmt_confirm_blocked_maps_to_provider_declined():
    recorder = _Recorder({"checkout/confirm": [_Resp(200, {"status": "blocked"})]})
    result = _cpmt(recorder)

    assert result["ok"] is False
    assert result["error_code"] == "upi_provider_declined"


def test_cpmt_confirm_with_an_unknown_status_fails_without_starting():
    recorder = _Recorder({"checkout/confirm": [_Resp(200, {"status": "pending"})]})
    result = _cpmt(recorder)

    assert result["ok"] is False
    assert result["error_code"] == "upi_cpmt_confirm_failed"
    assert len(recorder.calls) == 1


def test_cpmt_start_failure_returns_the_start_error():
    recorder = _Recorder(
        {
            "checkout/confirm": [_Resp(200, {"status": "success"})],
            "custom_payment_method/start": [_Resp(500, text="boom")],
        }
    )
    result = _cpmt(recorder)

    assert result["ok"] is False
    assert "cpmt start failed" in result["error"]
    assert result["error_code"]


def test_cpmt_start_blocked_maps_to_provider_declined():
    recorder = _Recorder(
        {
            "checkout/confirm": [_Resp(200, {"status": "success"})],
            "custom_payment_method/start": [_Resp(200, {"status": "blocked"})],
        }
    )
    assert _cpmt(recorder)["error_code"] == "upi_provider_declined"


def test_cpmt_start_without_a_link_is_reported_explicitly():
    recorder = _Recorder(
        {
            "checkout/confirm": [_Resp(200, {"status": "success"})],
            "custom_payment_method/start": [_Resp(200, {"status": "success"})],
        }
    )
    result = _cpmt(recorder)

    assert result["ok"] is False
    assert result["error_code"] == "upi_cpmt_no_link"


def test_cpmt_success_returns_the_link_and_the_full_result_contract():
    link = "upi://pay?pa=merchant@upi&am=199.00"
    recorder = _Recorder(
        {
            "checkout/confirm": [_Resp(200, {"status": "success"})],
            "custom_payment_method/start": [_Resp(200, {"status": "success", "url": link})],
        }
    )
    result = _cpmt(recorder)

    assert result["ok"] is True
    assert result["url"] == link
    assert result["upi_uri"] == link
    assert result["link_type"] == "upi_cpmt_link"
    assert result["payment_method_types"] == ["upi"]
    for key in (
        "hosted_url",
        "instructions_url",
        "qr_data",
        "qr_path",
        "expires_at",
        "amount",
        "currency",
        "target_country",
        "checkout_country",
        "billing_country",
        "payment_country",
        "approval_ok",
        "approval_blocked",
    ):
        assert key in result, key
    assert result["currency"] == "INR", "the currency is upper-cased in the contract"
    confirm_call, start_call = recorder.calls
    assert confirm_call[2]["json"]["selected_payment_method_type"] == "cpmt_upi_1"
    assert start_call[2]["json"]["custom_payment_method_type_id"] == "cpmt_upi_1"
    assert start_call[2]["headers"]["Authorization"] == "Bearer at-test"
    assert start_call[2]["headers"]["x-openai-target-path"].endswith("/custom_payment_method/start")


def test_cpmt_success_without_a_upi_scheme_link_reports_a_hosted_fallback():
    recorder = _Recorder(
        {
            "checkout/confirm": [_Resp(200, {"status": "success"})],
            "custom_payment_method/start": [
                _Resp(200, {"hosted_instructions_url": "https://payments.stripe.com/upi/instructions/cpmt_1"})
            ],
        }
    )
    result = _cpmt(recorder)

    assert result["ok"] is True
    assert result["upi_uri"] == ""
    assert result["instructions_url"] == "https://payments.stripe.com/upi/instructions/cpmt_1"


# ────────────────────────────── OAICS rail ──────────────────────────────

_PI_ID = "pi_3Up1"
_PI_SECRET = f"{_PI_ID}_secret_abc"


def _oaics(stripe: _Recorder, cs: _Recorder, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "access_token": "at-test",
        "device_id": "did-1",
        "cs_id": "oaics_1",
        "stripe_pk": "pk_test_1",
        "processor_entity": "openai",
        "billing": {},
        "fingerprint": {},
        "ctx": {},
        "elements": {},
        "oaics_state": {},
        "amount": 19900,
        "payment_currency": "inr",
        "target_country": "IN",
        "checkout_country": "IN",
        "payment_country": "IN",
        "checkout_proxy": "http://checkout:1",
        "provider_proxy": "http://provider:1",
        "approve_proxy": "http://approve:1",
        "qr_path": "",
        "wait_paid": False,
        "paid_timeout": 0.0,
    }
    kwargs.update(overrides)
    return f._upi_run_oaics_flow(stripe, cs, **kwargs)


def test_oaics_confirmation_token_failure_raises_with_the_status():
    stripe = _Recorder({"confirmation_tokens": [_Resp(402, text="declined")]})
    with pytest.raises(RuntimeError, match="confirmation_tokens failed: 402"):
        _oaics(stripe, _Recorder())


def test_oaics_rejects_a_token_that_is_not_a_ctoken():
    stripe = _Recorder({"confirmation_tokens": [_Resp(200, {"id": "setupintent_1"})]})
    with pytest.raises(RuntimeError, match="did not return ctoken"):
        _oaics(stripe, _Recorder())


def test_oaics_chatgpt_confirm_failure_raises():
    stripe = _Recorder({"confirmation_tokens": [_Resp(200, {"id": "ctoken_1"})]})
    cs = _Recorder({"checkout/confirm": [_Resp(400, text="rejected")]})
    with pytest.raises(RuntimeError, match="chatgpt confirm failed: 400"):
        _oaics(stripe, cs)


def test_oaics_blocked_confirm_raises_the_risk_control_error():
    stripe = _Recorder({"confirmation_tokens": [_Resp(200, {"id": "ctoken_1"})]})
    cs = _Recorder({"checkout/confirm": [_Resp(200, {"status": "blocked"})]})
    with pytest.raises(RuntimeError, match="provider risk control"):
        _oaics(stripe, cs)


def test_oaics_confirm_without_a_payment_intent_raises():
    stripe = _Recorder({"confirmation_tokens": [_Resp(200, {"id": "ctoken_1"})]})
    cs = _Recorder({"checkout/confirm": [_Resp(200, {"status": "success"})]})
    with pytest.raises(RuntimeError, match="missing PaymentIntent"):
        _oaics(stripe, cs)


def test_oaics_payment_intent_failure_raises():
    stripe = _Recorder(
        {
            "confirmation_tokens": [_Resp(200, {"id": "ctoken_1"})],
            "payment_intents/": [_Resp(500, text="pi boom")],
        }
    )
    cs = _Recorder({"checkout/confirm": [_Resp(200, {"status": "success", "client_secret": _PI_SECRET})]})
    with pytest.raises(RuntimeError, match="payment intent confirm failed: 500"):
        _oaics(stripe, cs)


def test_oaics_success_builds_the_upi_deep_link_result():
    upi_uri = "upi://pay?pa=merchant@upi&am=199.00"
    stripe = _Recorder(
        {
            "confirmation_tokens": [_Resp(200, {"id": "ctoken_1"})],
            "payment_intents/": [
                _Resp(
                    200,
                    {
                        "status": "requires_action",
                        "hosted_instructions_url": "https://pay.example/instructions",
                        "next_action": {
                            "type": "upi_handle_redirect_or_display_qr_code",
                            "upi_handle_redirect_or_display_qr_code": {"mobile_auth_url": upi_uri},
                        },
                    },
                )
            ],
        }
    )
    cs = _Recorder({"checkout/confirm": [_Resp(200, {"status": "success", "client_secret": _PI_SECRET})]})
    result = _oaics(stripe, cs)

    assert result["ok"] is True
    assert result["link_type"] == "upi_deep_link"
    assert result["upi_uri"] == upi_uri
    assert result["url"] == upi_uri
    assert result["payment_intent_id"] == _PI_ID
    assert result["confirmation_token_id"] == "ctoken_1"
    assert result["payment_method_types"] == ["card", "link", "upi"]
    assert result["checkout_ui_mode"] == "deferred"
    assert result["paid"] is False
    assert result["payment_status"] == "requires_action"
    assert result["expires_at"] > 0
    assert cs.calls[0][2]["json"]["confirm_token"] == "ctoken_1"
    assert cs.calls[0][2]["json"]["selected_payment_method_type"] == "upi"


def test_oaics_instructions_url_without_a_deep_link_is_reported_as_instructions():
    instructions = "https://payments.stripe.com/upi/instructions/oaics_1"
    stripe = _Recorder(
        {
            "confirmation_tokens": [_Resp(200, {"id": "ctoken_1"})],
            "payment_intents/": [
                _Resp(200, {"status": "requires_action", "next_action": {"redirect_to_url": {"url": instructions}}})
            ],
        }
    )
    cs = _Recorder({"checkout/confirm": [_Resp(200, {"status": "success", "client_secret": _PI_SECRET})]})
    result = _oaics(stripe, cs)

    assert result["ok"] is True
    assert result["upi_uri"] == ""
    assert result["instructions_url"] == instructions
    assert result["link_type"] == "upi_instructions_url"


def test_oaics_polls_the_payment_intent_until_the_link_appears():
    upi_uri = "upi://pay?pa=merchant@upi"
    stripe = _Recorder(
        {
            "confirmation_tokens": [_Resp(200, {"id": "ctoken_1"})],
            "payment_intents/": [
                _Resp(200, {"status": "requires_action"}),  # confirm: no link yet
                _Resp(200, {"status": "requires_action", "next_action": {"mobile_auth_url": upi_uri}}),  # poll
            ],
        }
    )
    cs = _Recorder({"checkout/confirm": [_Resp(200, {"status": "success", "client_secret": _PI_SECRET})]})
    result = _oaics(stripe, cs)

    assert result["upi_uri"] == upi_uri
    methods = [call[0] for call in stripe.calls]
    assert methods == ["POST", "POST", "GET"], methods


def test_oaics_poll_errors_are_non_fatal():
    upi_uri = "upi://pay?pa=merchant@upi"
    stripe = _Recorder(
        {
            "confirmation_tokens": [_Resp(200, {"id": "ctoken_1"})],
            "payment_intents/": [_Resp(200, {"status": "requires_action"})],
        }
    )

    def _get(url: str, **kwargs: Any) -> _Resp:
        stripe.calls.append(("GET", url, kwargs))
        raise RuntimeError("network down")

    stripe.get = _get  # type: ignore[method-assign]
    cs = _Recorder({"checkout/confirm": [_Resp(200, {"status": "success", "client_secret": _PI_SECRET})]})
    result = _oaics(stripe, cs, qr_path="")

    assert result["ok"] is True, "a failing poll must still return the checkout result"
    assert result["upi_uri"] == ""
    assert result["link_type"] == "upi_hosted_fallback"


def test_oaics_wait_paid_polls_until_succeeded():
    upi_uri = "upi://pay?pa=merchant@upi"
    stripe = _Recorder(
        {
            "confirmation_tokens": [_Resp(200, {"id": "ctoken_1"})],
            "payment_intents/": [
                _Resp(200, {"status": "requires_action", "next_action": {"mobile_auth_url": upi_uri}}),
                _Resp(200, {"status": "succeeded"}),
            ],
        }
    )
    cs = _Recorder({"checkout/confirm": [_Resp(200, {"status": "success", "client_secret": _PI_SECRET})]})
    result = _oaics(stripe, cs, wait_paid=True, paid_timeout=30)

    assert result["paid"] is True
    assert result["payment_status"] == "succeeded"
