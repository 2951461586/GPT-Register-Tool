"""Dual-mode Python vs Rust parity tests for wave-11 `rust-anything` crates.

Covers `extract-server-parse-rs`, `paypal-reverse-html-rs`, and `wham-usage-rs`,
asserting identical outputs between the compiled PyO3 fast path and the
pure-Python fallback.
"""

from __future__ import annotations

import json

import pytest

from sms_tool.accounts import wham_usage
from sms_tool.pay_link import extract_server
from sms_tool.paypal import reverse_protocol


def test_extract_server_validate_api_base_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if extract_server._NATIVE_EXTRACT_SERVER_PARSE is None:
        pytest.skip("extract-server-parse-rs native extension not built in this environment")

    samples = [
        "https://pay.example.com",
        "http://127.0.0.1:8080/api/extract",
        "https://[::1]:8443/v1",
        "HTTPS://PAY.EXAMPLE.COM/api",
        "ftp://pay.example.com",
        "https:///no-host",
        "https://[bad-ipv6/api",
        "https://[127.0.0.1]/api",
        "  https://pay.example.com\r\n",
        "not-a-url",
        "",
    ]
    rust_out = [extract_server._validate_api_base(s) for s in samples]
    monkeypatch.setattr(extract_server, "_NATIVE_VALIDATE_API_BASE", None)
    py_out = [extract_server._validate_api_base(s) for s in samples]
    assert rust_out == py_out


def test_paypal_reverse_html_parsers_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if reverse_protocol._NATIVE_PAYPAL_REVERSE_HTML is None:
        pytest.skip("paypal-reverse-html-rs native extension not built in this environment")

    html_samples = [
        (
            '<form name="challenge" action="/auth/verify?step=2">'
            '<input type="hidden" name="_csrf" value="tok1">'
            '<input type="text" name="email" value="buyer@example.com">'
            '<input type="submit" name="go" value="Continue">'
            '<select name="cc"><option value="GB">GB</option><option selected value="US">US</option></select>'
            "</form>"
        ),
        (
            '<form method="POST" action="https://www.paypal.com/checkoutnow/submit">'
            '<input type="hidden" name="a" value="1">'
            '<input type="text" name="signup_email" value="u@example.com">'
            "<button>Agree and Pay Now</button>"
            "</form>"
        ),
        '<div class="box"><form action="/auth/captcha/verify"><input type="hidden" name="x" value="y"></form></div>',
        "<html><body><div>no forms here</div></body></html>",
        "",
    ]
    url = "https://www.paypal.com/signin"

    rust_hidden = [reverse_protocol.extract_hidden_fields(h) for h in html_samples]
    rust_inputs = [reverse_protocol.extract_input_fields(h) for h in html_samples]
    rust_selects = [reverse_protocol.extract_select_fields(h) for h in html_samples]
    rust_signup = [reverse_protocol.find_signup_form(h, url) for h in html_samples]
    rust_submit = [reverse_protocol.find_submit_form(h, url) for h in html_samples]
    rust_challenge = [reverse_protocol.extract_challenge_form(h, url) for h in html_samples]

    for attr in (
        "_NATIVE_EXTRACT_HIDDEN_FIELDS",
        "_NATIVE_EXTRACT_INPUT_FIELDS",
        "_NATIVE_EXTRACT_SELECT_FIELDS",
        "_NATIVE_FIND_SIGNUP_FORM",
        "_NATIVE_FIND_SUBMIT_FORM",
        "_NATIVE_EXTRACT_CHALLENGE_FORM",
    ):
        monkeypatch.setattr(reverse_protocol, attr, None)

    assert rust_hidden == [reverse_protocol.extract_hidden_fields(h) for h in html_samples]
    assert rust_inputs == [reverse_protocol.extract_input_fields(h) for h in html_samples]
    assert rust_selects == [reverse_protocol.extract_select_fields(h) for h in html_samples]
    assert rust_signup == [reverse_protocol.find_signup_form(h, url) for h in html_samples]
    assert rust_submit == [reverse_protocol.find_submit_form(h, url) for h in html_samples]
    assert rust_challenge == [reverse_protocol.extract_challenge_form(h, url) for h in html_samples]


def test_wham_usage_parse_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if wham_usage._NATIVE_WHAM_USAGE is None:
        pytest.skip("wham-usage-rs native extension not built in this environment")

    samples = [
        {
            "usage": {
                "5h": {"used": 1200, "limit": 10000, "resets_at": "2026-11-27T00:00:00Z"},
                "7d": {"remaining": 250, "limit": 1000},
            }
        },
        json.dumps(
            {"rate_limits": {"5h": {"num_tokens_used": "333", "num_tokens_limit": "1000", "reset_at": 1700000000}}}
        ),
        {"rate_limits_info": {"five_hours": {"tokens_used": 5, "max": 50}, "weekly": {"consumed": 1, "cap": 2}}},
        {"5h": {"used": 3, "limit": 0}},
        "not json",
        None,
        {},
    ]
    rust_out = [wham_usage.parse_wham_usage(s) for s in samples]
    monkeypatch.setattr(wham_usage, "_NATIVE_PARSE_WHAM_USAGE", None)
    py_out = [wham_usage.parse_wham_usage(s) for s in samples]
    assert rust_out == py_out
