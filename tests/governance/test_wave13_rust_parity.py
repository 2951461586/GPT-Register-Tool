"""Dual-mode Python vs Rust parity tests for wave-13 `rust-anything` crates.

Covers `browser-flow-session-rs`, `browser-flow-dom-rs`, `batch-report-parse-rs`,
and `http-client-parse-rs`, asserting identical outputs between the compiled PyO3
fast paths and the pure-Python fallbacks.
"""

from __future__ import annotations

import pytest

from sms_tool.payment import batch_report
from sms_tool.registration_drivers.browser_flow import dom_fields, session
from sms_tool.transport import http_client


def test_browser_flow_session_failure_class_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if session._NATIVE_BROWSER_FLOW_SESSION is None:
        pytest.skip("browser-flow-session-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "   ",
        "REGISTRATION_CANCELLED",
        "browser_session_rate_limited",
        "browser_existing_account",
        "browser_session_account_not_linked",
        "email_otp_timeout",
        "mailbox_auth_invalid",
        "browser_email_value_mismatch",
        "browser_profile_create_failed",
        "browser_proxy_blocked",
        "browser_proxy_country_mismatch",
        "browser_api_key_missing",
        "unsupported_driver",
        "unknown_transport_drop",
        "中文_PROXY_BLOCKED",
    ]
    rust_out = [session._browser_failure_class(s) for s in samples]  # type: ignore[arg-type]
    monkeypatch.setattr(session, "_NATIVE_BROWSER_FAILURE_CLASS", None)
    py_out = [session._browser_failure_class(s) for s in samples]  # type: ignore[arg-type]
    assert rust_out == py_out


def test_browser_flow_dom_helpers_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if dom_fields._NATIVE_BROWSER_FLOW_DOM is None:
        pytest.skip("browser-flow-dom-rs native extension not built in this environment")

    url_samples = [
        None,
        "",
        "about:blank",
        "chatgpt.com",
        "//chatgpt.com/auth/login",
        "https://CHATGPT.COM:443/backend-api/me",
        "https://auth.openai.com/log-in?next=%2F",
        "https://accounts.google.com/o/oauth2",
        "http://login.microsoftonline.com/common",
        "https://notchatgpt.com/",
        "https://chatgpt.com.evil.org/",
        "ftp://accounts.google.com/",
        "http://[::1]:8080/login",
        "  \thttps://openai.com\r\n",
    ]
    closed_samples = [
        None,
        "",
        "Target page, context or browser has been closed",
        "TargetClosedError: Target closed",
        "Browser has been closed",
        "NoSuchWindowException: closed",
        "TimeoutError: waiting for selector",
        "Connection reset by peer",
    ]
    terminal_samples = [
        (200, ""),
        (200, "OAuth_Account-Not Linked"),
        (200, "access_denied"),
        (200, "session-required"),
        (200, "RefreshAccessTokenError"),
        (200, "oauth_callback_failed"),
        (200, "email-create-account"),
        (429, ""),
        (401, "none"),
        (403, "denied"),
        (404, ""),
        (405, ""),
        (500, ""),
        (0, None),
    ]

    rust_auth = [dom_fields._is_openai_auth_url(u) for u in url_samples]  # type: ignore[arg-type]
    rust_idp = [dom_fields._unexpected_identity_provider(u) for u in url_samples]  # type: ignore[arg-type]
    rust_closed = [dom_fields._session_context_closed(m) for m in closed_samples]  # type: ignore[arg-type]
    rust_term = [dom_fields._terminal_session_error(s, m) for s, m in terminal_samples]  # type: ignore[arg-type]

    for bad_url in ("http://[bad-ipv6:80", "http://[not-ipv6]:80", "http://[127.0.0.1]:80"):
        with pytest.raises(ValueError):
            dom_fields._is_openai_auth_url(bad_url)

    monkeypatch.setattr(dom_fields, "_NATIVE_IS_OPENAI_AUTH_URL", None)
    monkeypatch.setattr(dom_fields, "_NATIVE_UNEXPECTED_IDP", None)
    monkeypatch.setattr(dom_fields, "_NATIVE_SESSION_CONTEXT_CLOSED", None)
    monkeypatch.setattr(dom_fields, "_NATIVE_TERMINAL_SESSION_ERROR", None)

    assert rust_auth == [dom_fields._is_openai_auth_url(u) for u in url_samples]  # type: ignore[arg-type]
    assert rust_idp == [dom_fields._unexpected_identity_provider(u) for u in url_samples]  # type: ignore[arg-type]
    assert rust_closed == [dom_fields._session_context_closed(m) for m in closed_samples]  # type: ignore[arg-type]
    assert rust_term == [dom_fields._terminal_session_error(s, m) for s, m in terminal_samples]  # type: ignore[arg-type]

    for bad_url in ("http://[bad-ipv6:80", "http://[not-ipv6]:80", "http://[127.0.0.1]:80"):
        with pytest.raises(ValueError):
            dom_fields._is_openai_auth_url(bad_url)


def test_batch_report_parse_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if batch_report._NATIVE_BATCH_REPORT_PARSE is None:
        pytest.skip("batch-report-parse-rs native extension not built in this environment")

    id_samples = [
        None,
        "",
        "   ",
        "batch-2026.11.28_01",
        "  hello world / foo:bar@baz  ",
        "a" * 120,
        "中文批次#123!!!test",
        "paypal_2026-11-28T00:30:00Z+08:00",
    ]
    email_samples = [
        None,
        "",
        "   ",
        "USER@Example.COM",
        "  sample-1@test.org \t ",
        "中文@example.com",
    ]

    rust_ids = [batch_report.safe_batch_id(s) for s in id_samples]  # type: ignore[arg-type]
    rust_refs = [batch_report.account_ref(e) for e in email_samples]  # type: ignore[arg-type]

    monkeypatch.setattr(batch_report, "_NATIVE_SAFE_BATCH_ID", None)
    monkeypatch.setattr(batch_report, "_NATIVE_ACCOUNT_REF", None)

    assert rust_ids == [batch_report.safe_batch_id(s) for s in id_samples]  # type: ignore[arg-type]
    assert rust_refs == [batch_report.account_ref(e) for e in email_samples]  # type: ignore[arg-type]


def test_http_client_transient_error_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if http_client._NATIVE_HTTP_CLIENT_PARSE is None:
        pytest.skip("http-client-parse-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        "account_deactivated",
        "HTTP 400 Bad Request",
        "curl: (35) TLS connect error: OPENSSL_internal",
        "curl: (28) Operation timed out",
        "curl: (56) Proxy CONNECT aborted",
        "Connection reset by peer",
        "Page.goto: NS_ERROR_ABORT",
        "Navigation aborted by frame detach",
        RuntimeError("curl: (7) Failed to connect"),
        RuntimeError("invalid_grant"),
    ]

    rust_out = [http_client.is_transient_transport_error(s) for s in samples]
    monkeypatch.setattr(http_client, "_NATIVE_IS_TRANSIENT_TRANSPORT_ERROR", None)
    py_out = [http_client.is_transient_transport_error(s) for s in samples]
    assert rust_out == py_out
