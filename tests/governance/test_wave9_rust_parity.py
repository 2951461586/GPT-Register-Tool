"""Dual-mode Python vs Rust parity tests for wave-9 `rust-anything` crates.

Covers `tls-fingerprint-rs`, `session-builder-rs`, `account-lifecycle-rs`,
`nexsms-phone-rs`, `qualification-parse-rs`, and the expanded
`account-identity-rs` surface (`chatgpt_account_id_from_token`), asserting
identical outputs between the compiled PyO3 fast path and the pure-Python
fallback.
"""

from __future__ import annotations

import base64
import json

import pytest

from sms_tool.accounts import account_identity, account_lifecycle
from sms_tool.payment import qualification
from sms_tool.phone import nexsms
from sms_tool.session import builder as session_builder
from sms_tool.transport import tls_fingerprint


def _b64u(obj: object) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _make_jwt(payload: object) -> str:
    hdr = _b64u({"alg": "RS256"})
    body = _b64u(payload)
    return f"{hdr}.{body}.sig"


def test_tls_fingerprint_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if tls_fingerprint._NATIVE_TLS_FINGERPRINT is None:
        pytest.skip("tls-fingerprint-rs native extension not built in this environment")

    samples = [
        ("0-23-65281-10-11-35-16-5-34-18-51-43-13-45-28-27-65037", "did-001|salt"),
        ("0-23-65281-10-11-35-16-5-34-18-51-43-13-45-28-27-65037-21", "did-002|"),
        ("23-65281-10-11-35-16-21-21", "dup-pad"),
        ("0-23", "short"),
        ("0-23-10", ""),
        ("0-23-10-11-35", "unicode-种子-🚀"),
    ]
    rust_out = [tls_fingerprint.permute_extension_order(base, seed) for base, seed in samples]
    monkeypatch.setattr(tls_fingerprint, "_NATIVE_PERMUTE_EXTENSION_ORDER", None)
    py_out = [tls_fingerprint.permute_extension_order(base, seed) for base, seed in samples]
    assert rust_out == py_out


def test_session_builder_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if session_builder._NATIVE_SESSION_BUILDER is None:
        pytest.skip("session-builder-rs native extension not built in this environment")

    samples = [
        "oai-did=abc; __Secure-next-auth.session-token=direct_tok; cf=1",
        "__Secure-next-auth.session-token.2=CC; __Secure-next-auth.session-token.0=AA; __Secure-next-auth.session-token.1=BB",
        "__Secure-next-auth.session-token.10=TEN; __Secure-next-auth.session-token.2=TWO; __Secure-next-auth.session-token.0=ZERO",
        "next-auth.session-token.0=a; __Secure-next-auth.session-token=win; next-auth.session-token.1=b",
        "__Secure-next-auth.session-token.0=first; next-auth.session-token.0=override; next-auth.session-token.1=x=y",
        "__Secure-next-auth.session-token=; foo=bar; noequals",
        "",
    ]
    rust_out = [session_builder._session_token_from_cookie_header(h) for h in samples]
    monkeypatch.setattr(session_builder, "_NATIVE_SESSION_TOKEN_FROM_COOKIE_HEADER", None)
    py_out = [session_builder._session_token_from_cookie_header(h) for h in samples]
    assert rust_out == py_out


def test_account_lifecycle_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if account_lifecycle._NATIVE_ACCOUNT_LIFECYCLE is None:
        pytest.skip("account-lifecycle-rs native extension not built in this environment")

    samples = [
        "SampleAccount66@+oai01hotmail.com",
        "sampleaccount66+oai01@hotmail.com",
        "User.Name_123+tag@Sub.Example-Domain.org",
        "ΣΊΣΥΦΟΣ",
        "कि ٣٤",
        "---@@@+++",
        "",
    ]
    rust_out = [account_lifecycle._email_core(e) for e in samples]
    monkeypatch.setattr(account_lifecycle, "_NATIVE_EMAIL_CORE", None)
    py_out = [account_lifecycle._email_core(e) for e in samples]
    assert rust_out == py_out


def test_nexsms_phone_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if nexsms._NATIVE_NEXSMS_PHONE is None:
        pytest.skip("nexsms-phone-rs native extension not built in this environment")

    samples = [
        "+233 55-123-4567",
        "0086-138-0013-8000",
        "  +001 (415) 555-0199  ",
        "+00",
        "abc-def",
        "",
    ]
    rust_out = [nexsms.api_phone_number(p) for p in samples]
    monkeypatch.setattr(nexsms, "_NATIVE_API_PHONE_NUMBER", None)
    py_out = [nexsms.api_phone_number(p) for p in samples]
    assert rust_out == py_out


def test_qualification_parse_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if qualification._NATIVE_QUALIFICATION_PARSE is None:
        pytest.skip("qualification-parse-rs native extension not built in this environment")

    samples = [
        "gcash,momo,paypal",
        "GCash; Grab-Pay\nDIRECT-CARD  paypal, gcash",
        "auto",
        "AUTO, upi; gopay; qris; auto",
        "  kakao-pay ; naver-pay ; bizum  ",
        "",
    ]
    rust_out = [qualification.parse_method_selection(raw) for raw in samples]
    monkeypatch.setattr(qualification, "_NATIVE_PARSE_METHOD_SELECTION", None)
    py_out = [qualification.parse_method_selection(raw) for raw in samples]
    assert rust_out == py_out


def test_account_identity_wave9_jwt_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if account_identity._NATIVE_ACCOUNT_IDENTITY is None:
        pytest.skip("account-identity-rs native extension not built in this environment")

    samples = [
        _make_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-111"}, "sub": "u1"}),
        _make_jwt({"https://api.openai.com/auth": {"chatgptAccountId": "  acct-222  "}}),
        _make_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "   "}, "chatgpt_account_id": "acct-333"}),
        _make_jwt({"chatgptAccountId": "acct-444"}),
        "not-a-jwt",
        "a.!!!bad!!!.c",
        "",
    ]
    rust_out = [account_identity.chatgpt_account_id_from_token(tok) for tok in samples]
    monkeypatch.setattr(account_identity, "_NATIVE_CHATGPT_ACCOUNT_ID_FROM_TOKEN", None)
    py_out = [account_identity.chatgpt_account_id_from_token(tok) for tok in samples]
    assert rust_out == py_out
