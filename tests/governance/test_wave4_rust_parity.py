"""Dual-mode Python vs Rust parity tests for wave-4 `rust-anything` crates.

Covers `payment-wire-rs`, `pay-link-normalize-rs`, and `qualification-states-rs`,
asserting identical outputs between the compiled PyO3 fast path and the pure-Python
fallback.
"""

from __future__ import annotations

import pytest

from sms_tool.pay_link import normalize as pay_link_normalize
from sms_tool.payment import qualification_states, wire as payment_wire


def test_payment_wire_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if payment_wire._NATIVE_PAYMENT_WIRE is None:
        pytest.skip("payment-wire-rs native extension not built in this environment")

    html_samples = [
        "",
        None,
        "<html><body>plain</body></html>",
        '<script>{"webDeploymentAttestation":"eyJ0.payload.sig"}</script>',
        r"{\"webDeploymentAttestation\":\"abc.def.ghi\"}",
        "foo?webDeploymentAttestation=tok_1.part_2&bar=1",
        "webDeploymentAttestation=no_leading_punct.token",
        '{"webDeploymentAttestation": "nodot"}',
        " webDeploymentAttestation='quoted.token.v2' ",
    ]
    diag_samples = [
        ("", 180),
        (None, 180),
        ("  line1\r\n\t  line2\n\n   line3   ", 180),
        ("word " * 100, 40),
        ("中文错误信息   换行\n制表符\t测试" * 10, 32),
        ("abcdef", 5),
        ("abcdef", 2),
        (12345, 10),
    ]

    rust_att = [payment_wire.extract_web_deployment_attestation(h) for h in html_samples]  # type: ignore[arg-type]
    rust_diag = [payment_wire._compact_diagnostic(v, lim) for v, lim in diag_samples]

    monkeypatch.setattr(payment_wire, "_NATIVE_EXTRACT_WEB_DEPLOYMENT_ATTESTATION", None)
    monkeypatch.setattr(payment_wire, "_NATIVE_COMPACT_DIAGNOSTIC", None)

    py_att = [payment_wire.extract_web_deployment_attestation(h) for h in html_samples]  # type: ignore[arg-type]
    py_diag = [payment_wire._compact_diagnostic(v, lim) for v, lim in diag_samples]

    assert rust_att == py_att
    assert rust_diag == py_diag


def test_pay_link_normalize_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if pay_link_normalize._NATIVE_PAY_LINK_NORMALIZE is None:
        pytest.skip("pay-link-normalize-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "   ",
        123,
        "cancelled",
        "CANCELED",
        "Cancelled-By-User",
        "  keyboard_interrupt  ",
        "payment_link_cancelled",
        "user_canceled",
        "timed_out",
        "TIMED-OUT",
        "http_read_timeout",
        "stage_timed_out",
        "unknown",
        "confirm_outcome_unknown",
        "INCONCLUSIVE",
        "HTTP 503 Service Unavailable!!!",
        "___weird---chars@@@123___",
        "completed",
    ]

    rust_norm = [pay_link_normalize._normalized_contract_value(v) for v in samples]
    rust_term = [pay_link_normalize._canonical_terminal_state(v) for v in samples]

    monkeypatch.setattr(pay_link_normalize, "_NATIVE_NORMALIZED_CONTRACT_VALUE", None)
    monkeypatch.setattr(pay_link_normalize, "_NATIVE_CANONICAL_TERMINAL_STATE", None)

    py_norm = [pay_link_normalize._normalized_contract_value(v) for v in samples]
    py_term = [pay_link_normalize._canonical_terminal_state(v) for v in samples]

    assert rust_norm == py_norm
    assert rust_term == py_term


def test_qualification_states_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if qualification_states._NATIVE_QUALIFICATION_STATES is None:
        pytest.skip("qualification-states-rs native extension not built in this environment")

    results = [
        None,
        {},
        {"eligible": True, "conclusive": True, "payment_method": "paypal", "checkout_kind": "stripe"},
        {"eligible": True, "conclusive": True, "payment_method": "momo", "checkout_kind": "oaics"},
        {"eligible": True, "conclusive": True, "payment_method": "gcash"},
        {"eligible": False, "conclusive": True, "decision": "nonzero_offer"},
        {"eligible": False, "conclusive": True, "decision": "checkout_currency_mismatch"},
        {"eligible": False, "conclusive": True, "decision": "method_country_mismatch"},
        {"eligible": False, "conclusive": True, "error": "400 Billing country must match request country"},
        {"eligible": False, "conclusive": True, "decision": "card_only"},
        {"decision": "method_country_mismatch"},
        {"error": "checkout HTTP 401 unauthorized", "error_code": "checkout_unauthorized"},
        {"error": "Tunnel connection failed: 503 Target host denied"},
        {"error": "checkout HTTP 400 Our systems have detected unusual activity", "status": "failed"},
        {"error": "curl: (28) Operation timed out"},
        {"status": "failed", "error": "mystery"},
        {"eligible": True, "conclusive": False},
        {"ok": True},
    ]

    rust_states = [qualification_states.qualification_state(r) for r in results]
    rust_labels = [
        qualification_states.qualification_label(r, m) for r in results for m in ("", "paypal", "direct-card")
    ]

    monkeypatch.setattr(qualification_states, "_NATIVE_QUALIFICATION_STATE", None)
    monkeypatch.setattr(qualification_states, "_NATIVE_QUALIFICATION_LABEL", None)

    py_states = [qualification_states.qualification_state(r) for r in results]
    py_labels = [qualification_states.qualification_label(r, m) for r in results for m in ("", "paypal", "direct-card")]

    assert rust_states == py_states
    assert rust_labels == py_labels
