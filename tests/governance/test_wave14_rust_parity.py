"""Dual-mode Python vs Rust parity tests for wave-14 `rust-anything` crates.

Covers `account-promotion-url-rs`, `session-refresh-parse-rs`,
`auth-password-step-rs`, `proxy-bridge-parse-rs`, and
`browser-flow-recovery-rs`, asserting identical outputs between the compiled
PyO3 fast paths and the pure-Python fallbacks.
"""

from __future__ import annotations

import pytest

from sms_tool.accounts import account_promotion
from sms_tool.auth_flow import password_step
from sms_tool.proxy import bridge
from sms_tool.registration_drivers.browser_flow import recovery
from sms_tool.session import refresh as session_refresh
from sms_tool.transport import endpoints


def test_account_promotion_coupon_url_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if account_promotion._NATIVE_ACCOUNT_PROMOTION_URL is None:
        pytest.skip("account-promotion-url-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "   ",
        "\t\n\u0085",
        "plus-1-month-free",
        "  team-1-month-free  ",
        "plus-2-months-50-pct-off",
        "promo~2026.11-test_v1",
        "a/b c+d%e&f=g?h#i",
        "coupon/中文 🚀",
        "   \u3000plus-unicode-space\u2000  ",
        12345,
    ]
    rust_out = [account_promotion.coupon_url_for_campaign(s) for s in samples]  # type: ignore[arg-type]
    monkeypatch.setattr(account_promotion, "_NATIVE_COUPON_URL_FOR_CAMPAIGN", None)
    py_out = [account_promotion.coupon_url_for_campaign(s) for s in samples]  # type: ignore[arg-type]
    assert rust_out == py_out


def test_account_promotion_coupon_url_respects_chatgpt_base_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(endpoints, "CHATGPT_BASE", "https://custom.chatgpt.test")
    url = account_promotion.coupon_url_for_campaign("team-1-month-free")
    assert url.startswith(
        "https://custom.chatgpt.test/backend-api/promo_campaign/check_coupon?coupon=team-1-month-free"
    )


def test_session_refresh_has_session_cookie_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if session_refresh._NATIVE_SESSION_REFRESH_PARSE is None:
        pytest.skip("session-refresh-parse-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "   ",
        "; ; ;",
        "oai-did=abc; __cf_bm=xyz",
        "__Secure-next-auth.session-token=tok",
        "oai-did=1;  __Secure-next-auth.session-token.0 = part0 ; b=2",
        "__Secure-next-auth.session-token",
        "__Secure-next-auth.session-token2=nope",
        "__secure-next-auth.session-token=wrongcase",
        "token=__Secure-next-auth.session-token=in_value",
        "\t\u0085 __Secure-next-auth.session-token \t =val",
    ]
    rust_out = [session_refresh._has_session_cookie(s) for s in samples]
    monkeypatch.setattr(session_refresh, "_NATIVE_HAS_SESSION_COOKIE", None)
    py_out = [session_refresh._has_session_cookie(s) for s in samples]
    assert rust_out == py_out


def test_auth_password_step_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if password_step._NATIVE_AUTH_PASSWORD_STEP is None:
        pytest.skip("auth-password-step-rs native extension not built in this environment")

    samples: list[tuple[object, object]] = [
        ("", None),
        (None, None),
        ("https://auth.openai.com/log-in/password", None),
        ("https://AUTH.OPENAI.COM/LOG-IN/PASSWORD///?state=1#frag", None),
        ("https://auth.openai.com/create-account/password;s=1?x=1", None),
        ("  \thttps://auth.openai.com/log-in/password\r\n ", None),
        ("https://auth.openai.com/email-verification", None),
        ("https://chatgpt.com/log-in/password", None),
        ("https://auth.openai.com/email-verification", {"page": {"type": "PASSWORD"}}),
        ("", {"page": {"type": "  login_password "}}),
        ("", {"page": {"type": ""}, "page_type": "create_account_password"}),
        ("", {"page": {"type": 0}, "page_type": "password"}),
        ("", {"page": {"type": ["x"]}, "page_type": "password"}),
        ("", {"page": {"type": {"k": 1}}, "page_type": "password"}),
        ("", {"page": {"type": object()}, "page_type": "password"}),
        ("", {"page": "not-a-dict", "page_type": "login_password"}),
        ("", {"page": {"type": "email_otp"}}),
    ]
    rust_out = [password_step._is_login_password_step(u, p) for u, p in samples]
    with pytest.raises(ValueError):
        password_step._is_login_password_step("https://[::1/bad-ipv6")

    monkeypatch.setattr(password_step, "_NATIVE_IS_LOGIN_PASSWORD_STEP", None)
    py_out = [password_step._is_login_password_step(u, p) for u, p in samples]
    assert rust_out == py_out
    with pytest.raises(ValueError):
        password_step._is_login_password_step("https://[::1/bad-ipv6")


def test_proxy_bridge_parse_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if bridge._NATIVE_PROXY_BRIDGE_PARSE is None:
        pytest.skip("proxy-bridge-parse-rs native extension not built in this environment")

    host_samples = [
        "127.0.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "255.255.255.255 ",
        "127.1",
        "0177.0.0.1",
        "0x7f.1",
        "4294967295",
        "08.0.0.1",
        "1.2.3.4 ",
        "1.2.3.4 foo",
        " 1.2.3.4",
        "::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "fe80::1%eth0",
        "[::1]",
        "chatgpt.com",
        "",
    ]
    rules = ("gvt1.com", "dl.google.com", "telemetry.mozilla.org")
    block_samples = [
        ("", rules),
        ("   ", rules),
        ("...", rules),
        ("gvt1.com", rules),
        ("GVT1.COM.", rules),
        ("rr8.gvt1.com", rules),
        ("notgvt1.com", rules),
        ("sub.telemetry.mozilla.org.", rules),
        ("chatgpt.com", rules),
        ("gvt1.com", ()),
    ]

    rust_atyp = [bridge._addr_type(h) for h in host_samples]
    rust_blocked = [bridge._host_matches_blocked(h, r) for h, r in block_samples]
    with pytest.raises(ValueError):
        bridge._addr_type("bad\x00host")

    monkeypatch.setattr(bridge, "_NATIVE_ADDR_TYPE", None)
    monkeypatch.setattr(bridge, "_NATIVE_HOST_MATCHES_BLOCKED", None)

    assert rust_atyp == [bridge._addr_type(h) for h in host_samples]
    assert rust_blocked == [bridge._host_matches_blocked(h, r) for h, r in block_samples]
    with pytest.raises(ValueError):
        bridge._addr_type("bad\x00host")


def test_browser_flow_recovery_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if recovery._NATIVE_BROWSER_FLOW_RECOVERY is None:
        pytest.skip("browser-flow-recovery-rs native extension not built in this environment")

    samples: list[object] = [
        None,
        "",
        0,
        False,
        "account_deactivated",
        "Page.goto: net::ERR_ABORTED",
        "Navigation failed: abort requested",
        "Navigation succeeded cleanly",
        "curl: (35) TLS connect error: OPENSSL_internal",
        "Proxy tunnel timed out",
        RuntimeError("Page.goto: Target closed"),
        RuntimeError("invalid_grant"),
        "中文错误：page.goto 加载中断",
    ]
    rust_out = [recovery.is_navigation_retryable(s) for s in samples]
    monkeypatch.setattr(recovery, "_NATIVE_IS_NAVIGATION_RETRYABLE", None)
    py_out = [recovery.is_navigation_retryable(s) for s in samples]
    assert rust_out == py_out
