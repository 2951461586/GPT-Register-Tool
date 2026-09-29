"""Tests for the direct SetupIntent mandate confirm (UPI AutoPay).

Ported 2026-09-30 from ``tilian/provider_checkout.retry_approved_local_mandate``.
Payment Page confirm drops ``payment_method_options`` as an unknown parameter on
this Checkout revision, so an approved submission can still leave its SetupIntent
at ``requires_payment_method``. UPI AutoPay only signs the mandate when it is
submitted straight to ``/v1/setup_intents/{id}/confirm``, and *that* response is
what carries the ``upi://`` deep link -- without the stage every run degraded to
the hosted instructions page (measured 3/3 accounts, 2026-09-30).
"""

from __future__ import annotations

import json

import pytest

from sms_tool.upi_link import stripe as S
from sms_tool.upi_link.constants import (
    UPI_LOCAL_MANDATE_DEFAULT_AMOUNT,
    UPI_LOCAL_MANDATE_ENABLED,
    UPI_LOCAL_MANDATE_STRIPE_VERSION,
)

SETI = {
    "id": "seti_123",
    "object": "setup_intent",
    "client_secret": "seti_123_secret_abc",
    "status": "requires_payment_method",
}

UPI_URI = "upi://mandate?pa=stripe@axisbank&am=1999.00"
INSTRUCTIONS = "https://payments.stripe.com/upi/instructions/abc123"


def _resp(status: int, payload: dict) -> object:
    class _R:
        status_code = status
        text = json.dumps(payload)

        def json(self) -> dict:
            return payload

    return _R()


def _intent_payload() -> dict:
    return {
        "id": "seti_123",
        "object": "setup_intent",
        "status": "requires_action",
        "next_action": {
            "type": "upi_handle_redirect_or_display_qr_code",
            "upi_handle_redirect_or_display_qr_code": {
                "hosted_instructions_url": INSTRUCTIONS,
                "mobile_auth_url": UPI_URI,
            },
        },
    }


class _FakeStripe:
    """POST recorder that pops one canned response per call."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": dict(data or {}), "headers": dict(headers or {}), "timeout": timeout})
        return self._responses.pop(0)


# --------------------------------------------------------------------------
# variant ladder / amount / flattening
# --------------------------------------------------------------------------


def test_ladder_is_widest_first_and_ends_with_pm_only():
    variants = S._upi_local_mandate_variants("upi", 199900)
    assert [name for name, _ in variants] == ["upi_max_1y", "upi_fixed_1y", "upi_max_no_end", "upi_pm_only"]

    first = variants[0][1]["mandate_options"]
    assert first["amount"] == 199900
    assert first["amount_type"] == "maximum"
    assert "end_date" in first
    assert variants[1][1]["mandate_options"]["amount_type"] == "fixed"
    assert "end_date" not in variants[2][1]["mandate_options"]
    assert variants[3][1] == {}


def test_non_upi_provider_has_no_mandate_ladder():
    assert S._upi_local_mandate_variants("pix", 9990) == [("pm_only", {})]


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, UPI_LOCAL_MANDATE_DEFAULT_AMOUNT),
        (1, UPI_LOCAL_MANDATE_DEFAULT_AMOUNT),
        (2, 2),
        ("2500", 2500),
        (None, UPI_LOCAL_MANDATE_DEFAULT_AMOUNT),
        ("x", UPI_LOCAL_MANDATE_DEFAULT_AMOUNT),
        ([], UPI_LOCAL_MANDATE_DEFAULT_AMOUNT),
    ],
)
def test_mandate_amount_uses_checkout_amount_then_provider_default(value, expected):
    assert S._upi_local_mandate_amount(value) == expected


def test_flatten_nested_params_matches_stripe_form_encoding():
    flat = S._upi_flatten_stripe_params(
        {"mandate_options": {"amount": 199900, "ok": True, "off": False}},
        "payment_method_options[upi]",
    )
    assert flat == {
        "payment_method_options[upi][mandate_options][amount]": "199900",
        "payment_method_options[upi][mandate_options][ok]": "true",
        "payment_method_options[upi][mandate_options][off]": "false",
    }


# --------------------------------------------------------------------------
# guard + success path
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "intent,pm_id",
    [
        ({"id": "pi_1", "client_secret": "s"}, "pm_1"),
        ({"id": "seti_1"}, "pm_1"),
        ({"id": "seti_1", "client_secret": "s"}, ""),
        ({"id": "seti_1", "client_secret": "s"}, "card_1"),
        ("not-a-mapping", "pm_1"),
    ],
)
def test_skips_without_conditions_and_makes_no_call(intent, pm_id):
    fake = _FakeStripe([])
    result = S._upi_confirm_local_mandate(fake, setup_intent=intent, pm_id=pm_id, return_url="", stripe_pk="pk")
    assert result["skipped"] is True
    assert result["ok"] is False
    assert fake.calls == []
    assert "conditions unmet" in result["error"]


def test_confirms_setup_intent_and_returns_upi_deep_link(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    fake = _FakeStripe([_resp(200, _intent_payload())])
    result = S._upi_confirm_local_mandate(
        fake,
        setup_intent=SETI,
        pm_id="pm_9",
        return_url="https://chatgpt.com/checkout/openai_ie/cs_live_x",
        stripe_pk="pk_test",
        amount=0,
    )

    assert result["ok"] is True
    assert result["upi_uri"] == UPI_URI
    assert result["redirect_url"] == INSTRUCTIONS
    assert result["error"] == ""

    call = fake.calls[0]
    assert call["url"] == "https://api.stripe.com/v1/setup_intents/seti_123/confirm"
    assert call["headers"]["Stripe-Version"] == UPI_LOCAL_MANDATE_STRIPE_VERSION
    assert call["data"]["client_secret"] == SETI["client_secret"]
    assert call["data"]["payment_method"] == "pm_9"
    assert call["data"]["use_stripe_sdk"] == "true"
    assert call["data"]["key"] == "pk_test"
    assert call["data"]["return_url"].endswith("/cs_live_x")
    assert call["data"]["mandate_data[customer_acceptance][type]"] == "online"
    assert call["data"]["mandate_data[customer_acceptance][online][infer_from_client]"] == "true"
    assert call["data"]["payment_method_options[upi][mandate_options][amount]"] == str(UPI_LOCAL_MANDATE_DEFAULT_AMOUNT)
    assert call["data"]["payment_method_options[upi][mandate_options][amount_type]"] == "maximum"


def test_falls_back_to_setup_intent_payment_method_when_pm_id_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    fake = _FakeStripe([_resp(200, _intent_payload())])
    intent = {**SETI, "payment_method": {"id": "pm_from_intent"}}
    result = S._upi_confirm_local_mandate(fake, setup_intent=intent, pm_id="", return_url="", stripe_pk="pk")
    assert result["ok"] is True
    assert fake.calls[0]["data"]["payment_method"] == "pm_from_intent"


# --------------------------------------------------------------------------
# degradation
# --------------------------------------------------------------------------


def test_falls_through_variant_and_version_ladders(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    # three Stripe-Version retries of upi_max_1y all 4xx, then upi_fixed_1y wins
    responses = [_resp(400, {"error": {"message": "nope"}})] * 3 + [_resp(200, _intent_payload())]
    fake = _FakeStripe(responses)
    result = S._upi_confirm_local_mandate(fake, setup_intent=SETI, pm_id="pm_9", return_url="u", stripe_pk="pk")

    assert result["ok"] is True
    assert [entry["variant"] for entry in result["variants"]] == [
        "upi_max_1y",
        "upi_max_1y",
        "upi_max_1y",
        "upi_fixed_1y",
    ]
    assert len({entry["api_version"] for entry in result["variants"][:3]}) == 3
    assert result["variants"][-1]["intent_status"] == "requires_action"
    # the winning variant still carries the mandate options
    assert "payment_method_options[upi][mandate_options][amount_type]" in fake.calls[-1]["data"]


def test_pm_only_last_resort_drops_payment_method_options(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    responses = [_resp(402, {})] * 9 + [_resp(200, _intent_payload())]
    fake = _FakeStripe(responses)
    result = S._upi_confirm_local_mandate(fake, setup_intent=SETI, pm_id="pm_9", return_url="u", stripe_pk="pk")

    assert result["ok"] is True
    assert result["variants"][-1]["variant"] == "upi_pm_only"
    assert "payment_method_options" not in " ".join(fake.calls[-1]["data"])


def test_transport_exception_degrades_instead_of_raising(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))

    class _Boom:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= 3:
                raise OSError("connection reset")
            return _resp(200, _intent_payload())

    boom = _Boom()
    result = S._upi_confirm_local_mandate(boom, setup_intent=SETI, pm_id="pm_9", return_url="u", stripe_pk="pk")
    assert result["ok"] is True
    assert result["variants"][0]["status"] == -1


def test_reports_error_when_every_variant_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    fake = _FakeStripe([_resp(400, {"error": {"message": "rejected"}})] * 12)
    result = S._upi_confirm_local_mandate(fake, setup_intent=SETI, pm_id="pm_9", return_url="u", stripe_pk="pk")

    assert result["ok"] is False
    assert result["skipped"] is False
    assert "rejected" in result["error"]
    assert len(result["variants"]) == 12  # 4 variants x 3 Stripe-Version candidates


def test_200_without_deep_link_keeps_payload_for_hosted_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    payload = {"id": "seti_123", "status": "succeeded"}
    fake = _FakeStripe([_resp(200, payload)] * 12)
    result = S._upi_confirm_local_mandate(fake, setup_intent=SETI, pm_id="pm_9", return_url="u", stripe_pk="pk")

    assert result["ok"] is False
    assert result["payload"] == payload
    assert "no upi://" in result["error"]


# --------------------------------------------------------------------------
# post-approval SetupIntent rescue (reference ``need_setup_recover``)
# --------------------------------------------------------------------------


def _payment_page(
    *, setup_status: str = "requires_payment_method", submission_state: str = "failed", last_error: str = "", redirect: str = ""
) -> dict:
    setup_intent: dict = {"id": "seti_123", "object": "setup_intent", "status": setup_status}
    if last_error:
        setup_intent["last_setup_error"] = {"code": last_error}
    payload: dict = {
        "object": "checkout.session",
        "id": "ppage_1",
        "setup_intent": setup_intent,
        "submission_attempt": {"state": submission_state},
    }
    if redirect:
        payload["next_action"] = {"redirect_to_url": {"url": redirect}}
    return payload


@pytest.mark.parametrize(
    "payload,expected",
    [
        pytest.param(_payment_page(setup_status="requires_payment_method"), True, id="requires_payment_method"),
        pytest.param(_payment_page(setup_status="requires_confirmation"), True, id="requires_confirmation"),
        pytest.param({"object": "checkout.session"}, True, id="no-setup-intent-yet"),
        pytest.param(_payment_page(setup_status="succeeded"), False, id="succeeded"),
        pytest.param(_payment_page(setup_status="succeeded", last_error="generic_decline"), True, id="decline"),
        pytest.param(
            _payment_page(redirect=INSTRUCTIONS),
            False,
            id="redirect-already-present",
        ),
        pytest.param("not-a-mapping", False, id="non-mapping"),
    ],
)
def test_needs_setup_recover_matches_the_reference_condition(payload, expected):
    assert S._upi_needs_setup_recover(payload) is expected


class _FakePageStripe:
    """GET recorder returning a scripted payment-page sequence."""

    def __init__(self, payloads: list) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return _resp(200, self._payloads.pop(0))


def test_poll_rescues_setup_intent_then_returns_redirect(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    monkeypatch.setenv("UPI_FAILED_STATE_GRACE_POLL", "1")
    pages = [
        _payment_page(setup_status="requires_payment_method", last_error="generic_decline"),
        _payment_page(setup_status="succeeded", submission_state="succeeded", redirect=INSTRUCTIONS),
    ]
    fake = _FakePageStripe(pages)
    seen: list = []

    redirect, qr = S._upi_poll_payment_page(
        fake,
        "cs_live_x",
        "pk",
        {},
        current_pm_id="pm_9",
        rescue=lambda payload: seen.append(payload) or True,
    )

    assert redirect == INSTRUCTIONS
    assert qr == []
    assert fake.calls == 2
    assert len(seen) == 1
    assert seen[0]["setup_intent"]["status"] == "requires_payment_method"


def test_poll_rescues_before_treating_the_decline_as_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    monkeypatch.setenv("UPI_FAILED_STATE_GRACE_POLL", "1")
    fake = _FakePageStripe([_payment_page(last_error="generic_decline")] * 4)
    with pytest.raises(RuntimeError) as err:
        S._upi_poll_payment_page(
            fake, "cs_live_x", "pk", {}, current_pm_id="pm_9", rescue=lambda payload: False
        )
    assert "generic_decline" in str(err.value)


def test_poll_does_not_rescue_when_a_redirect_is_already_present(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    fake = _FakePageStripe([_payment_page(setup_status="requires_payment_method", redirect=INSTRUCTIONS)])
    seen: list = []

    redirect, _qr = S._upi_poll_payment_page(
        fake, "cs_live_x", "pk", {}, current_pm_id="pm_9", rescue=lambda payload: seen.append(payload) or True
    )

    assert redirect == INSTRUCTIONS
    assert seen == []


def test_poll_without_a_rescue_callback_keeps_legacy_behaviour(monkeypatch, tmp_path):
    """No rescue callback (e.g. mandate stage disabled) -> the decline stays terminal."""
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    monkeypatch.setenv("UPI_FAILED_STATE_GRACE_POLL", "1")
    fake = _FakePageStripe([_payment_page(last_error="generic_decline")] * 3)
    with pytest.raises(RuntimeError) as err:
        S._upi_poll_payment_page(fake, "cs_live_x", "pk", {}, current_pm_id="pm_9")
    assert "generic_decline" in str(err.value)


def test_checkout_created_setup_intent_stops_the_ladder_immediately(monkeypatch, tmp_path):
    """Structural refusal, measured live 2026-09-30: every variant and every
    ``Stripe-Version`` candidate answers the same sentence, so the ladder must
    stop after the first reply instead of spending 11 more round trips."""
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    body = {"error": {"message": "You cannot confirm SetupIntents created by Checkout."}}
    fake = _FakeStripe([_resp(400, body)] * 12)
    result = S._upi_confirm_local_mandate(fake, setup_intent=SETI, pm_id="pm_9", return_url="u", stripe_pk="pk")

    assert result["ok"] is False
    assert result["fatal"] is True
    assert len(fake.calls) == 1
    assert len(result["variants"]) == 1
    assert "created by Checkout" in result["error"]


def test_non_fatal_errors_still_walk_the_whole_ladder(monkeypatch, tmp_path):
    monkeypatch.setenv("UPI_DUMP_DIR", str(tmp_path))
    body = {"error": {"message": "parameter_unknown"}}
    fake = _FakeStripe([_resp(400, body)] * 12)
    result = S._upi_confirm_local_mandate(fake, setup_intent=SETI, pm_id="pm_9", return_url="u", stripe_pk="pk")

    assert result["fatal"] is False
    assert len(result["variants"]) == 12


# --------------------------------------------------------------------------
# wiring defaults
# --------------------------------------------------------------------------


def test_mandate_stage_is_on_by_default():
    """The mandate stage is the only producer of the ``upi://`` deep link."""
    assert UPI_LOCAL_MANDATE_ENABLED is True


def test_pipeline_imports_the_mandate_stage():
    from sms_tool.upi_link import pipeline

    assert pipeline._upi_confirm_local_mandate is S._upi_confirm_local_mandate
