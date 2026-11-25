"""Dual-mode Python vs Rust parity tests for wave-3 `rust-anything` crates.

Covers `pp-link-helpers-rs`, `account-identity-rs`, `playwright-proxy-rs`,
`account-recovery-rs`, and `account-inspection-rs`, asserting identical outputs
between the compiled PyO3 fast path and the pure-Python fallback.
"""

from __future__ import annotations

import base64
import json

import pytest

from sms_tool.accounts import account_identity, account_inspection, account_recovery
from sms_tool.payment import pp_link_helpers
from sms_tool.registration_drivers import browser_session


def _make_jwt(payload: object) -> str:
    hdr = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode("ascii").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{hdr}.{body}.sig_placeholder"


def test_pp_link_helpers_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if pp_link_helpers._NATIVE_PP_LINK_HELPERS is None:
        pytest.skip("pp-link-helpers-rs native extension not built in this environment")

    templates = [
        None,
        "",
        "proxy.example:8080:user-region-US-sid-a1b2c3:pass1",
        "proxy.example:8080@user-region-JP-st-tokyo-city-minato-sid-xyz99:pass2",
        "http://acct_custom_zone_US_s1:secret@ipwo.example:9000",
        "socks5h://u1:basepw-GB-sess77-30m@kookeey.example:1080",
        "http://u1:pw_country-fr_ttl-10m@vendor.example:8888",
        "http://plainuser-us@edge.example:3128",
        "http://127.0.0.1:7890",
    ]
    urls = [
        None,
        "",
        "https://www.paypal.com/agreements/approve?ba_token=BA-123456789&useraction=commit",
        "https://paypal.com/agreements/approve/?nolegacy=1&ba_token=BA-XYZ987",
        "https://www.paypal.com/agreements/approve?ba_token=",
        "https://evil-paypal.com.example/agreements/approve?ba_token=BA-111",
        "https://www.paypal.com/checkoutnow?token=EC-111",
    ]

    rust_norm = [pp_link_helpers.normalize_proxy_template(t) for t in templates]
    rust_cc = [pp_link_helpers.proxy_for_country_template(t, cc) for t in templates for cc in ("DE", "jp", "")]
    rust_url = [pp_link_helpers.is_paypal_ba_approve_url(u) for u in urls]

    monkeypatch.setattr(pp_link_helpers, "_NATIVE_NORMALIZE_PROXY_TEMPLATE", None)
    monkeypatch.setattr(pp_link_helpers, "_NATIVE_PROXY_FOR_COUNTRY_TEMPLATE", None)
    monkeypatch.setattr(pp_link_helpers, "_NATIVE_IS_PAYPAL_BA_APPROVE_URL", None)

    py_norm = [pp_link_helpers.normalize_proxy_template(t) for t in templates]
    py_cc = [pp_link_helpers.proxy_for_country_template(t, cc) for t in templates for cc in ("DE", "jp", "")]
    py_url = [pp_link_helpers.is_paypal_ba_approve_url(u) for u in urls]

    assert rust_norm == py_norm
    assert rust_cc == py_cc
    assert rust_url == py_url


def test_account_identity_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if account_identity._NATIVE_ACCOUNT_IDENTITY is None:
        pytest.skip("account-identity-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "http://user-region-US-sid-sess1001-t-5:pw1@gw.example:8080",
        "socks5h://u1:base-DE-sess2002-15m@kookeey.example:1080",
        "proxy.example:9000:user-sid-sess3003:pw2",
        "http://127.0.0.1:7890",
        "http://[::1]:10808",
    ]
    rust_out = [account_identity.proxy_egress_key(s) for s in samples]
    monkeypatch.setattr(account_identity, "_NATIVE_PROXY_EGRESS_KEY", None)
    py_out = [account_identity.proxy_egress_key(s) for s in samples]
    assert rust_out == py_out


def test_playwright_proxy_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if browser_session._NATIVE_PLAYWRIGHT_PROXY_MOD is None:
        pytest.skip("playwright-proxy-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "   ",
        "socks5h://127.0.0.1:10808",
        "socks5h://u%40test:p%3Atest@127.0.0.1:10808",
        "http://user1:pass1@proxy.example:8080",
        "proxy.example:8888:user2:pass2",
        "socks5://[::1]:1080",
        "198.51.100.10:3128",
    ]
    rust_out = [browser_session._playwright_proxy(s) for s in samples]
    monkeypatch.setattr(browser_session, "_NATIVE_PLAYWRIGHT_PROXY", None)
    py_out = [browser_session._playwright_proxy(s) for s in samples]
    assert rust_out == py_out


def test_account_recovery_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if account_recovery._NATIVE_ACCOUNT_RECOVERY is None:
        pytest.skip("account-recovery-rs native extension not built in this environment")

    redact_samples = [
        None,
        "",
        "timeout via http://user1:secret1@proxy.example:8080 and SOCKS5H://u2:p2@127.0.0.1:1080",
        "refresh failed token=rt_AbCdEf0123456789._~- end",
        "bearer " + _make_jwt({"sub": "u1", "exp": 1900000000}) + " leaked",
        "中文截断测试 " * 300,
    ]
    quota_samples = [
        {},
        {"ok": False, "error": "account_deactivated"},
        {"ok": False, "error": "You have been deleted or deactivated."},
        {"ok": False, "error": "redirected to /add_phone"},
        {"ok": False, "error": "mailbox_transport_unavailable"},
        {"ok": False, "error": "mailbox_auth_invalid"},
        {"ok": False, "mode": "cooldown", "error": "relogin_cooldown"},
        {
            "ok": False,
            "attempts": [
                {"mode": "chatgpt_email_otp", "error": "existing_login_password_verify_failed"},
            ],
        },
        {
            "ok": False,
            "attempts": [
                {"mode": "chatgpt_email_otp", "error": "otp_code_rejected"},
            ],
        },
        {"ok": False, "error": "generic_failure"},
    ]

    rust_redact = [account_recovery._redact_recovery_error(s) for s in redact_samples]
    rust_quota = [account_recovery._relogin_failure_quota_status(s) for s in quota_samples]

    monkeypatch.setattr(account_recovery, "_NATIVE_REDACT_RECOVERY_ERROR", None)
    monkeypatch.setattr(account_recovery, "_NATIVE_RELOGIN_FAILURE_QUOTA_STATUS", None)

    py_redact = [account_recovery._redact_recovery_error(s) for s in redact_samples]
    py_quota = [account_recovery._relogin_failure_quota_status(s) for s in quota_samples]

    assert rust_redact == py_redact
    assert rust_quota == py_quota


def test_account_inspection_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if account_inspection._NATIVE_ACCOUNT_INSPECTION is None:
        pytest.skip("account-inspection-rs native extension not built in this environment")

    samples = [
        None,
        "",
        "   ",
        "not-a-jwt",
        "a.b",
        "a..c",
        "a.!!!.c",
        _make_jwt({"sub": "u1", "exp": 1893456000}),
        "Bearer " + _make_jwt({"sub": "u2", "exp": 1760000123.5}),
        "  bearer   " + _make_jwt({"exp": 0}) + "  ",
        _make_jwt({"sub": "no_exp"}),
        _make_jwt({"exp": True}),
        _make_jwt({"exp": "1893456000"}),
        _make_jwt([1, 2, 3]),
    ]
    rust_out = [account_inspection.decode_jwt_exp(s) for s in samples]
    monkeypatch.setattr(account_inspection, "_NATIVE_DECODE_JWT_EXP", None)
    py_out = [account_inspection.decode_jwt_exp(s) for s in samples]
    assert rust_out == py_out
