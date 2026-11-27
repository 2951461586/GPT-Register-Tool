"""Dual-mode Python vs Rust parity tests for wave-12 `rust-anything` crates.

Covers `capability-browser-parse-rs` and `qualification-batch-parse-rs`,
asserting identical outputs between the compiled PyO3 fast path and the
pure-Python fallback.
"""

from __future__ import annotations

import pytest

from sms_tool.payment import capability_browser, qualification_batch


def test_capability_browser_playwright_proxy_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if capability_browser._NATIVE_CAPABILITY_BROWSER_PARSE is None:
        pytest.skip("capability-browser-parse-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "   ",
        "http://127.0.0.1:8080",
        "https://user:pass@proxy.example.com:8443/path",
        "socks5h://u%401:p%3A2@10.0.0.1:1080",
        "socks5://user%20name:pass%2Fword@[::1]:10808",
        "http://[bad-ipv6:8080",
        "http://proxy.example:0",
        "no-scheme.example:8080",
    ]
    rust_out = [capability_browser._playwright_proxy(s) for s in samples]
    monkeypatch.setattr(capability_browser, "_NATIVE_PLAYWRIGHT_PROXY", None)
    py_out = [capability_browser._playwright_proxy(s) for s in samples]
    assert rust_out == py_out


def test_qualification_batch_parsers_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if qualification_batch._NATIVE_QUALIFICATION_BATCH_PARSE is None:
        pytest.skip("qualification-batch-parse-rs native extension not built in this environment")

    proxy_samples = [
        None,
        "",
        "127.0.0.1:8080",
        "http://user-sid-abc:pass@proxy.example.com:8080",
        "socks5h://user%40domain:p%3Aw@[2001:db8::1]:1080",
        "http://[invalid-v6:8080",
        "http://host.example:99999",
        "a:b:c:",
    ]
    reason_samples = [
        "",
        "   \t\n  ",
        (
            "ProxyError: HTTPSConnectionPool(host='api.stripe.com', port=443): "
            "Max retries exceeded (Caused by ProxyError('Tunnel connection failed: 503 Target host denied'))"
        ),
        "HTTP 407 Proxy Authentication Required (bad session)",
        "x" * 40 + " " + "y" * 200,
        "a" * 200,
        "中文错误信息：" + "连接超时测试 " * 30,
        "short error",
    ]

    rust_fp = [qualification_batch._proxy_fingerprint(p) for p in proxy_samples]
    rust_reason = [qualification_batch._stripe_reason_text(r) for r in reason_samples]

    monkeypatch.setattr(qualification_batch, "_NATIVE_PROXY_FINGERPRINT", None)
    monkeypatch.setattr(qualification_batch, "_NATIVE_STRIPE_REASON_TEXT", None)

    assert rust_fp == [qualification_batch._proxy_fingerprint(p) for p in proxy_samples]
    assert rust_reason == [qualification_batch._stripe_reason_text(r) for r in reason_samples]
