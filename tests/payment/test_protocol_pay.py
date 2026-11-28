"""Unit & offline protocol tests for protocol_codec, protocol_pay, and CLI wiring.

All card numbers, Stripe keys, and session IDs are strictly synthetic fixtures.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sms_tool import cli
from sms_tool.pay_link.team_trial import currency_for_country
from sms_tool.payment import link_manager
from sms_tool.payment.protocol_codec import (
    BILLING_REGIONS,
    RVBuildFields,
    ZERO_DECIMAL_CURRENCIES,
    billing_address_for,
    billing_currency_for,
    billing_locale_for,
    billing_timezone_for,
    checkout_confirm_blocked,
    checkout_confirm_message,
    classify_stripe_outcome,
    decode_rv_timestamp,
    decline_message,
    encode_rv_timestamp,
    expected_amount_minor,
    extract_rv_build_fields,
    intent_id_from_secret,
    minor_units_from_amount,
    nested_intent_status,
    normalize_card,
    openai_internal_checkout_id,
    payment_intent_from_confirm,
    payment_page_settled,
    processor_from_checkout_url,
    public_checkout_id,
    quote_from_payload,
    rv_timestamp_from_script,
    scrub_card_secrets,
    stripe_billing_details,
    stripe_checkout_id,
    submission_failure_detail,
    submission_state,
)
from sms_tool.payment.protocol_pay import (
    CustomCheckoutProtocolClient,
    HostedCheckoutProtocolClient,
    ProtocolPaymentError,
    execute_protocol_card_payment,
    load_card_from_env_or_file,
)


SYNTHETIC_CARD = {
    "number": "4000 0000-0000 0002",
    "cvc": "999",
    "exp_month": "07",
    "exp_year": "31",
}
SYNTHETIC_PK = "pk_test_synthetic_0000000000000001"
SYNTHETIC_CS = "cs_test_a1B2c3D4e5F6g7H8"
SYNTHETIC_OAICS = "oaics_9876543210abcdef"


class _FakeResp:
    def __init__(self, status_code: int = 200, data: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._data = data
        self.text = text or (json.dumps(data) if isinstance(data, (dict, list)) else "")

    def json(self) -> Any:
        if self._data is None:
            raise ValueError("not json")
        return self._data


# ─── protocol_codec unit tests ───────────────────────────────────────────────


def test_rv_timestamp_roundtrip_and_script_extraction() -> None:
    encoded = encode_rv_timestamp("2026-11-26 12:00:00 -0000", "rev-abc-123", "sv-xyz-9")
    decoded = decode_rv_timestamp(encoded)
    assert decoded is not None
    assert decoded == RVBuildFields("2026-11-26 12:00:00 -0000", "rev-abc-123", "sv-xyz-9")
    assert decoded.as_dict() == {"rvTs": "2026-11-26 12:00:00 -0000", "rv": "rev-abc-123", "sv": "sv-xyz-9"}
    assert decode_rv_timestamp("not-valid-rv-timestamp") is None
    assert decode_rv_timestamp("") is None

    direct_js = 'const x = {foo:1, rvTs: "2026-11-26 00:00:00 -0000", rv: "r-1", sv: "s-1"};'
    assert extract_rv_build_fields(direct_js) == RVBuildFields("2026-11-26 00:00:00 -0000", "r-1", "s-1")

    aliased_js = (
        "const TS = \"2026-11-26 01:02:03 -0000\"; let RV = 'rev-2'; var SV = `sv-2`;"
        'const cfg = {"rvTs": /* cmt */ TS, "rv": // line\nRV, sv: SV};'
    )
    ts = rv_timestamp_from_script(aliased_js)
    assert ts is not None
    assert decode_rv_timestamp(ts) == RVBuildFields("2026-11-26 01:02:03 -0000", "rev-2", "sv-2")

    assert extract_rv_build_fields("const dyn = {rvTs: `${Date.now()}`, rv: 'r', sv: 's'};") is None
    assert rv_timestamp_from_script("no timing fields here") is None


def test_quotes_and_minor_unit_amounts() -> None:
    assert "JPY" in ZERO_DECIMAL_CURRENCIES
    assert minor_units_from_amount("20.00", "USD") == 2000
    assert minor_units_from_amount(3000, "JPY") == 3000
    with pytest.raises(ValueError):
        minor_units_from_amount("12.345", "USD")

    assert expected_amount_minor({"total_summary": {"due": 2500}}) == 2500
    assert expected_amount_minor({"invoice": {"amount_due": 1999}}) == 1999
    assert expected_amount_minor({"checkout_state": {"total": {"total": {"minorUnitsAmount": 3100}}}}) == 3100
    assert expected_amount_minor({"line_items": [{"amount": 1200}, {"amount_total": 800}]}) == 2000
    assert expected_amount_minor({"snapshot": {"amount_total": 0, "due": 1500}}) == 1500
    assert expected_amount_minor({}) is None

    assert quote_from_payload({"currency": "usd", "amount_total": 2000}) == (Decimal("20.00"), "USD")
    assert quote_from_payload({"currency": "jpy", "amount_total": 3000}) == (Decimal("3000"), "JPY")
    assert quote_from_payload(
        {"currency": "krw", "checkout_state": {"total": {"total": {"minorUnitsAmount": 29000}}}}
    ) == (Decimal("29000"), "KRW")
    assert quote_from_payload({"nested": {"total": {"amount": "19.95"}}}, "usd") == (Decimal("19.95"), "USD")


def test_card_validation_and_secret_scrubbing() -> None:
    safe = normalize_card(SYNTHETIC_CARD)
    assert safe == {"number": "4000000000000002", "cvc": "999", "exp_month": "7", "exp_year": "2031"}

    for bad_card in (
        None,
        {"number": "123", "cvc": "999", "exp_month": "12", "exp_year": "2030"},
        {"number": "4000000000000002", "cvc": "12", "exp_month": "12", "exp_year": "2030"},
        {"number": "4000000000000002", "cvc": "999", "exp_month": "13", "exp_year": "2030"},
        {"number": "4000000000000002", "cvc": "999", "exp_month": "12", "exp_year": "203"},
    ):
        with pytest.raises(ValueError) as exc_info:
            normalize_card(bad_card)
        assert "4000000000000002" not in str(exc_info.value)
        assert "999" not in str(exc_info.value)

    scrubbed = scrub_card_secrets("Upstream failed for 4000000000000002 cvc 999 year 2031", safe)
    assert "4000000000000002" not in scrubbed
    assert "999" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_billing_regions_and_team_trial_currency_alignment() -> None:
    assert len(BILLING_REGIONS) == 25
    assert billing_currency_for("JP") == "JPY"
    assert billing_currency_for("CL") == "CLP"
    assert billing_currency_for("ZZ") == "USD"
    assert billing_locale_for("DE") == "de-DE"
    assert billing_timezone_for("KR") == "Asia/Seoul"
    assert currency_for_country("CL") == "CLP"
    assert currency_for_country("AE") == "AED"

    addr = billing_address_for("JP")
    assert addr["country"] == "JP"
    assert addr["city"] and addr["postal_code"] and addr["line1"]
    details = stripe_billing_details(addr)
    assert details["address"]["country"] == "JP"


def test_id_parsers_and_stripe_outcome_classifier() -> None:
    hosted_url = f"https://pay.openai.com/c/pay/{SYNTHETIC_CS}#fidkdWxOYHwnPyd"
    custom_url = f"https://chatgpt.com/checkout/openai_ie/{SYNTHETIC_OAICS}?foo=bar"
    assert public_checkout_id(hosted_url) == SYNTHETIC_CS
    assert stripe_checkout_id(hosted_url) == SYNTHETIC_CS
    assert openai_internal_checkout_id(custom_url) == SYNTHETIC_OAICS
    assert processor_from_checkout_url(custom_url) == "openai_ie"
    assert intent_id_from_secret("pi_12345_secret_abcdef") == "pi_12345"
    assert intent_id_from_secret("seti_12345_secret_abcdef") == "seti_12345"
    assert checkout_confirm_blocked({"status": "blocked"}) is True
    assert checkout_confirm_message({"error": {"message": "limit reached"}}) == "limit reached"

    pi_id, secret, ret = payment_intent_from_confirm(
        {
            "payment_intent": {"id": "pi_99", "client_secret": "pi_99_secret_xyz", "status": "requires_confirmation"},
            "confirm_return_url": "https://chatgpt.com/checkout/verify",
        }
    )
    assert (pi_id, secret, ret) == ("pi_99", "pi_99_secret_xyz", "https://chatgpt.com/checkout/verify")
    assert submission_state({"submission_attempt": {"state": "FAILED"}}) == "failed"
    assert nested_intent_status({"payment_intent": {"status": "Requires_Action"}}) == "requires_action"
    assert payment_page_settled({"payment_intent": {"status": "succeeded"}}) is True
    assert "decline_code=do_not_honor" in submission_failure_detail(
        {"submission_attempt": {"error": {"decline_code": "do_not_honor", "code": "card_declined"}}}
    )
    assert "发卡行拒绝: do_not_honor" in decline_message("do_not_honor")

    paid = classify_stripe_outcome({"status": 200, "body": {"payment_intent": {"status": "succeeded"}}})
    assert paid["state"] == "paid"

    challenge = classify_stripe_outcome(
        {
            "status": 200,
            "body": {
                "payment_intent": {
                    "status": "requires_action",
                    "next_action": {
                        "type": "use_stripe_sdk",
                        "use_stripe_sdk": {"type": "intent_confirmation_challenge"},
                    },
                }
            },
        }
    )
    assert challenge["state"] == "challenge_required"
    assert challenge["challenge_type"] == "intent_confirmation_challenge"

    declined = classify_stripe_outcome(
        {
            "status": 402,
            "body": {
                "payment_intent": {
                    "status": "requires_payment_method",
                    "last_payment_error": {"code": "card_declined", "decline_code": "insufficient_funds"},
                }
            },
        }
    )
    assert declined["state"] == "card_declined"
    assert declined["decline_code"] == "insufficient_funds"


# ─── HostedCheckoutProtocolClient offline HTTP tests ─────────────────────────


def test_hosted_checkout_quote_and_live_rv_timestamp() -> None:
    session = MagicMock()
    session.headers = {}
    session.post.side_effect = [
        _FakeResp(200, {"eid": "eid_1", "currency": "usd", "config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_2", "total_summary": {"due": 2700}}),
    ]
    client = HostedCheckoutProtocolClient(session=session, sleep_fn=lambda _: None)
    quoted = client.quote(
        session_id=SYNTHETIC_CS,
        publishable_key=SYNTHETIC_PK,
        billing=billing_address_for("US"),
        currency="USD",
    )
    assert quoted["ok"] is True
    assert quoted["amount_minor"] == 2700
    assert quoted["checkout_config_id"] == "cfg_2"
    assert quoted["currency"] == "usd"

    session.get.side_effect = [
        _FakeResp(
            200, text='<script src="https://js.stripe.com/v3/fingerprinted/js/checkout-app-init-abc.js"></script>'
        ),
        _FakeResp(200, text='const t={rvTs:"2026-11-26 12:00:00 -0000",rv:"r-9",sv:"s-9"};'),
    ]
    timing = client.fetch_live_rv_timestamp(SYNTHETIC_CS)
    assert decode_rv_timestamp(timing) == RVBuildFields("2026-11-26 12:00:00 -0000", "r-9", "s-9")


def test_hosted_tokenize_retries_connect_error_and_drops_secrets() -> None:
    session = MagicMock()
    session.headers = {}
    captured_forms: list[dict[str, str]] = []

    def fake_post(url: str, *, data: dict[str, str], headers: dict[str, str], timeout: int) -> _FakeResp:
        captured_forms.append(dict(data))
        if len(captured_forms) == 1:
            raise OSError("transient tls reset")
        return _FakeResp(200, {"id": "pm_test_12345", "card": {"brand": "visa", "last4": "0002"}})

    session.post.side_effect = fake_post
    client = HostedCheckoutProtocolClient(session=session, sleep_fn=lambda _: None)
    pm_id = client.tokenize_card(
        card=SYNTHETIC_CARD,
        billing=billing_address_for("US"),
        publishable_key=SYNTHETIC_PK,
        session_id=SYNTHETIC_CS,
        checkout_config_id="cfg_1",
    )
    assert pm_id == "pm_test_12345"
    assert len(captured_forms) == 2
    assert "radar_options[hcaptcha_token]" not in captured_forms[0]


def test_hosted_confirm_never_retries_lost_response() -> None:
    session = MagicMock()
    session.headers = {}
    session.post.side_effect = OSError("connection reset after confirm sent")
    client = HostedCheckoutProtocolClient(session=session, sleep_fn=lambda _: None)
    ctx = {
        "session_id": SYNTHETIC_CS,
        "publishable_key": SYNTHETIC_PK,
        "eid": "eid_1",
        "amount_minor": 2500,
        "checkout_config_id": "cfg_1",
        "client_session_id": "csid_1",
        "guid": "g",
        "muid": "m",
        "sid": "s",
    }
    with pytest.raises(ProtocolPaymentError) as exc_info:
        client.confirm(ctx, "pm_test_1")
    assert exc_info.value.error_code == "payment_outcome_unknown"
    assert exc_info.value.retryable is False
    assert session.post.call_count == 1


def test_hosted_pay_stops_cleanly_on_challenge_required_and_requotes_mismatch() -> None:
    session = MagicMock()
    session.headers = {}
    session.get.side_effect = [
        _FakeResp(200, {"session_id": "elements_1"}),
        _FakeResp(200, text=""),
    ]
    session.post.side_effect = [
        # prepare -> quote (init + 3 tax updates) + post-elements refresh
        _FakeResp(200, {"eid": "eid_1", "currency": "usd", "config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_2", "total_summary": {"due": 2500}}),
        # confirm #1 -> checkout_amount_mismatch
        _FakeResp(400, {"error": {"code": "checkout_amount_mismatch", "message": "Amount changed"}}),
        # re-quote (init + 3 tax updates)
        _FakeResp(200, {"eid": "eid_1", "currency": "usd", "config_id": "cfg_3", "total_summary": {"due": 2600}}),
        _FakeResp(200, {"config_id": "cfg_3", "total_summary": {"due": 2600}}),
        _FakeResp(200, {"config_id": "cfg_3", "total_summary": {"due": 2600}}),
        _FakeResp(200, {"config_id": "cfg_3", "total_summary": {"due": 2600}}),
        # confirm #2 -> requires_action (MUST stop cleanly, zero CAPTCHA solver)
        _FakeResp(
            200,
            {
                "payment_intent": {
                    "status": "requires_action",
                    "next_action": {
                        "type": "use_stripe_sdk",
                        "use_stripe_sdk": {"type": "intent_confirmation_challenge"},
                    },
                }
            },
        ),
    ]
    client = HostedCheckoutProtocolClient(session=session, sleep_fn=lambda _: None)
    result = client.pay(
        f"https://pay.openai.com/c/pay/{SYNTHETIC_CS}",
        publishable_key=SYNTHETIC_PK,
        payment_method_id="pm_test_existing",
        country="US",
    )
    assert result["ok"] is False
    assert result["state"] == "challenge_required"
    assert result["manual_action_required"] is True
    assert result["challenge_type"] == "intent_confirmation_challenge"
    assert result["amount_minor"] == 2600


# ─── CustomCheckoutProtocolClient offline tests ──────────────────────────────


def test_custom_checkout_read_only_and_full_success_flow() -> None:
    open_page = {
        "checkout_session_id": SYNTHETIC_OAICS,
        "publishable_key": SYNTHETIC_PK,
        "status": "open",
        "payment_status": "unpaid",
        "currency": "usd",
        "checkout_state": {"currency": "usd", "total": {"total": {"minorUnitsAmount": 2000}}},
    }
    complete_page = {**open_page, "status": "complete", "payment_status": "paid"}

    # 1. Read-only mode must never call POST
    get_calls: list[str] = []
    post_calls: list[str] = []

    def fake_get_ro(url: str, *_args: Any) -> _FakeResp:
        get_calls.append(url)
        return _FakeResp(200, open_page)

    def fake_post_ro(url: str, *_args: Any) -> _FakeResp:
        post_calls.append(url)
        raise AssertionError("read_only must not mutate")

    ro_client = CustomCheckoutProtocolClient(
        access_token="at_test",
        checkout_get_fn=fake_get_ro,
        checkout_post_fn=fake_post_ro,
        stripe_session=MagicMock(),
    )
    spec = {
        "checkout_url": f"https://chatgpt.com/checkout/openai_llc/{SYNTHETIC_OAICS}",
        "checkout_id": SYNTHETIC_OAICS,
        "publishable_key": SYNTHETIC_PK,
        "currency": "USD",
        "amount_minor": 2000,
    }
    ro_res = ro_client.run({}, spec, read_only=True)
    assert ro_res["ok"] is True
    assert ro_res["status"] == "read_only"
    assert ro_res["state"] == "read_only"
    assert ro_res["mode"] == "custom_checkout"
    assert ro_res["session_id"] == SYNTHETIC_OAICS
    assert ro_res["amount_minor"] == 2000
    assert ro_res["currency"] == "USD"
    assert len(get_calls) == 1
    assert len(post_calls) == 0

    # 2. Full payment flow -> paid
    stripe_http = MagicMock()
    stripe_http.get.side_effect = [
        _FakeResp(200, {"customer": "cus_test_1"}),
        _FakeResp(
            200,
            {"id": "pi_test_1", "status": "requires_confirmation", "amount": 2000, "currency": "usd"},
        ),
    ]
    stripe_http.post.side_effect = [
        _FakeResp(200, {"id": "ctoken_test_1", "payment_method_preview": {"id": "pm_test_1"}}),
        _FakeResp(200, {"id": "pi_test_1", "status": "succeeded"}),
    ]
    get_seq = [
        _FakeResp(200, open_page),
        _FakeResp(200, open_page),
        _FakeResp(200, complete_page),
        _FakeResp(200, {"plan_type": "plus", "is_active": True}),
    ]
    post_seq = [
        _FakeResp(200, {"checkout_session": open_page}),
        _FakeResp(200, {"ok": True}),
        _FakeResp(
            200,
            {
                "payment_intent": {"id": "pi_test_1", "client_secret": "pi_test_1_secret_abc"},
                "confirm_return_url": "https://chatgpt.com/checkout/verify",
            },
        ),
        _FakeResp(200, {"success": True}),
    ]
    full_client = CustomCheckoutProtocolClient(
        access_token="at_test",
        checkout_get_fn=lambda *_a: get_seq.pop(0),
        checkout_post_fn=lambda *_a: post_seq.pop(0),
        stripe_session=stripe_http,
    )
    ctx: dict[str, Any] = {}
    res = full_client.run(ctx, spec, card=SYNTHETIC_CARD, billing=billing_address_for("US"))
    assert res["ok"] is True
    assert res["state"] == "paid"
    assert ctx["ack_complete"] is True


# ─── Card loader, auto-router, facade, and CLI tests ─────────────────────────


def test_load_card_from_env_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GPT_CARD_JSON", raising=False)
    with pytest.raises(ProtocolPaymentError) as exc_info:
        load_card_from_env_or_file("")
    assert exc_info.value.error_code == "missing_card_payload"

    monkeypatch.setenv("GPT_CARD_JSON", json.dumps(SYNTHETIC_CARD))
    assert load_card_from_env_or_file()["exp_year"] == "2031"

    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(SYNTHETIC_CARD), encoding="utf-8")
    monkeypatch.delenv("GPT_CARD_JSON", raising=False)
    assert load_card_from_env_or_file(str(card_path))["number"] == "4000000000000002"


def test_link_manager_lazy_exports_and_cli_flags() -> None:
    assert link_manager.execute_protocol_card_payment is execute_protocol_card_payment
    assert link_manager.load_card_from_env_or_file is load_card_from_env_or_file

    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--protocol-card-quote",
            "--checkout-url",
            f"https://pay.openai.com/c/pay/{SYNTHETIC_CS}",
            "--stripe-publishable-key",
            SYNTHETIC_PK,
            "--checkout-country",
            "JP",
        ]
    )
    assert args.protocol_card_quote is True
    assert args.checkout_url.endswith(SYNTHETIC_CS)
    assert args.stripe_publishable_key == SYNTHETIC_PK
    assert args.checkout_country == "JP"

    with patch.object(
        link_manager,
        "execute_protocol_card_payment",
        return_value={"ok": True, "session_id": SYNTHETIC_CS, "amount_minor": 3000, "currency": "jpy"},
    ) as mock_exec:
        cli._protocol_card_payment(args)
    mock_exec.assert_called_once()
    assert mock_exec.call_args.kwargs["quote_only"] is True
    assert mock_exec.call_args.kwargs["country"] == "JP"


def test_hosted_confirm_interrupted_and_gateway_errors_never_classify_as_card_declined() -> None:
    unknown = classify_stripe_outcome(
        {
            "status": 0,
            "body": {"error": {"code": "payment_outcome_unknown", "message": "transport dropped"}},
        }
    )
    assert unknown["state"] == "unknown"

    gateway = classify_stripe_outcome(
        {
            "status": 502,
            "body": {"error": {"code": "stripe_http_502", "message": "bad gateway"}},
        }
    )
    assert gateway["state"] == "unknown"

    rate_limited = classify_stripe_outcome(
        {
            "status": 429,
            "body": {"error": {"code": "stripe_http_429", "message": "rate limit"}},
        }
    )
    assert rate_limited["state"] == "confirm_error"

    session = MagicMock()
    session.headers = {}
    session.get.side_effect = [_FakeResp(200, {"session_id": "elements_1"}), _FakeResp(200, text="")]
    session.post.side_effect = [
        _FakeResp(200, {"eid": "eid_1", "currency": "usd", "config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_2", "total_summary": {"due": 2500}}),
        OSError("connection reset mid-confirm"),
    ]
    client = HostedCheckoutProtocolClient(session=session, sleep_fn=lambda _: None)
    res = client.pay(
        f"https://pay.openai.com/c/pay/{SYNTHETIC_CS}",
        publishable_key=SYNTHETIC_PK,
        payment_method_id="pm_test_1",
        country="US",
    )
    assert res["ok"] is False
    assert res["state"] == "unknown"
    assert res["status"] == "unknown"
    assert res["requires_reconciliation"] is True
    assert res["side_effect_started"] is True


def test_hosted_pay_amount_mismatch_refreshes_eid_and_classifies_second_confirm_failure() -> None:
    session = MagicMock()
    session.headers = {}
    confirm_bodies: list[str] = []

    responses = [
        _FakeResp(200, {"eid": "eid_1", "currency": "usd", "config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_1", "total_summary": {"due": 2500}}),
        _FakeResp(200, {"config_id": "cfg_2", "total_summary": {"due": 2500}}),
        _FakeResp(400, {"error": {"code": "checkout_amount_mismatch", "message": "Amount changed"}}),
        _FakeResp(200, {"eid": "eid_2", "currency": "usd", "config_id": "cfg_3", "total_summary": {"due": 2600}}),
        _FakeResp(200, {"config_id": "cfg_3", "total_summary": {"due": 2600}}),
        _FakeResp(200, {"config_id": "cfg_3", "total_summary": {"due": 2600}}),
        _FakeResp(200, {"config_id": "cfg_3", "total_summary": {"due": 2600}}),
        _FakeResp(
            402,
            {"error": {"code": "card_declined", "decline_code": "insufficient_funds", "message": "Insufficient funds"}},
        ),
    ]

    def fake_post(url: str, **kwargs: Any) -> _FakeResp:
        if url.endswith("/confirm"):
            confirm_bodies.append(str(kwargs.get("data") or ""))
        return responses.pop(0)

    session.get.side_effect = [_FakeResp(200, {"session_id": "elements_1"}), _FakeResp(200, text="")]
    session.post.side_effect = fake_post
    client = HostedCheckoutProtocolClient(session=session, sleep_fn=lambda _: None)
    res = client.pay(
        f"https://pay.openai.com/c/pay/{SYNTHETIC_CS}",
        publishable_key=SYNTHETIC_PK,
        payment_method_id="pm_test_1",
        country="US",
    )
    assert len(confirm_bodies) == 2
    assert "eid=eid_1" in confirm_bodies[0]
    assert "eid=eid_2" in confirm_bodies[1]
    assert res["ok"] is False
    assert res["state"] == "card_declined"
    assert res["decline_code"] == "insufficient_funds"
    assert "余额或额度不足" in res["message"]


def test_custom_checkout_forwards_country_polls_settlement_supports_team_plan_and_rejects_pm_id() -> None:
    # 1. oaics_ + pm_ fails fast with explicit error code
    with pytest.raises(ProtocolPaymentError) as exc_info:
        execute_protocol_card_payment(
            f"https://chatgpt.com/checkout/openai_llc/{SYNTHETIC_OAICS}",
            publishable_key=SYNTHETIC_PK,
            payment_method_id="pm_existing_1",
            access_token="at_test",
        )
    assert exc_info.value.error_code == "oaics_payment_method_id_unsupported"

    # 2. Country propagates from execute_protocol_card_payment -> pay_custom_checkout -> /taxes
    open_jp = {
        "checkout_session_id": SYNTHETIC_OAICS,
        "publishable_key": SYNTHETIC_PK,
        "status": "open",
        "payment_status": "unpaid",
        "currency": "jpy",
        "checkout_state": {"currency": "jpy", "total": {"total": {"minorUnitsAmount": 3000}}},
    }
    complete_jp = {**open_jp, "status": "complete", "payment_status": "paid"}
    recorded_taxes: list[dict[str, Any]] = []

    stripe_http = MagicMock()
    stripe_http.get.side_effect = [
        _FakeResp(200, {"customer": "cus_jp_1"}),
        _FakeResp(200, {"id": "pi_jp_1", "status": "requires_confirmation", "amount": 3000, "currency": "jpy"}),
        # First poll after Stripe confirm: intent succeeded while ChatGPT webhook hasn't settled yet
        _FakeResp(200, {"id": "pi_jp_1", "status": "succeeded", "amount": 3000, "currency": "jpy"}),
    ]
    stripe_http.post.side_effect = [
        _FakeResp(200, {"id": "ctoken_jp_1", "payment_method_preview": {"id": "pm_jp_1"}}),
        _FakeResp(200, {"id": "pi_jp_1", "status": "succeeded"}),
    ]
    get_seq = [
        _FakeResp(200, open_jp),  # initial read
        _FakeResp(200, open_jp),  # pre-confirm reconcile
        _FakeResp(200, open_jp),  # poll #1: webhook not settled yet
        _FakeResp(200, complete_jp),  # poll #2: webhook settled!
        _FakeResp(200, {"plan_type": "team", "is_active": True}),  # non-Plus paid plan recognized
    ]
    post_seq = [
        _FakeResp(200, {"checkout_session": open_jp}),
        _FakeResp(200, {"ok": True}),
        _FakeResp(
            200,
            {
                "payment_intent": {"id": "pi_jp_1", "client_secret": "pi_jp_1_secret_abc"},
                "confirm_return_url": "https://chatgpt.com/checkout/verify",
            },
        ),
        _FakeResp(200, {"success": True}),
    ]

    def fake_post(url: str, body: dict[str, Any], *_rest: Any) -> _FakeResp:
        if url.endswith("/taxes"):
            recorded_taxes.append(body)
        return post_seq.pop(0)

    sleeps: list[float] = []
    client = CustomCheckoutProtocolClient(
        access_token="at_test",
        checkout_get_fn=lambda *_a: get_seq.pop(0),
        checkout_post_fn=fake_post,
        stripe_session=stripe_http,
        sleep_fn=sleeps.append,
        max_poll_tries=3,
        poll_interval_seconds=0.25,
    )
    res = client.run(
        {},
        {
            "checkout_url": f"https://chatgpt.com/checkout/openai_llc/{SYNTHETIC_OAICS}",
            "checkout_id": SYNTHETIC_OAICS,
            "publishable_key": SYNTHETIC_PK,
            "currency": "JPY",
            "country": "JP",
            "amount_minor": 3000,
        },
        card=SYNTHETIC_CARD,
    )
    assert recorded_taxes[0]["billing_details"]["country"] == "JP"
    assert recorded_taxes[0]["billing_details"]["currency"] == "JPY"
    assert sleeps == [0.25]
    assert res["ok"] is True
    assert res["state"] == "paid"
    assert res["amount_minor"] == 3000
    assert res["currency"] == "JPY"

    # 3. Auto-router passes country="JP" through to pay_custom_checkout
    with patch(
        "sms_tool.payment.protocol_pay.pay_custom_checkout", return_value={"ok": True, "state": "read_only"}
    ) as m:
        out = execute_protocol_card_payment(
            f"https://chatgpt.com/checkout/openai_llc/{SYNTHETIC_OAICS}",
            publishable_key=SYNTHETIC_PK,
            access_token="at_test",
            country="JP",
            quote_only=True,
        )
    assert out["ok"] is True
    assert m.call_args.kwargs["country"] == "JP"
    assert m.call_args.kwargs["currency"] == "JPY"
    assert m.call_args.kwargs["read_only"] is True


def test_execute_protocol_card_payment_uses_operation_journal_replay_guard(tmp_path: Path) -> None:
    runtime_cfg = {"runtime_dir": str(tmp_path)}
    url = f"https://pay.openai.com/c/pay/{SYNTHETIC_CS}"

    # 1. Clean card_declined allows retrying with a different card on the same checkout session
    with patch(
        "sms_tool.payment.protocol_pay.pay_team_hosted_checkout",
        return_value={"ok": False, "status": "failed", "state": "card_declined", "decline_code": "insufficient_funds"},
    ):
        declined = execute_protocol_card_payment(
            url,
            publishable_key=SYNTHETIC_PK,
            payment_method_id="pm_card_1",
            runtime_config=runtime_cfg,
        )
    assert declined["state"] == "card_declined"
    assert declined["operation_id"]

    # 2. Succeeding on retry finalizes the operation and blocks duplicate replay
    with patch(
        "sms_tool.payment.protocol_pay.pay_team_hosted_checkout",
        return_value={"ok": True, "status": "completed", "state": "paid", "amount_minor": 2500, "currency": "USD"},
    ):
        paid = execute_protocol_card_payment(
            url,
            publishable_key=SYNTHETIC_PK,
            payment_method_id="pm_card_2",
            runtime_config=runtime_cfg,
        )
    assert paid["ok"] is True
    assert paid["state"] == "paid"

    with patch("sms_tool.payment.protocol_pay.pay_team_hosted_checkout") as replay_mock:
        blocked = execute_protocol_card_payment(
            url,
            publishable_key=SYNTHETIC_PK,
            payment_method_id="pm_card_3",
            runtime_config=runtime_cfg,
        )
    replay_mock.assert_not_called()
    assert blocked["ok"] is False
    assert blocked["error_code"] == "payment_operation_reconciliation_required"

    # 3. Unknown outcome also blocks blind replay until reconciled
    url2 = "https://pay.openai.com/c/pay/cs_test_unknown_guard_001"
    with patch(
        "sms_tool.payment.protocol_pay.pay_team_hosted_checkout",
        return_value={
            "ok": False,
            "status": "unknown",
            "state": "unknown",
            "requires_reconciliation": True,
            "code": "payment_outcome_unknown",
        },
    ):
        unknown = execute_protocol_card_payment(
            url2,
            publishable_key=SYNTHETIC_PK,
            payment_method_id="pm_card_4",
            runtime_config=runtime_cfg,
        )
    assert unknown["state"] == "unknown"

    with patch("sms_tool.payment.protocol_pay.pay_team_hosted_checkout") as replay_mock2:
        blocked2 = execute_protocol_card_payment(
            url2,
            publishable_key=SYNTHETIC_PK,
            payment_method_id="pm_card_4",
            runtime_config=runtime_cfg,
        )
    replay_mock2.assert_not_called()
    assert blocked2["ok"] is False
    assert blocked2["state"] == "unknown"
    assert blocked2["requires_reconciliation"] is True
