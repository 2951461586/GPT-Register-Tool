"""Dual-mode Python vs Rust parity tests for wave-15 `rust-anything` crates.

Covers `paypal-auth-queue-rs`, `codex-oauth-stage-rs`,
`sentinel-client-digest-rs`, `browser-flow-decisions-rs`, and
`sentinel-runner-parse-rs`, asserting identical outputs between the compiled
PyO3 fast paths and the pure-Python fallbacks.
"""

from __future__ import annotations

import pytest

from sms_tool.codex import oauth_helpers
from sms_tool.paypal import authorization_queue
from sms_tool.registration_drivers.browser_flow import decisions
from sms_tool.sentinel import client as sentinel_client
from sms_tool.sentinel import runner as sentinel_runner


def test_paypal_auth_queue_account_ref_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if authorization_queue._NATIVE_PAYPAL_AUTH_QUEUE is None:
        pytest.skip("paypal-auth-queue-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        "   ",
        "user@example.com",
        "  USER+tag@Example.COM ",
        "中文@example.com",
        "🚀@test.io",
        12345,
    ]
    rust_out = [authorization_queue._account_ref(s) for s in samples]  # type: ignore[arg-type]
    monkeypatch.setattr(authorization_queue, "_NATIVE_ACCOUNT_REF", None)
    py_out = [authorization_queue._account_ref(s) for s in samples]  # type: ignore[arg-type]
    assert rust_out == py_out


def test_codex_oauth_detect_protocol_stage_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if oauth_helpers._NATIVE_CODEX_OAUTH_STAGE is None:
        pytest.skip("codex-oauth-stage-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        "http://localhost:1455/auth/callback?code=abc&state=xyz",
        "http://localhost:1455/auth/callback?CODE=abc&STATE=xyz",
        "https://auth.openai.com/add-phone",
        "https://auth.openai.com/phone-verification?step=1",
        "https://auth.openai.com/email-verification",
        "https://AUTH.OPENAI.COM/EMAIL-OTP",
        "https://auth.openai.com/log-in/password",
        "https://auth.openai.com/login/password",
        "https://auth.openai.com/create-account/password",
        "https://auth.openai.com/consent",
        "https://auth.openai.com/workspace",
        "https://auth.openai.com/oauth/authorize?client_id=1",
        "https://chatgpt.com/",
        123,
    ]
    rust_out = [oauth_helpers._detect_protocol_stage(s) for s in samples]
    monkeypatch.setattr(oauth_helpers, "_NATIVE_DETECT_PROTOCOL_STAGE", None)
    py_out = [oauth_helpers._detect_protocol_stage(s) for s in samples]
    assert rust_out == py_out


def test_sentinel_client_secret_digest_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if sentinel_client._NATIVE_SENTINEL_CLIENT_DIGEST is None:
        pytest.skip("sentinel-client-digest-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        "http://user:pass@1.2.3.4:8080",
        "__Secure-next-auth.session-token=eyJhbGciOi...",
        "中文-secret-🚀",
        0,
        42,
    ]
    rust_out = [sentinel_client._secret_digest(s) for s in samples]  # type: ignore[arg-type]
    monkeypatch.setattr(sentinel_client, "_NATIVE_SECRET_DIGEST", None)
    py_out = [sentinel_client._secret_digest(s) for s in samples]  # type: ignore[arg-type]
    assert rust_out == py_out


def test_browser_flow_decisions_needs_chat_base_navigation_matches_python_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if decisions._NATIVE_BROWSER_FLOW_DECISIONS is None:
        pytest.skip("browser-flow-decisions-rs native extension not built in this environment")

    samples: list[tuple[object, object]] = [
        (None, None),
        ("", "https://chatgpt.com/"),
        ("https://chatgpt.com", "https://auth.openai.com/log-in"),
        ("https://CHATGPT.COM:443/", "https://chatgpt.com/c/123"),
        ("  \thttps://chatgpt.com\r\n ", "https://CHATGPT.COM/"),
        ("http://[::1]:8080", "http://[::1]:8080/chat"),
        ("chatgpt.com", "https://chatgpt.com/"),
        ("https://chatgpt.com", 12345),
    ]
    rust_out = [decisions.needs_chat_base_navigation(b, u) for b, u in samples]  # type: ignore[arg-type]
    for bad_url in ("http://[bad-ipv6:80", "http://[127.0.0.1]:80", "http://[not-ip]:80"):
        with pytest.raises(ValueError):
            decisions.needs_chat_base_navigation(bad_url, "https://chatgpt.com")

    monkeypatch.setattr(decisions, "_NATIVE_NEEDS_CHAT_BASE_NAVIGATION", None)
    py_out = [decisions.needs_chat_base_navigation(b, u) for b, u in samples]  # type: ignore[arg-type]
    assert rust_out == py_out
    for bad_url in ("http://[bad-ipv6:80", "http://[127.0.0.1]:80", "http://[not-ip]:80"):
        with pytest.raises(ValueError):
            decisions.needs_chat_base_navigation(bad_url, "https://chatgpt.com")


def test_sentinel_runner_impersonate_version_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if sentinel_runner._NATIVE_SENTINEL_RUNNER_PARSE is None:
        pytest.skip("sentinel-runner-parse-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        "chrome152",
        "safari18_4_ios",
        "Firefox135",
        "edge131",
        "123chrome",
        "chrome",
        "\u212a99",
        "chrome\uff11\uff12",
        999,
    ]
    rust_out = [sentinel_runner._impersonate_version(s) for s in samples]  # type: ignore[arg-type]
    monkeypatch.setattr(sentinel_runner, "_NATIVE_IMPERSONATE_VERSION", None)
    py_out = [sentinel_runner._impersonate_version(s) for s in samples]  # type: ignore[arg-type]
    assert rust_out == py_out
