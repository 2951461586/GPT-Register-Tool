"""Dual-mode Python vs Rust parity tests for wave-10 `rust-anything` crates.

Covers `smsbower-phone-rs`, `account-stages-url-rs`, `agent-identity-jwt-rs`,
`codex-oauth-jwt-rs`, and the expanded `icloud-html-rs` surface
(`_yangyang_api_paths`), asserting identical outputs between the compiled PyO3
fast path and the pure-Python fallback.
"""

from __future__ import annotations

import base64
import json

import pytest

from sms_tool.codex import oauth as codex_oauth
from sms_tool.importers import agent_identity
from sms_tool.phone import smsbower
from sms_tool.providers import mailbox_icloud_url
from sms_tool.registration import account_stages


def _b64u(obj: object) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _make_jwt(payload: object) -> str:
    return f"{_b64u({'alg': 'RS256'})}.{_b64u(payload)}.sig"


def test_smsbower_normalize_phone_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if smsbower._NATIVE_SMSBOWER_PHONE is None:
        pytest.skip("smsbower-phone-rs native extension not built in this environment")

    samples = [
        "+233 55-123-4567",
        "0086-138-0013-8000",
        "  233 (55) 999-8888  ",
        "+001 415-555-0199",
        "+",
        "00",
        "abc-def",
        "",
    ]
    rust_out = [smsbower.normalize_phone(s) for s in samples]
    monkeypatch.setattr(smsbower, "_NATIVE_NORMALIZE_PHONE", None)
    py_out = [smsbower.normalize_phone(s) for s in samples]
    assert rust_out == py_out


def test_account_stages_strip_prompt_query_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if account_stages._NATIVE_ACCOUNT_STAGES_URL is None:
        pytest.skip("account-stages-url-rs native extension not built in this environment")

    samples = [
        "https://auth.openai.com/oauth/authorize?client_id=app_1&prompt=login&state=st_1",
        "https://auth.openai.com/oauth/authorize?prompt=select_account&prompt=login&scope=openid+email#done",
        "https://auth.openai.com/oauth/authorize;v=1?a=1&prompt=none&a=2&empty=#frag",
        "https://[::1]:8443/authorize?prompt=login&x=1",
        "/relative/path?prompt=login",
        "https:///no-host?prompt=login",
        "",
    ]
    rust_out = [account_stages._strip_prompt_query(u) for u in samples]
    monkeypatch.setattr(account_stages, "_NATIVE_STRIP_PROMPT_QUERY", None)
    py_out = [account_stages._strip_prompt_query(u) for u in samples]
    assert rust_out == py_out


def test_agent_identity_jwt_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if agent_identity._NATIVE_AGENT_IDENTITY_JWT is None:
        pytest.skip("agent-identity-jwt-rs native extension not built in this environment")

    samples = [
        _make_jwt({"https://api.openai.com/auth": {"chatgpt_user_id": "user-111"}, "sub": "auth0|1"}),
        _make_jwt({"https://api.openai.com/auth": {"user_id": "  user-222  "}, "sub": "auth0|2"}),
        _make_jwt({"sub": "auth0|333", "exp": 1900000000}),
        _make_jwt({"https://api.openai.com/auth": {"chatgpt_user_id": ""}, "sub": ""}),
        "not-a-jwt",
        "a.!!!bad!!!.c",
        "",
    ]
    rust_claims = [agent_identity._jwt_claims(t) for t in samples]
    rust_uid = [agent_identity._chatgpt_user_id(t) for t in samples]
    monkeypatch.setattr(agent_identity, "_NATIVE_JWT_CLAIMS", None)
    monkeypatch.setattr(agent_identity, "_NATIVE_CHATGPT_USER_ID", None)
    py_claims = [agent_identity._jwt_claims(t) for t in samples]
    py_uid = [agent_identity._chatgpt_user_id(t) for t in samples]
    assert rust_claims == py_claims
    assert rust_uid == py_uid


def test_codex_oauth_jwt_and_safe_url_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if codex_oauth._NATIVE_CODEX_OAUTH_JWT is None:
        pytest.skip("codex-oauth-jwt-rs native extension not built in this environment")

    seg_samples = [
        _b64u({"sub": "auth0|1", "exp": 1900000000}),
        _b64u({"email": "user@example.com", "email_verified": True}),
        "W10",
        "!!!bad!!!",
        "",
    ]
    url_samples = [
        "https://auth.openai.com/oauth/authorize?client_id=abc&state=secret#frag",
        "https://chatgpt.com/api/auth/callback/openai;p=1?code=xyz",
        "http://localhost:1455/auth/callback?code=123&state=456",
        "/relative/path?token=abc",
        "https://[invalid-ipv6/path?a=1",
        "  padded  ",
        "",
    ]
    rust_seg = [codex_oauth._jwt_segment(s) for s in seg_samples]
    rust_url = [codex_oauth._safe_url(u) for u in url_samples]
    monkeypatch.setattr(codex_oauth, "_NATIVE_JWT_SEGMENT", None)
    monkeypatch.setattr(codex_oauth, "_NATIVE_SAFE_URL", None)
    py_seg = [codex_oauth._jwt_segment(s) for s in seg_samples]
    py_url = [codex_oauth._safe_url(u) for u in url_samples]
    assert rust_seg == py_seg
    assert rust_url == py_url


def test_icloud_yangyang_api_paths_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if mailbox_icloud_url._NATIVE_ICLOUD_HTML is None:
        pytest.skip("icloud-html-rs native extension not built in this environment")

    samples = [
        "<script>var detailBase = '/api/d/'; var detailSuffix = '.json'; var pageBase = '/api/p/';</script>",
        '<script>var  detailBase="/d/";\nvar detailSuffix = "/body";\nvar\tpageBase=\'/p/\';</script>',
        "<script>var detailBase = '/only-one/';</script>",
        "<html><body>no vars here</body></html>",
        "",
    ]
    rust_out = [mailbox_icloud_url._yangyang_api_paths(s) for s in samples]
    monkeypatch.setattr(mailbox_icloud_url, "_NATIVE_YANGYANG_API_PATHS", None)
    py_out = [mailbox_icloud_url._yangyang_api_paths(s) for s in samples]
    assert rust_out == py_out
