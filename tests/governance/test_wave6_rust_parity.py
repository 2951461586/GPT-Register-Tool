"""Dual-mode Python vs Rust parity tests for wave-6 `rust-anything` crates.

Covers `captcha-config-rs`, `auth-flow-steps-rs`, `auth-wire-rs`, and the
expanded `icloud-html-rs` surface (`_latest_mail_received_at`,
`_decode_js_string`, `_with_message_limit`), asserting identical outputs
between the compiled PyO3 fast path and the pure-Python fallback.
"""

from __future__ import annotations

import json

import pytest

from sms_tool.auth_flow import steps as auth_steps
from sms_tool.browser import captcha_solver
from sms_tool.providers import mailbox_icloud_url
from sms_tool.transport import auth_wire


def test_captcha_config_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if captcha_solver._NATIVE_CAPTCHA_CONFIG is None:
        pytest.skip("captcha-config-rs native extension not built in this environment")

    html_samples = [
        "",
        "<html><body>No captcha here</body></html>",
        '<div class="h-captcha" data-sitekey="c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a"></div>',
        "<div DATA-SITEKEY='abc-123' DATA-RQDATA='rq-xyz'></div>",
        '<script>{"hcaptcha_site_key": "hc-1", "hcaptcha_rqdata": "rq-1"}</script>',
        '<script>{"site_key": "hc-2", "rqdata": "rq-2"}</script>',
        '<script>{"recaptchaSiteKey": "6Ltest1"}</script>',
        '<script>{"recaptcha_site_key": "6Ltest2", "rqdata": "rq-3"}</script>',
    ]
    proxy_samples = [
        None,
        "",
        "   ",
        "http://127.0.0.1:7897",
        "socks5h://u1:p2@proxy.example:1080",
        "socks5h://onlyuser@127.0.0.1:10808",
        "http://[::1]:8080",
        "http://[invalid",
        "http://host:99999",
    ]

    rust_cfg = [captcha_solver.extract_captcha_config(h) for h in html_samples]
    rust_proxy = [captcha_solver._playwright_proxy(p) for p in proxy_samples]

    monkeypatch.setattr(captcha_solver, "_NATIVE_EXTRACT_CAPTCHA_CONFIG", None)
    monkeypatch.setattr(captcha_solver, "_NATIVE_PLAYWRIGHT_PROXY", None)

    py_cfg = [captcha_solver.extract_captcha_config(h) for h in html_samples]
    py_proxy = [captcha_solver._playwright_proxy(p) for p in proxy_samples]

    assert rust_cfg == py_cfg
    assert rust_proxy == py_proxy


def test_auth_flow_steps_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if auth_steps._NATIVE_AUTH_FLOW_STEPS is None:
        pytest.skip("auth-flow-steps-rs native extension not built in this environment")

    url_samples = [
        None,
        "",
        "https://chatgpt.com/auth/login",
        "https://chatgpt.com/auth/log-in/?next=/",
        "https://sub.chatgpt.com/auth/login/",
        "https://auth.openai.com/create-account/password?state=1",
        "https://auth.openai.com/create-account/password;jsessionid=1",
        "https://auth.openai.com/create-account/",
        "https://auth.openai.com/email-verification",
        "https://auth.openai.com/api/accounts/email-otp/send",
        "https://auth.openai.com/log-in/password",
        "https://example.com/email-verification",
    ]
    so_samples = [
        None,
        "",
        "not-json",
        "[]",
        json.dumps({"p": "payload", "flow": "authorize_continue"}),
        json.dumps({"p": "payload", "flow": "  oauth_create_account  "}),
        json.dumps({"p": "payload"}),
    ]

    rust_login = [auth_steps._is_chatgpt_auth_login_landing(u) for u in url_samples]
    rust_pw_page = [auth_steps._is_signup_password_page(u) for u in url_samples]
    rust_pw_step = [auth_steps._is_signup_password_step(u) for u in url_samples]
    rust_ev_step = [auth_steps._is_email_verification_step(u) for u in url_samples]
    rust_flow = [auth_steps.so_token_flow(t) for t in so_samples]
    rust_same = [
        auth_steps.same_flow_so_token(t, exp) for t in so_samples for exp in ("authorize_continue", "other", "")
    ]

    monkeypatch.setattr(auth_steps, "_NATIVE_IS_CHATGPT_AUTH_LOGIN_LANDING", None)
    monkeypatch.setattr(auth_steps, "_NATIVE_IS_SIGNUP_PASSWORD_PAGE", None)
    monkeypatch.setattr(auth_steps, "_NATIVE_IS_SIGNUP_PASSWORD_STEP", None)
    monkeypatch.setattr(auth_steps, "_NATIVE_IS_EMAIL_VERIFICATION_STEP", None)
    monkeypatch.setattr(auth_steps, "_NATIVE_SO_TOKEN_FLOW", None)
    monkeypatch.setattr(auth_steps, "_NATIVE_SAME_FLOW_SO_TOKEN", None)

    py_login = [auth_steps._is_chatgpt_auth_login_landing(u) for u in url_samples]
    py_pw_page = [auth_steps._is_signup_password_page(u) for u in url_samples]
    py_pw_step = [auth_steps._is_signup_password_step(u) for u in url_samples]
    py_ev_step = [auth_steps._is_email_verification_step(u) for u in url_samples]
    py_flow = [auth_steps.so_token_flow(t) for t in so_samples]
    py_same = [auth_steps.same_flow_so_token(t, exp) for t in so_samples for exp in ("authorize_continue", "other", "")]

    assert rust_login == py_login
    assert rust_pw_page == py_pw_page
    assert rust_pw_step == py_pw_step
    assert rust_ev_step == py_ev_step
    assert rust_flow == py_flow
    assert rust_same == py_same


def test_auth_wire_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if auth_wire._NATIVE_AUTH_WIRE is None:
        pytest.skip("auth-wire-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "https://auth.openai.com/log-in",
        "https://auth.openai.com/log-in/password?state=1",
        "https://auth.openai.com/login/",
        "https://sub.auth.openai.com/login/callback",
        "https://auth.openai.com/log-in;p=1",
        "https://auth.openai.com/log-in-extra",
        "https://chatgpt.com/log-in",
        "/log-in",
        "/login/step",
        "/create-account",
    ]
    rust_out = [auth_wire._is_existing_login_redirect(u) for u in samples]
    monkeypatch.setattr(auth_wire, "_NATIVE_IS_EXISTING_LOGIN_REDIRECT", None)
    py_out = [auth_wire._is_existing_login_redirect(u) for u in samples]
    assert rust_out == py_out


def test_icloud_html_wave6_additions_match_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if mailbox_icloud_url._NATIVE_ICLOUD_HTML is None:
        pytest.skip("icloud-html-rs native extension not built in this environment")

    time_samples = [
        "",
        "no date",
        "2026年09月16日 23:48:03 (北京时间)",
        "2024年02月29日 00:00:00",
        "2025年02月29日 12:00:00",
        "2026年13月01日 12:00:00",
    ]
    js_samples = [
        "",
        "plain text",
        r"<p>Hello \"World\"</p>\r\n\u4f60\u597d",
        r"broken \x escape \"quoted\" \r\n https:\/\/openai.com\/a",
    ]
    limit_samples = [
        ("https://icloud-api.top/inbox/abc", 25),
        ("https://icloud-api.top/inbox/abc?token=xyz&n=5", 25),
        ("https://icloud-api.top/inbox/abc?a=1&b=hello+world#sec", 100),
        ("https://icloud-api.top/inbox/abc?k=v", 0),
        ("  padded  ", 0),
    ]

    rust_time = [mailbox_icloud_url._latest_mail_received_at(v) for v in time_samples]
    rust_js = [mailbox_icloud_url._decode_js_string(v) for v in js_samples]
    rust_lim = [mailbox_icloud_url._with_message_limit(u, lim) for u, lim in limit_samples]

    monkeypatch.setattr(mailbox_icloud_url, "_NATIVE_LATEST_MAIL_RECEIVED_AT", None)
    monkeypatch.setattr(mailbox_icloud_url, "_NATIVE_DECODE_JS_STRING", None)
    monkeypatch.setattr(mailbox_icloud_url, "_NATIVE_WITH_MESSAGE_LIMIT", None)

    py_time = [mailbox_icloud_url._latest_mail_received_at(v) for v in time_samples]
    py_js = [mailbox_icloud_url._decode_js_string(v) for v in js_samples]
    py_lim = [mailbox_icloud_url._with_message_limit(u, lim) for u, lim in limit_samples]

    assert rust_time == py_time
    assert rust_js == py_js
    assert rust_lim == py_lim
