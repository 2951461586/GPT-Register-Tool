"""Dual-mode Python vs Rust parity tests for wave-5 `rust-anything` crates.

Covers `sentinel-tokens-rs`, `proxy-edge-probe-rs`, `auth-headers-rs`, and the
expanded `normalize-proxy-url-rs` surface (`_mask_credentials`,
`redact_proxy_text`, `retarget_region`, `infer_region`), asserting identical
outputs between the compiled PyO3 fast path and the pure-Python fallback, plus
pinning `CHALLENGE_MARKERS` between Python and Rust.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sms_tool.proxy import edge_probe, entry as proxy_entry
from sms_tool.registration import sentinel_tokens
from sms_tool.transport import auth_headers

ROOT = Path(__file__).resolve().parents[2]


def test_proxy_edge_probe_markers_match_rust_port() -> None:
    rust_port = ROOT / "rust" / "proxy-edge-probe-rs" / "src" / "port.rs"
    text = rust_port.read_text(encoding="utf-8")
    match = re.search(r"pub const CHALLENGE_MARKERS:\s*\[&str;\s*\d+\]\s*=\s*\[(?P<body>.*?)\];", text, re.S)
    assert match is not None, "CHALLENGE_MARKERS table missing from rust/proxy-edge-probe-rs/src/port.rs"
    rust_markers = tuple(re.findall(r'"([^"]+)"', match.group("body")))
    assert rust_markers == tuple(edge_probe._CHALLENGE_MARKERS), (
        f"CHALLENGE_MARKERS drifted between Python ({edge_probe._CHALLENGE_MARKERS}) and Rust ({rust_markers})"
    )


def test_normalize_proxy_url_wave5_additions_match_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if proxy_entry._NATIVE_PROXY_ENTRY is None:
        pytest.skip("normalize-proxy-url-rs native extension not built in this environment")

    mask_samples = [
        "",
        "plain log line without proxy",
        "http://alice:p@ssword@proxy.example:8080/path",
        "socks5h://proxy.example:1080:user1:pass1",
        "user1:pass1@proxy.example:8080",
        "user@example.com",
        "proxy.example:8080:user1:pass1",
        "[::1]:1080:user1:pass1",
        "user1:pass1:proxy.example:8080",
    ]
    redact_samples = [
        ("connect failed via http://u1:s1@gw.example:8080/", "http://u1:s1@gw.example:8080/"),
        ("proxy err gw.example:9000:u2:p2 timeout", "gw.example:9000:u2:p2"),
        ("nested socks5h://alice:p@ss@host.example:1080 failed", None),
        ("", None),
        (None, None),
    ]
    retarget_samples = [
        ("http://user-region-US-sid-abc123-t-5:pw@gw.example:8080", "jp"),
        ("http://acct-geo-VN-sid-999-ttl-10:pw@gw.example:8080", "DE"),
        ("http://SYNTH_1-country-vn:pw@rola.example:7000", "us"),
        ("http://acct_custom_zone_US_s1:secret@ipwo.example:9000", "gb"),
        ("socks5h://u1:basepw-US-sess77-30m@kookeey.example:1080", "FR"),
        ("http://u1:pw_country-de_session-1234@iproyal.example:12321", "SG"),
        ("http://plain:pw@gw.example:8080", "US"),
        ("", "US"),
    ]
    infer_samples = [
        "http://user-region-JP-sid-abc:pw@gw.example:8080",
        "http://acct-geo-VN-sid-99:pw@gw.example:8080",
        "http://acct_custom_zone_DE_s1:pw@ipwo.example:9000",
        "socks5h://u1:basepw-GB-sess77-30m@kookeey.example:1080",
        "http://plainuser-fr:pw@gw.example:8080",
        "http://127.0.0.1:7890",
        "",
    ]

    rust_mask = [proxy_entry._mask_credentials(s) for s in mask_samples]
    rust_redact = [proxy_entry.redact_proxy_text(v, p) for v, p in redact_samples]
    rust_retarget = [proxy_entry.retarget_region(p, cc) for p, cc in retarget_samples]
    rust_infer = [proxy_entry.infer_region(p) for p in infer_samples]

    monkeypatch.setattr(proxy_entry, "_NATIVE_MASK_CREDENTIALS", None)
    monkeypatch.setattr(proxy_entry, "_NATIVE_REDACT_PROXY_TEXT", None)
    monkeypatch.setattr(proxy_entry, "_NATIVE_RETARGET_REGION", None)
    monkeypatch.setattr(proxy_entry, "_NATIVE_INFER_REGION", None)

    py_mask = [proxy_entry._mask_credentials(s) for s in mask_samples]
    py_redact = [proxy_entry.redact_proxy_text(v, p) for v, p in redact_samples]
    py_retarget = [proxy_entry.retarget_region(p, cc) for p, cc in retarget_samples]
    py_infer = [proxy_entry.infer_region(p) for p in infer_samples]

    assert rust_mask == py_mask
    assert rust_redact == py_redact
    assert rust_retarget == py_retarget
    assert rust_infer == py_infer


def test_sentinel_tokens_pow_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if sentinel_tokens._NATIVE_SENTINEL_TOKENS is None:
        pytest.skip("sentinel-tokens-rs native extension not built in this environment")

    samples = [
        ("seed-alpha", "0ffff"),
        ("seed-beta", "00fff"),
        ("0.918273645", "000fff"),
        ("sentinel-req-1", "ffff"),
        ("sentinel-req-2", "0x00fff"),
        ("sentinel-req-3", " 00_ff "),
        ("sentinel-req-4", ""),
        ("sentinel-req-5", "not-hex"),
        ("sentinel-req-6", "-0fff"),
        ("中文seed", "00fff"),
    ]
    rust_out = [sentinel_tokens._solve_pow(seed, diff) for seed, diff in samples]
    monkeypatch.setattr(sentinel_tokens, "_NATIVE_SOLVE_POW", None)
    py_out = [sentinel_tokens._solve_pow(seed, diff) for seed, diff in samples]
    assert rust_out == py_out


def test_proxy_edge_probe_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if edge_probe._NATIVE_PROXY_EDGE_PROBE is None:
        pytest.skip("proxy-edge-probe-rs native extension not built in this environment")

    samples = [
        (0, "", None),
        (-1, "", None),
        (200, "<html>ChatGPT</html>", {"server": "cloudflare"}),
        (302, "", {"location": "https://auth.openai.com/log-in"}),
        (401, '{"detail":"Unauthorized"}', None),
        (403, "", None),
        (429, "rate limited", {"retry-after": "30"}),
        (429, "<title>Just a moment...</title>", {"server": "cloudflare"}),
        (503, "busy", {"cf-mitigated": "challenge"}),
        (502, "bad gateway", [("Server", "cloudflare")]),
        (400, "Enable JavaScript and cookies to continue", None),
        (429, "x" * 70000 + "cf-chl", None),
        ("not-a-number", "", {}),  # type: ignore[arg-type]
    ]
    rust_out = [edge_probe.classify_edge_response(code, body, headers) for code, body, headers in samples]
    monkeypatch.setattr(edge_probe, "_NATIVE_CLASSIFY_EDGE_RESPONSE", None)
    py_out = [edge_probe.classify_edge_response(code, body, headers) for code, body, headers in samples]
    assert rust_out == py_out


def test_auth_headers_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if auth_headers._NATIVE_AUTH_HEADERS is None:
        pytest.skip("auth-headers-rs native extension not built in this environment")

    version_samples = ["firefox144", "chrome146", "safari18_4_ios", "edge101", "", "unknown"]
    fvl_samples = [
        ('"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"', "146.0.7680.178"),
        ('"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"', "145.0.7632.110"),
        ('"Chromium";v="146.0.7680.178", "Not-A.Brand";v="24"', ""),
        ("", "146.0.7680.178"),
    ]
    enc_samples = [
        "firefox144",
        "firefox120",
        "chrome146",
        "chrome120",
        "edge123",
        "edge101",
        "safari18_4",
        "safari18_4_ios",
        "safari26_3",
        "safari26_3_ios",
        "safari260",
        "safari270",
        "unknown",
    ]
    ref_samples = [
        "https://auth.openai.com/log-in?client_id=abc",
        "https://chatgpt.com/backend-api/payments/checkout",
        "http://[::1]:8080/path",
        "http://[invalid/path",
        "not-a-url",
        "",
    ]

    rust_ver = [auth_headers._browser_version(v) for v in version_samples]
    rust_fvl = [auth_headers._full_version_list(ch, fv) for ch, fv in fvl_samples]
    rust_enc = [auth_headers.accept_encoding_for(v) for v in enc_samples]
    rust_ref = [auth_headers.origin_from_referer(r) for r in ref_samples]

    monkeypatch.setattr(auth_headers, "_NATIVE_BROWSER_VERSION", None)
    monkeypatch.setattr(auth_headers, "_NATIVE_FULL_VERSION_LIST", None)
    monkeypatch.setattr(auth_headers, "_NATIVE_ACCEPT_ENCODING_FOR", None)
    monkeypatch.setattr(auth_headers, "_NATIVE_ORIGIN_FROM_REFERER", None)

    py_ver = [auth_headers._browser_version(v) for v in version_samples]
    py_fvl = [auth_headers._full_version_list(ch, fv) for ch, fv in fvl_samples]
    py_enc = [auth_headers.accept_encoding_for(v) for v in enc_samples]
    py_ref = [auth_headers.origin_from_referer(r) for r in ref_samples]

    assert rust_ver == py_ver
    assert rust_fvl == py_fvl
    assert rust_enc == py_enc
    assert rust_ref == py_ref
