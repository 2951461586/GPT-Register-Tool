"""Dual-mode Python vs Rust parity tests for `rust/protocol-payment-rs`.

Asserts 100% behavioral parity between the compiled PyO3 fast path and the
pure-Python fallback for every shipped function in
``sms_tool/payment/protocol_codec.py``.
"""

from __future__ import annotations

import pytest

from sms_tool.payment import protocol_codec


def test_protocol_payment_rs_rv_timestamp_and_quote_match_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if protocol_codec._NATIVE_PROTOCOL_PAYMENT is None:
        pytest.skip("protocol-payment-rs native extension not built in this environment")

    triples = [
        ("2026-11-26 12:34:56 -0000", "2026-11-26-ab12cd34", "checkout-v3-20261126"),
        ("2025-05-01 00:00:00 +0000", "rev-short", "sv-1"),
        ("2024-01-01 00:00:00 -0000", "rev-unicode-\u00e9-\u4e2d\u6587", 'sv"quote\\slash'),
        ("", "", ""),
    ]
    scripts = [
        'const x = {rvTs: "2026-11-26 12:00:00 -0000", rv: "rev-001", sv: "sv-001"};',
        'const A="2026-11-26 18:00:00 -0000";let B=\'rev-alias-01\';var C=`sv-tpl-01`;const cfg={"rvTs":/*ts*/A,"rv"://rv\nB,sv:C};',
        'const dyn={rvTs:`${Date.now()}`,rv:"x",sv:"y"};',
        "no timing constants in this script",
        "",
    ]
    quote_inputs = [
        ({"currency": "usd", "amount_total": 2000}, ""),
        ({"currency": "jpy", "amount_total": 3000}, ""),
        ({"currency": "krw", "checkout_state": {"total": {"total": {"minorUnitsAmount": 29000}}}}, ""),
        ({"deep": {"nested": {"taxInclusive": 2200, "currency": "eur"}}}, "usd"),
        ({"nested": {"total": {"amount": "19.95"}}}, "usd"),
        ({"display_amount": "USD 20.00", "currency": "usd"}, ""),
        ({}, "gbp"),
        (None, "usd"),
    ]

    rust_encoded = [protocol_codec.encode_rv_timestamp(a, b, c) for a, b, c in triples]
    decode_inputs = [*rust_encoded, "", "not-valid-base64!!!", "\x01\x02", None]
    rust_decoded = [protocol_codec._decode_rv_timestamp_dict(v) for v in decode_inputs]
    rust_extracted = [protocol_codec._extract_rv_build_fields_dict(s) for s in scripts]
    rust_from_script = [protocol_codec.rv_timestamp_from_script(s) for s in scripts]
    rust_quotes = [protocol_codec._quote_from_payload_str(p, cur) for p, cur in quote_inputs]

    for attr in (
        "_NATIVE_ENCODE_RV_TIMESTAMP",
        "_NATIVE_DECODE_RV_TIMESTAMP_DICT",
        "_NATIVE_EXTRACT_RV_BUILD_FIELDS_DICT",
        "_NATIVE_RV_TIMESTAMP_FROM_SCRIPT",
        "_NATIVE_QUOTE_FROM_PAYLOAD_STR",
    ):
        monkeypatch.setattr(protocol_codec, attr, None)

    assert rust_encoded == [protocol_codec.encode_rv_timestamp(a, b, c) for a, b, c in triples]
    assert rust_decoded == [protocol_codec._decode_rv_timestamp_dict(v) for v in decode_inputs]
    assert rust_extracted == [protocol_codec._extract_rv_build_fields_dict(s) for s in scripts]
    assert rust_from_script == [protocol_codec.rv_timestamp_from_script(s) for s in scripts]
    assert rust_quotes == [protocol_codec._quote_from_payload_str(p, cur) for p, cur in quote_inputs]
