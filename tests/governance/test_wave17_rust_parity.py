"""Dual-mode Python vs Rust parity tests for wave-17 `rust-anything` crates.

Covers `payment-capability-parse-rs`, `operation-journal-rs`,
`paypal-config-picker-rs`, and `token-telemetry-rs`, asserting identical
outputs between the compiled PyO3 fast paths and the pure-Python fallbacks.
"""

from __future__ import annotations

import pytest

from sms_tool import operation_journal
from sms_tool.observability import token_telemetry
from sms_tool.payment import capability as payment_capability
from sms_tool.paypal import config_picker


def test_payment_capability_redact_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if payment_capability._NATIVE_PAYMENT_CAPABILITY_PARSE is None:
        pytest.skip("payment-capability-parse-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        "plain error message",
        "failed via http://alice:secret@gate.example.com:8080 with Bearer eyJabc.def-123/xyz",
        "SOCKS5H://u:p@10.0.0.1:1080 and HTTPS://bob:pw@proxy.test:443 BEARER   tok_123~+/=",
        "中文报错 http://user:pass@proxy.cn:9000 Bearer secret.token",
        "a" * 620,
    ]
    rust_out = [payment_capability._redact_capability_error(s) for s in samples]
    monkeypatch.setattr(payment_capability, "_NATIVE_REDACT_CAPABILITY_ERROR", None)
    py_out = [payment_capability._redact_capability_error(s) for s in samples]
    assert rust_out == py_out


def test_operation_journal_hash_id_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if operation_journal._NATIVE_OPERATION_JOURNAL is None:
        pytest.skip("operation-journal-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        "   ",
        "0123456789abcdef0123456789abcdef",
        "  ABCDEF0123456789ABCDEF0123456789  ",
        "0123456789abcdef0123456789abcdeg",
        "protocol_card_pay:cs_live_test_12345",
        "alice@example.com",
        "中文订单-2026",
    ]
    rust_out = [operation_journal._hash_public_operation_id(s) for s in samples]
    monkeypatch.setattr(operation_journal, "_NATIVE_HASH_PUBLIC_OPERATION_ID", None)
    py_out = [operation_journal._hash_public_operation_id(s) for s in samples]
    assert rust_out == py_out


def test_paypal_config_picker_stem_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if config_picker._NATIVE_PAYPAL_CONFIG_PICKER is None:
        pytest.skip("paypal-config-picker-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        "alice+tag@example.com",
        "bad/path:name space@example.com",
        "中文用户+1@example.com",
        "+++",
        "user_01.test-2@sub.domain.org",
    ]
    rust_out = [config_picker._safe_paypal_session_stem(s) for s in samples]
    monkeypatch.setattr(config_picker, "_NATIVE_SAFE_PAYPAL_SESSION_STEM", None)
    py_out = [config_picker._safe_paypal_session_stem(s) for s in samples]
    assert rust_out == py_out


def test_token_telemetry_claims_and_hash_match_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if token_telemetry._NATIVE_TOKEN_TELEMETRY is None:
        pytest.skip("token-telemetry-rs native extension not built in this environment")

    samples: list[str | None] = [
        None,
        "",
        "   ",
        "not-a-jwt-token",
        "eyJhbGciOiJIUzI1NiJ9.eyJpYXQiOjE3MDAwMDAwMDAsImV4cCI6MTcwMDAwMzYwMH0.sig",
        "  eyJhbGciOiJIUzI1NiJ9.eyJpYXQiOiIxNzAwMDAwMDAwIiwiZXhwIjoxNzAwMDAzNjAwLjV9.sig  ",
        "eyJhbGciOiJIUzI1NiJ9.eyJpYXQiOnRydWUsImV4cCI6ZmFsc2V9.sig",
        "eyJhbGciOiJIUzI1NiJ9.W10.sig",
        "hdr.invalid!!!base64.sig",
        "hdr.eyJpYXQiOiAiIDEyMzQgIiwgImV4cCI6ICItOTkifQ.sig",
    ]
    rust_out = [token_telemetry._parse_token_claims_and_hash(s) for s in samples]
    monkeypatch.setattr(token_telemetry, "_NATIVE_PARSE_TOKEN_CLAIMS_AND_HASH", None)
    py_out = [token_telemetry._parse_token_claims_and_hash(s) for s in samples]
    assert rust_out == py_out
