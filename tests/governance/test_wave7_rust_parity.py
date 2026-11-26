"""Dual-mode Python vs Rust parity tests for wave-7 `rust-anything` crates.

Covers `batch-proxy-rs`, `profile-reclaim-rs`, `team-trial-rs`,
`account-scan-jwt-rs`, `liveness-parse-rs`, `sub2api-parse-rs`, plus the
expanded surfaces of `gmail-html-rs` (`_parse_proxy_url`) and `icloud-html-rs`
(`_same_origin`, `_extract_url_credentials`).
"""

from __future__ import annotations

import base64
import json

import pytest

from sms_tool.accounts import account_liveness, account_scan
from sms_tool.browser import profile_reclaim
from sms_tool.importers import sub2api
from sms_tool.pay_link import team_trial
from sms_tool.providers import mailbox_gmail, mailbox_icloud_url
from sms_tool.registration import batch_runner


def _b64u(obj: object) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_batch_proxy_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if batch_runner._NATIVE_BATCH_PROXY is None:
        pytest.skip("batch-proxy-rs native extension not built in this environment")

    samples = [
        [],
        ["http://u1:p@a.example:8080"],
        ["http://u1:p@a.example:8080", "http://u2:p@a.example:8080", "http://u1:p@a.example:8080"],
        [
            "http://a:1@h1.example:8080",
            "http://b:2@h1.example:8080",
            "http://c:3@h2.example:8080",
            "http://d:4@h3.example:9090",
            "http://e:5@h1.example:8080",
        ],
        ["http://[::1]:8080", "http://[::1]:8081", "http://[::1]:8080"],
    ]
    rust_out = [batch_runner._interleave_proxy_endpoints(s) for s in samples]
    monkeypatch.setattr(batch_runner, "_NATIVE_INTERLEAVE_PROXY_ENDPOINTS", None)
    py_out = [batch_runner._interleave_proxy_endpoints(s) for s in samples]
    assert rust_out == py_out


def test_profile_reclaim_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if profile_reclaim._NATIVE_PROFILE_RECLAIM is None:
        pytest.skip("profile-reclaim-rs native extension not built in this environment")

    cmd = (
        r'"C:\Program Files\Camoufox\camoufox.exe" --no-remote '
        r'-profile "F:\runtime\browser_profiles\camoufox\alice@example.com"'
    )
    samples = [
        (cmd, "f:/runtime/browser_profiles/camoufox/alice@example.com"),
        (cmd, "f:/runtime/browser_profiles/camoufox/alice"),
        (cmd, ""),
        (r"chrome.exe --user-data-dir=C:\tmp\prof1/", "c:/tmp/prof1"),
        (r"firefox.exe -profile=C:\tmp\prof2", "c:/tmp/prof2"),
        (r"notepad.exe C:\notes.txt", "c:/tmp/prof1"),
    ]
    rust_out = [profile_reclaim._references_profile(c, t) for c, t in samples]
    monkeypatch.setattr(profile_reclaim, "_NATIVE_REFERENCES_PROFILE", None)
    py_out = [profile_reclaim._references_profile(c, t) for c, t in samples]
    assert rust_out == py_out


def test_team_trial_and_account_scan_jwt_rust_match_python_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if team_trial._NATIVE_TEAM_TRIAL is None or account_scan._NATIVE_ACCOUNT_SCAN_JWT is None:
        pytest.skip("JWT wave-7 native extensions not built in this environment")

    tokens = [
        "",
        "not-a-jwt",
        "a..c",
        "a.中文.c",
        f"h.{_b64u({'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1', 'chatgpt_plan_type': 'plus'}})}.s",
        f"h.{_b64u({'https://api.openai.com/auth': {'chatgpt_account_id': 'default', 'plan_type': 'team'}})}.s",
        f"h.{_b64u({'https://api.openai.com/auth': {'chatgpt_account_id': '  acct-2  '}})}.s",
        f"h.{_b64u(['not-a-dict'])}.s",
    ]
    rust_aid = [team_trial._jwt_chatgpt_account_id(t) for t in tokens]
    rust_plan = [account_scan._jwt_plan_type(t) for t in tokens]

    monkeypatch.setattr(team_trial, "_NATIVE_JWT_CHATGPT_ACCOUNT_ID", None)
    monkeypatch.setattr(account_scan, "_NATIVE_JWT_PLAN_TYPE", None)

    py_aid = [team_trial._jwt_chatgpt_account_id(t) for t in tokens]
    py_plan = [account_scan._jwt_plan_type(t) for t in tokens]

    assert rust_aid == py_aid
    assert rust_plan == py_plan


def test_liveness_parse_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if account_liveness._NATIVE_LIVENESS_PARSE is None:
        pytest.skip("liveness-parse-rs native extension not built in this environment")

    samples = [
        (401, ""),
        (200, ""),
        (200, "HTTP 401 unauthorized"),
        (403, "code 14012 rate limited"),
        (403, "Your authentication token has been invalidated"),
        (400, "OAuth error: INVALID_GRANT"),
        (400, "missing refresh_token"),
        (500, "upstream gateway timeout"),
        (0, "中文401中文"),
        (200, "status=401; retry=false"),
    ]
    rust_out = [account_liveness._is_token_invalid(code, err) for code, err in samples]
    monkeypatch.setattr(account_liveness, "_NATIVE_IS_TOKEN_INVALID", None)
    py_out = [account_liveness._is_token_invalid(code, err) for code, err in samples]
    assert rust_out == py_out


def test_sub2api_parse_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if sub2api._NATIVE_SUB2API_PARSE is None:
        pytest.skip("sub2api-parse-rs native extension not built in this environment")

    origin_samples = [
        "",
        "   ",
        "https://panel.example.com/api/v1/accounts",
        "https://panel.example.com/sub/API/V1/users/",
        "https://panel.example.com/sub/api/admin",
        "https://panel.example.com/custom/path/?k=1#frag",
        "http://[::1]:8080/api/v1",
        "panel.example.com/api/v1",
        "https://panel.example.com/İ/api/v1",
    ]
    int_samples = [
        None,
        "",
        "1, 2, 3",
        "#1, 2；#1，0, -5, abc, 42\n#100",
        [1, "2", "#3", "#1", 0, -1, "bad", "1_000"],
    ]

    rust_origins = [sub2api._normalize_sub2api_origin(u) for u in origin_samples]
    rust_ints = [sub2api._parse_int_list(v) for v in int_samples]

    monkeypatch.setattr(sub2api, "_NATIVE_NORMALIZE_SUB2API_ORIGIN", None)
    monkeypatch.setattr(sub2api, "_NATIVE_PARSE_INT_LIST", None)

    py_origins = [sub2api._normalize_sub2api_origin(u) for u in origin_samples]
    py_ints = [sub2api._parse_int_list(v) for v in int_samples]

    assert rust_origins == py_origins
    assert rust_ints == py_ints


def test_gmail_and_icloud_wave7_additions_match_python_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if mailbox_gmail._NATIVE_GMAIL_HTML is None or mailbox_icloud_url._NATIVE_ICLOUD_HTML is None:
        pytest.skip("gmail-html-rs / icloud-html-rs native extensions not built in this environment")

    proxy_samples = [
        "127.0.0.1:7890",
        "http://127.0.0.1:7897",
        "https://proxy.example",
        "socks5://proxy.example",
        "socks5h://u%401:p%3A2@gw.example:1080",
        "http://[::1]:8080",
        "http://host:0",
    ]
    origin_pairs = [
        ("https://icloud-api.top/inbox", "https://icloud-api.top/api/pickup/messages"),
        ("https://icloud-api.top:443/inbox", "https://icloud-api.top/api/pickup/messages"),
        ("http://icloud-api.top:80/inbox", "http://icloud-api.top/api/pickup/messages"),
        ("https://icloud-api.top/inbox", "http://icloud-api.top/api/pickup/messages"),
        ("https://icloud-api.top/inbox", "https://evil.example/api/pickup/messages"),
    ]
    cred_pairs = [
        ("https://mail.example/inbox#email=Alice@Example.com&key=secret123", "fallback@example.com"),
        ("https://mail.example/inbox#/view?token=tok456&email=bob@example.com", "fallback@example.com"),
        ("https://mail.example/inbox?auth_code=ac789&email=carol@example.com", "fallback@example.com"),
        ("https://mail.example/inbox#/route-only", "FALLBACK@EXAMPLE.COM"),
    ]

    rust_px = [mailbox_gmail._parse_proxy_url(p) for p in proxy_samples]
    rust_so = [mailbox_icloud_url._same_origin(a, b) for a, b in origin_pairs]
    rust_cr = [mailbox_icloud_url._extract_url_credentials(u, f) for u, f in cred_pairs]

    monkeypatch.setattr(mailbox_gmail, "_NATIVE_PARSE_PROXY_URL", None)
    monkeypatch.setattr(mailbox_icloud_url, "_NATIVE_SAME_ORIGIN", None)
    monkeypatch.setattr(mailbox_icloud_url, "_NATIVE_EXTRACT_URL_CREDENTIALS", None)

    py_px = [mailbox_gmail._parse_proxy_url(p) for p in proxy_samples]
    py_so = [mailbox_icloud_url._same_origin(a, b) for a, b in origin_pairs]
    py_cr = [mailbox_icloud_url._extract_url_credentials(u, f) for u, f in cred_pairs]

    assert rust_px == py_px
    assert rust_so == py_so
    assert rust_cr == py_cr
