"""Dual-mode Python vs Rust parity tests for wave-2 `rust-anything` crates.

Covers `gmail-html-rs`, `data-saver-rs`, `http-utils-rs`, `sms-code-rs`, and
`cpa-classify-rs`, asserting identical outputs between the compiled PyO3 fast
path and the pure-Python fallback.
"""

from __future__ import annotations

import pytest

from sms_tool.importers import cpa
from sms_tool.phone import sms_utils
from sms_tool.providers import mailbox_gmail
from sms_tool.registration_drivers import data_saver
from sms_tool.transport import http_utils


def test_gmail_html_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if mailbox_gmail._NATIVE_GMAIL_HTML is None:
        pytest.skip("gmail-html-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "Plain text 482910",
        "<html><head><STYLE>.a{color:red}</STYLE><script>var s='<b>1</b>';</script></head><body><p>Code: <b>482910</b> &amp; &nbsp; &#x20AC; &#0; &#128;</p></body></html>",
        "a <> b <<tag> c <unclosed",
        "Line 1\t\n\r  Line 2\u00a0Line 3",
        0,
        True,
    ]
    rust_out = [mailbox_gmail._html_to_text(sample) for sample in samples]
    monkeypatch.setattr(mailbox_gmail, "_NATIVE_HTML_TO_TEXT", None)
    py_out = [mailbox_gmail._html_to_text(sample) for sample in samples]
    assert rust_out == py_out


def test_data_saver_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if data_saver._NATIVE_DATA_SAVER is None:
        pytest.skip("data-saver-rs native extension not built in this environment")

    cases = [
        ("https://cdn.oaistatic.com/assets/app-9a8b7c.js?v=2", "script", "GET"),
        ("https://cdn.oaistatic.com/assets/main.css", "stylesheet", "GET"),
        ("https://chatgpt.com/_next/static/chunks/main.js", "script", "GET"),
        ("https://cdn.openai.com/images/logo.webp", "image", "GET"),
        ("https://cdn.oaistatic.com/assets/app.js", "script", "POST"),
        ("https://chatgpt.com/backend-api/sentinel/req.js", "script", "GET"),
        ("https://challenges.cloudflare.com/turnstile/v0/api.js", "script", "GET"),
        ("https://cdn.oaistatic.com/.js", "script", "GET"),
        ("https://cdn.oaistatic.com/a.js/", "script", "GET"),
        ("https://evil-oaistatic.com/assets/app.js", "script", "GET"),
        ("https://auth.openai.com/login/bundle.js", "script", "GET"),
        ("https://cdn.oaistatic.com./assets/bundle.mjs", "", ""),
        ("", "script", "GET"),
    ]
    rust_out = [data_saver.is_cacheable_static_asset(url, rtype, method=method) for url, rtype, method in cases]
    monkeypatch.setattr(data_saver, "_NATIVE_IS_CACHEABLE_STATIC_ASSET", None)
    py_out = [data_saver.is_cacheable_static_asset(url, rtype, method=method) for url, rtype, method in cases]
    assert rust_out == py_out


def test_http_utils_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if http_utils._NATIVE_HTTP_UTILS is None:
        pytest.skip("http-utils-rs native extension not built in this environment")

    auth_base = "https://auth.openai.com"
    chat_base = "https://chatgpt.com"
    url_cases = [
        "https://chatgpt.com/api/auth/callback/openai?code=abc",
        "https://auth.openai.com:443/authorize?state=1",
        "http://chatgpt.com:80/callback",
        "https://chatgpt.com:8443/callback",
        "https://user:pass@chatgpt.com/callback",
        "https://@chatgpt.com/ok",
        "https://evil.example.com/callback",
        "https://chatgpt.com/cb\r\nX-Injected: 1",
        "https://chatgpt.com\\@evil.com/",
        "javascript:alert(1)",
        "",
    ]
    cookie_cases = [
        None,
        "",
        "a=1; __Host-next-auth.csrf-token=tok1; __Secure-next-auth.session-token.0=p0; __Secure-next-auth.session-token.1=p1; empty=; badtoken",
        "__Secure-next-auth.callback-url=https%3A%2F%2Fchatgpt.com; __Host-next-auth.csrf-token=   ",
    ]

    rust_urls = [http_utils._is_allowed_callback_url(u, auth_base, chat_base) for u in url_cases]
    rust_cookies = [http_utils._minimal_chatgpt_cookie_header(c) for c in cookie_cases]

    monkeypatch.setattr(http_utils, "_NATIVE_IS_ALLOWED_CALLBACK_URL", None)
    monkeypatch.setattr(http_utils, "_NATIVE_MINIMAL_CHATGPT_COOKIE_HEADER", None)

    py_urls = [http_utils._is_allowed_callback_url(u, auth_base, chat_base) for u in url_cases]
    py_cookies = [http_utils._minimal_chatgpt_cookie_header(c) for c in cookie_cases]

    assert rust_urls == py_urls
    assert rust_cookies == py_cookies


def test_sms_code_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if sms_utils._NATIVE_SMS_CODE is None:
        pytest.skip("sms-code-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "Your OpenAI verification code is: 482910. Do not share it.",
        "Your OTP: 938472 for login",
        "Your code is 619204\n",
        "Security alert on 2026-11-24: ref 774128 ready",
        "Copyright 2026 OpenAI Inc.",
        "Order ID 1234567890 shipped.",
        "【OpenAI】您的验证码为 884120，10分钟内有效。",
        "Verify 5519 to continue",
    ]
    rust_out = [sms_utils._extract_sms_code(sample) for sample in samples]
    monkeypatch.setattr(sms_utils, "_NATIVE_EXTRACT_SMS_CODE", None)
    py_out = [sms_utils._extract_sms_code(sample) for sample in samples]
    assert rust_out == py_out


def test_cpa_classify_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if cpa._NATIVE_CPA_CLASSIFY is None:
        pytest.skip("cpa-classify-rs native extension not built in this environment")

    samples = [
        None,
        {},
        {"status": "active", "status_message": "ok", "probe": {"status_code": 200}},
        {
            "status": "error",
            "status_message": "authentication token has been invalidated",
            "probe": {"status_code": 401},
        },
        {"status": "error", "message": "context canceled", "probe": {"status_code": 504}},
        {"status": "unknown", "error": "Account suspended for abuse", "probe": {"status_code": 403}},
        {"status": "ok", "disabled": True},
        {"status": "pending", "message": "账号已封禁"},
        {"status": "custom_state", "message": "waiting", "probe": {}},
    ]
    rust_out = [cpa.classify_cpa_auth_file(sample) for sample in samples]
    monkeypatch.setattr(cpa, "_NATIVE_CLASSIFY_CPA_AUTH_FILE", None)
    py_out = [cpa.classify_cpa_auth_file(sample) for sample in samples]
    assert rust_out == py_out
