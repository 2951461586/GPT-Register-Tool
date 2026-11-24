"""Dual-mode Python vs Rust parity tests for `cfworker-html-rs` and `icloud-html-rs`."""

from __future__ import annotations

import pytest

from sms_tool.providers import cfworker_client, mailbox_icloud_url


def test_cfworker_html_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if cfworker_client._NATIVE_CFWORKER_HTML is None:
        pytest.skip("cfworker-html-rs native extension not built in this environment")

    html_samples = [
        None,
        "",
        "Plain text 444444",
        "<html><head><style>.code{font-size:24px}</style></head><body><p>你的验证码是 <strong>302959</strong></p></body></html>",
        "<div>Line 1<br/>Line 2 &amp; &mdash; &rsquo; &#x20AC; &#0; &#128;</div>",
        "#bodyCell { padding: 20px; } @media only screen { body { width: 100%; } } Code: 388302",
        {"a": "b", "n": [1, 2]},
    ]
    rust_display = [cfworker_client._display_text(sample) for sample in html_samples]
    rust_css = [cfworker_client._strip_leading_css(sample) for sample in html_samples]

    email_payloads = [
        ({"emails": ["A@Liziai.Cloud", "b@other.com", "a@liziai.cloud"]}, "liziai.cloud"),
        ({"items": [{"address": "User+1@Example.COM"}, "cc bob@example.com"]}, ""),
        (None, ""),
    ]
    rust_emails = [cfworker_client._extract_emails(payload, domain=dom) for payload, dom in email_payloads]

    monkeypatch.setattr(cfworker_client, "_NATIVE_DISPLAY_TEXT", None)
    monkeypatch.setattr(cfworker_client, "_NATIVE_STRIP_LEADING_CSS", None)
    monkeypatch.setattr(cfworker_client, "_NATIVE_EXTRACT_EMAILS", None)

    py_display = [cfworker_client._display_text(sample) for sample in html_samples]
    py_css = [cfworker_client._strip_leading_css(sample) for sample in html_samples]
    py_emails = [cfworker_client._extract_emails(payload, domain=dom) for payload, dom in email_payloads]

    assert rust_display == py_display
    assert rust_css == py_css
    assert rust_emails == py_emails


def test_icloud_html_rust_matches_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    if mailbox_icloud_url._NATIVE_ICLOUD_HTML is None:
        pytest.skip("icloud-html-rs native extension not built in this environment")

    samples = [
        "",
        "Plain text\r\nSecond line\n\nThird",
        "<html><!-- comment --><head><style>body{color:red}</style></head><body><p>Code: <b>654321</b></p><br/><p>OpenAI &mdash; Thanks &nbsp; &#128;</p></body></html>",
        "<div><svg><title>Icon</title></svg><script>var a=1;</script><noscript>Off</noscript>Visible 987654</div>",
        "Line A\u2028Line B\u2029Line C\x0bLine D",
    ]
    rust_out = [mailbox_icloud_url._strip_html_to_visible_text(sample) for sample in samples]

    monkeypatch.setattr(mailbox_icloud_url, "_NATIVE_STRIP_HTML_TO_VISIBLE_TEXT", None)
    py_out = [mailbox_icloud_url._strip_html_to_visible_text(sample) for sample in samples]

    assert rust_out == py_out
