"""Dual-mode Python vs Rust parity tests for wave-16 `rust-anything` crates.

Covers `account-2fa-reauth-rs`, `account-terminal-rs`,
`paypal-proxy-parse-rs`, and `stage-proxy-key-rs`, asserting identical outputs
between the compiled PyO3 fast paths and the pure-Python fallbacks.
"""

from __future__ import annotations

import pytest

from sms_tool.accounts import account_2fa, account_terminal
from sms_tool.paypal import proxy as paypal_proxy
from sms_tool.proxy import stage_health


def test_account_2fa_needs_reauth_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if account_2fa._NATIVE_ACCOUNT_2FA_REAUTH is None:
        pytest.skip("account-2fa-reauth-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        RuntimeError("MFA enroll HTTP 401: recent_auth_required"),
        RuntimeError("MFA enroll HTTP 403 forbidden"),
        ValueError("Please reauth to continue"),
        RuntimeError("step=Login_Challenge"),
        RuntimeError("MFA enroll HTTP 500 server error"),
        "İ_non_ascii_REAUTH_marker",
    ]
    rust_out = [account_2fa._needs_reauth(s) for s in samples]  # type: ignore[arg-type]
    monkeypatch.setattr(account_2fa, "_NATIVE_NEEDS_REAUTH", None)
    py_out = [account_2fa._needs_reauth(s) for s in samples]  # type: ignore[arg-type]
    assert rust_out == py_out


def test_account_terminal_deactivated_markers_match_python_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if account_terminal._NATIVE_ACCOUNT_TERMINAL is None:
        pytest.skip("account-terminal-rs native extension not built in this environment")

    text_samples: list[object] = [
        None,
        "",
        "short",
        '{"status":"registered","email":"u@example.com"}',
        '{"error":{"code":"ACCOUNT_DEACTIVATED"}}',
        "legacy account_deatived marker",
        "Your Account Has Been Deleted.",
        "This account has been deactivated by policy.",
        "User deleted or deactivated",
        "İ_non_ascii_ACCOUNT_DEACTIVATED_marker",
        "中文正常错误：连接超时",
        404,
    ]
    rust_texts = [account_terminal.text_has_account_deactivated(t) for t in text_samples]
    monkeypatch.setattr(account_terminal, "_NATIVE_TEXT_HAS_ACCOUNT_DEACTIVATED", None)
    py_texts = [account_terminal.text_has_account_deactivated(t) for t in text_samples]
    assert rust_texts == py_texts


def test_paypal_proxy_parse_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if paypal_proxy._NATIVE_PAYPAL_PROXY_PARSE is None:
        pytest.skip("paypal-proxy-parse-rs native extension not built in this environment")

    class ReadTimeout(Exception):
        pass

    err_samples: list[object] = [
        None,
        "",
        "curl: (35) TLS connect error",
        "HTTPSConnectionPool: Read timed out",
        "Max retries exceeded with url: /v1/checkout",
        "Unable to connect to proxy",
        "HTTP 403 Forbidden: target host denied",
        ReadTimeout("socket read timeout"),
        RuntimeError("Remote end closed connection without response"),
    ]
    proxy_samples: list[object] = [
        None,
        "",
        "   ",
        "127.0.0.1:7897",
        "https://user:pass@gate.example.com:8443",
        "socks5://acct:pw@1.2.3.4:1080",
        "socks5h://[::1]:1080",
        "socks4://127.0.0.1:1080",
        "gate.example.com:9000:user-region-us:secret",
    ]
    rust_errs = [paypal_proxy.is_retryable_network_error(e) for e in err_samples]
    rust_cands = [paypal_proxy._proxy_scheme_candidates(p) for p in proxy_samples]  # type: ignore[arg-type]

    monkeypatch.setattr(paypal_proxy, "_NATIVE_IS_RETRYABLE_NETWORK_ERROR", None)
    monkeypatch.setattr(paypal_proxy, "_NATIVE_PROXY_SCHEME_CANDIDATES", None)
    py_errs = [paypal_proxy.is_retryable_network_error(e) for e in err_samples]
    py_cands = [paypal_proxy._proxy_scheme_candidates(p) for p in proxy_samples]  # type: ignore[arg-type]

    assert rust_errs == py_errs
    assert rust_cands == py_cands


def test_stage_proxy_key_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if stage_health._NATIVE_STAGE_PROXY_KEY is None:
        pytest.skip("stage-proxy-key-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        "   ",
        "http://127.0.0.1:7897",
        "socks5h://127.0.0.1:1080",
        "http://user-region-us-sid-abc123:secret@gate.provider.io:8080",
        "http://user-region-us-sid-xyz789:secret@gate.provider.io:8080",
        "gate.example.com:9000:customer-country-jp-sessid-998877:pass123",
        "http://rola_abc123-region-de:pass@1.2.3.4:3128",
        "http://user:base-US-sid999-30m@kookeey.example.com:8000",
        "http://[::1]:8080",
    ]
    rust_out = [stage_health.proxy_key(s) for s in samples]  # type: ignore[arg-type]
    assert rust_out[5] == rust_out[6]
    monkeypatch.setattr(stage_health, "_NATIVE_PROXY_KEY", None)
    py_out = [stage_health.proxy_key(s) for s in samples]  # type: ignore[arg-type]
    assert rust_out == py_out
