"""Dual-mode Python vs Rust parity tests for wave-8 `rust-anything` crates.

Covers `env-loader-rs`, `phone-proxy-rs`, `codex-jwt-rs`, `paypal-dom-rs`, and
the expanded `captcha-config-rs` surface (`_build_hcaptcha_bridge_html`).
"""

from __future__ import annotations

import base64
import json

import pytest

from sms_tool.browser import captcha_solver
from sms_tool.codex import export as codex_export
from sms_tool.kernel import env_loader
from sms_tool.paypal import dom_fields
from sms_tool.phone import phone_proxy


def _b64u(obj: object) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_env_loader_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if env_loader._NATIVE_ENV_LOADER is None:
        pytest.skip("env-loader-rs native extension not built in this environment")

    samples = [
        [],
        [
            "# comment",
            "   ",
            'API_KEY="abc\\n123"',
            "export TOKEN = 'sec\\'ret'",
            "BAD-KEY=1",
            "NO_EQ",
            "PLAIN=val=with=eq",
            "API_KEY=override",
        ],
        ["export  A=1", "exportB=2", "export=3", '  _UNDER_1 = "unicode 中文\\t\\r\\n\\"x\\""  '],
    ]
    rust_out = [env_loader._parse_lines(lines) for lines in samples]
    monkeypatch.setattr(env_loader, "_NATIVE_PARSE_LINES", None)
    py_out = [env_loader._parse_lines(lines) for lines in samples]
    assert rust_out == py_out


def test_phone_proxy_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if phone_proxy._NATIVE_PHONE_PROXY is None:
        pytest.skip("phone-proxy-rs native extension not built in this environment")

    samples = [
        ("", "US"),
        ("https://proxy.example/get?region=US", ""),
        ("https://proxy.example/get?key=abc&region=US&num=1", "jp"),
        ("https://proxy.example/get?region=de&format=txt#frag", "VN"),
        ("https://proxy.example/{region}/pick?r={REGION}", "ph"),
        ("https://proxy.example/static/list", "gb"),
        ("  https://proxy.example/get?a=1&b=hello+world&region=  ", "  sg  "),
    ]
    rust_out = [phone_proxy._api_url_for_region(u, iso) for u, iso in samples]
    monkeypatch.setattr(phone_proxy, "_NATIVE_API_URL_FOR_REGION", None)
    py_out = [phone_proxy._api_url_for_region(u, iso) for u, iso in samples]
    assert rust_out == py_out


def test_codex_jwt_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if codex_export._NATIVE_CODEX_JWT is None:
        pytest.skip("codex-jwt-rs native extension not built in this environment")

    tokens = [
        "",
        "not-a-jwt",
        "a..c",
        "a.中文.c",
        f"h.{_b64u({'sub': 'auth0|1', 'exp': 1900000000})}.s",
        f"h.{_b64u({'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1', 'chatgpt_plan_type': 'plus'}})}.s",
        f"h.{_b64u([1, 2, 3])}.s",
    ]
    rust_out = [codex_export._jwt_claims(t) for t in tokens]
    monkeypatch.setattr(codex_export, "_NATIVE_JWT_CLAIMS", None)
    py_out = [codex_export._jwt_claims(t) for t in tokens]
    assert rust_out == py_out


def test_paypal_dom_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if dom_fields._NATIVE_PAYPAL_DOM is None:
        pytest.skip("paypal-dom-rs native extension not built in this environment")

    samples = [
        ("+1 (415) 555-0199", "4155550199"),
        ("4111 1111 1111 4242", "4242"),
        ("Alice Smith", "alice"),
        ("anything", "   "),
        ("Straße 12", "straße"),
        ("电话：+86 138-0013-8000", "13800138000"),
        ("1234", "5678"),
    ]
    rust_out = [dom_fields._value_matches(a, e) for a, e in samples]
    monkeypatch.setattr(dom_fields, "_NATIVE_VALUE_MATCHES", None)
    py_out = [dom_fields._value_matches(a, e) for a, e in samples]
    assert rust_out == py_out


def test_captcha_bridge_html_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if captcha_solver._NATIVE_CAPTCHA_CONFIG is None:
        pytest.skip("captcha-config-rs native extension not built in this environment")

    samples = [
        ("frame-123", "https://b.stripecdn.com/HCaptchaInvisible.html?id=1", "sk-abc", "rq-xyz", "acct_123", "en-US"),
        ("frame-中文-😀", "https://example.com/wrap", 'site"key\\1', 'rq\ndata\t"x"', "", "zh-CN"),
        ("f\x08\x0c\r\n\t\x01", "https://a.test/", "k", "r", "m", ""),
    ]
    rust_out = [captcha_solver._build_hcaptcha_bridge_html(*args) for args in samples]
    monkeypatch.setattr(captcha_solver, "_NATIVE_BUILD_HCAPTCHA_BRIDGE_HTML", None)
    py_out = [captcha_solver._build_hcaptcha_bridge_html(*args) for args in samples]
    assert rust_out == py_out
