"""Behaviour tests for ``sms_tool/captcha_solver.py`` (round-5 audit target).

Why this file exists
--------------------
This module decides *which* CAPTCHA solver to run against a live PayPal/Stripe
page, and it is the only place the vendor site key is parsed out of raw HTML.
Two things are pinned here:

1. **The type classifier.** ``extract_captcha_config`` decides hCaptcha vs
   reCAPTCHA.  Its reCAPTCHA branch is unreachable (identical regex to the
   hCaptcha branch that returns first), so a real reCAPTCHA page is handed to
   the hCaptcha solver.  That is pinned as-is rather than "fixed in the test".

2. **The exception-chaining defect the audit flagged.** Inside bare
   ``except ImportError:`` handlers the module raises ``CaptchaError`` with no
   ``from exc``, so the original ImportError is only reachable via the implicit
   ``__context__`` and not via ``__cause__``.  The audit said 5 sites; the AST
   scan in this file pins the real count so the discrepancy is visible.

No browser is launched.  Playwright *is* installed in this environment, so the
two solver entry points are replaced with recording stubs for every test that
would otherwise reach them, and the ImportError path is provoked deliberately.
"""

from __future__ import annotations

import ast
import builtins
import json

import pytest

from sms_tool import captcha_solver
from sms_tool.captcha_solver import (
    CaptchaError,
    _accept_language_for_locale,
    _build_hcaptcha_bridge_html,
    _build_hcaptcha_bridge_url,
    _playwright_proxy,
    extract_captcha_config,
    solve_captcha,
)


# ─────────────────────────────── helpers ─────────────────────────────────────


@pytest.fixture
def no_browser(monkeypatch):
    """Replace the Playwright-driven solvers so nothing can launch a browser."""
    calls = []

    def _hcaptcha(**kwargs):
        calls.append(("hcaptcha", kwargs))
        return ("hc-token", "hc-ekey")

    def _recaptcha(**kwargs):
        calls.append(("recaptcha", kwargs))
        return ("rc-token", "rc-ekey")

    monkeypatch.setattr(captcha_solver, "_solve_hcaptcha", _hcaptcha)
    monkeypatch.setattr(captcha_solver, "_solve_recaptcha", _recaptcha)
    return calls


def missing_playwright(monkeypatch, module="playwright.sync_api"):
    """Make ``from playwright... import ...`` raise ImportError."""
    real_import = builtins.__import__

    def _fake(name, *args, **kwargs):
        if name == module or name.startswith(module + "."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake)


# ─────────────────────────── extract_captcha_config ──────────────────────────


def test_extract_captcha_config_reads_an_hcaptcha_site_key():
    html = '<h-captcha data-sitekey="hc-key-1"></h-captcha>'
    assert extract_captcha_config(html) == {"type": "hcaptcha", "site_key": "hc-key-1", "rqdata": ""}


def test_extract_captcha_config_reads_the_rqdata_alongside_the_site_key():
    html = '<div data-sitekey="hc-key-1" data-rqdata="rq-value"></div>'
    assert extract_captcha_config(html)["rqdata"] == "rq-value"


def test_extract_captcha_config_misclassifies_a_recaptcha_page_as_hcaptcha():
    """AUDIT POINT (confirmed defect): the reCAPTCHA branch is dead code.

    Lines 56-59 repeat the *same* regex as the hCaptcha branch at line 49, which
    already returned.  So a real Google reCAPTCHA widget (``data-sitekey`` on a
    ``.g-recaptcha`` div) is classified ``hcaptcha`` and handed to the hCaptcha
    solver, which will never produce a usable token for it.
    """
    html = '<div class="g-recaptcha" data-sitekey="rc-key-1"></div>'
    assert extract_captcha_config(html)["type"] == "hcaptcha"


def test_extract_captcha_config_only_reaches_recaptcha_via_js_variables():
    """The one working route to type='recaptcha' is the JSON-variable patterns."""
    assert extract_captcha_config('{"recaptchaSiteKey": "rc-key"}')["type"] == "recaptcha"
    assert extract_captcha_config('{"recaptcha_site_key": "rc-key"}')["type"] == "recaptcha"


def test_extract_captcha_config_reads_the_hcaptcha_js_variable():
    assert extract_captcha_config('{"hcaptcha_site_key": "hc"}') == {
        "type": "hcaptcha", "site_key": "hc", "rqdata": ""}


def test_extract_captcha_config_picks_up_rqdata_from_the_js_variables():
    html = '{"site_key": "sk", "hcaptcha_rqdata": "rq-9"}'
    assert extract_captcha_config(html)["rqdata"] == "rq-9"


def test_extract_captcha_config_returns_empty_strings_when_nothing_matches():
    assert extract_captcha_config("<html><body>no captcha</body></html>") == {
        "type": "", "site_key": "", "rqdata": ""}


@pytest.mark.parametrize("html", ["", "   ", "<<<>>>", '\x00\x01', "data-sitekey=", "{\"site_key\": "])
def test_extract_captcha_config_is_safe_on_malformed_html(html):
    assert extract_captcha_config(html)["type"] in ("", "hcaptcha", "recaptcha")


def test_extract_captcha_config_raises_on_none_instead_of_returning_empty():
    """No None guard, unlike the surrounding helper style in this codebase."""
    with pytest.raises(TypeError):
        extract_captcha_config(None)


def test_extract_captcha_config_uses_the_first_site_key_in_the_document():
    html = '<div data-sitekey="first"></div><div data-sitekey="second"></div>'
    assert extract_captcha_config(html)["site_key"] == "first"


def test_extract_captcha_config_is_idempotent():
    html = '<div data-sitekey="k" data-rqdata="r"></div>'
    assert [extract_captcha_config(html) for _ in range(4)] == [extract_captcha_config(html)] * 4


def test_extract_captcha_config_tolerates_a_very_large_document():
    html = ("<p>filler</p>" * 200_000) + '<div data-sitekey="deep-key"></div>'
    assert extract_captcha_config(html)["site_key"] == "deep-key"


# ─────────────────────────────── solve_captcha ───────────────────────────────


def test_solve_captcha_raises_when_the_page_has_no_captcha():
    with pytest.raises(CaptchaError, match="no CAPTCHA found"):
        solve_captcha("<html></html>")


def test_solve_captcha_raises_for_an_unsupported_type(no_browser, monkeypatch):
    monkeypatch.setattr(captcha_solver, "extract_captcha_config",
                        lambda html: {"type": "turnstile", "site_key": "k", "rqdata": ""})
    with pytest.raises(CaptchaError, match="unsupported CAPTCHA type: turnstile"):
        solve_captcha("anything")


def test_solve_captcha_raises_when_the_type_is_present_but_the_key_is_not(no_browser, monkeypatch):
    monkeypatch.setattr(captcha_solver, "extract_captcha_config",
                        lambda html: {"type": "hcaptcha", "site_key": "", "rqdata": ""})
    with pytest.raises(CaptchaError, match="no CAPTCHA found"):
        solve_captcha("anything")


def test_solve_captcha_dispatches_to_the_hcaptcha_solver(no_browser):
    token, ekey = solve_captcha('<div data-sitekey="hc-1" data-rqdata="rq"></div>',
                                proxy="http://u:p@h:1", headless=True, timeout_ms=5,
                                locale="zh-CN")
    assert (token, ekey) == ("hc-token", "hc-ekey")
    name, kwargs = no_browser[0]
    assert name == "hcaptcha"
    assert kwargs["site_key"] == "hc-1"
    assert kwargs["rqdata"] == "rq"
    assert kwargs["proxy"] == "http://u:p@h:1"
    assert kwargs["timeout_ms"] == 5
    assert kwargs["locale"] == "zh-CN"
    assert kwargs["headless"] is True
    assert kwargs["log"] is print


def test_solve_captcha_dispatches_to_the_recaptcha_solver(no_browser):
    token, _ = solve_captcha('{"recaptchaSiteKey": "rc-1"}')
    assert token == "rc-token"
    assert no_browser[0][0] == "recaptcha"


def test_solve_captcha_logs_only_a_prefix_of_the_site_key(no_browser, capsys):
    key = "a" * 60
    solve_captcha(f'{{"recaptchaSiteKey": "{key}"}}', log=print)
    out = capsys.readouterr().out
    assert key[:16] in out
    assert key not in out


def test_solve_captcha_accepts_a_custom_log_callable(no_browser):
    messages = []
    solve_captcha('{"recaptchaSiteKey": "rc-1"}', log=messages.append)
    assert any("detected recaptcha" in m for m in messages)


def test_solve_captcha_propagates_a_solver_failure(no_browser, monkeypatch):
    def _boom(**kwargs):
        raise CaptchaError("solver exploded")

    monkeypatch.setattr(captcha_solver, "_solve_recaptcha", _boom)
    with pytest.raises(CaptchaError, match="solver exploded"):
        solve_captcha('{"recaptchaSiteKey": "rc-1"}')


# ──────────────────────── _build_hcaptcha_bridge_url ─────────────────────────


def test_build_hcaptcha_bridge_url_defaults_to_the_invisible_page():
    url = _build_hcaptcha_bridge_url()
    assert "HCaptchaInvisible.html" in url
    assert _build_hcaptcha_bridge_url(invisible=False).endswith("HCaptcha.html?") or "HCaptcha.html" in \
           _build_hcaptcha_bridge_url(invisible=False)


def test_build_hcaptcha_bridge_url_uses_the_visible_page_when_asked():
    assert "HCaptcha.html" in _build_hcaptcha_bridge_url(invisible=False)


def test_build_hcaptcha_bridge_url_embeds_the_frame_id_and_origin():
    url = _build_hcaptcha_bridge_url(frame_id="frame-9", origin="https://js.stripe.com")
    assert "id=frame-9" in url
    assert "origin=https%3A%2F%2Fjs.stripe.com" in url


def test_build_hcaptcha_bridge_url_generates_a_unique_frame_id_per_call():
    first = _build_hcaptcha_bridge_url()
    second = _build_hcaptcha_bridge_url()
    assert first != second


def test_build_hcaptcha_bridge_url_is_deterministic_for_a_fixed_frame_id():
    assert _build_hcaptcha_bridge_url(frame_id="x") == _build_hcaptcha_bridge_url(frame_id="x")


def test_build_hcaptcha_bridge_url_points_at_the_stripe_cdn():
    assert _build_hcaptcha_bridge_url().startswith("https://b.stripecdn.com/")


# ──────────────────────── _build_hcaptcha_bridge_html ────────────────────────


def test_build_hcaptcha_bridge_html_embeds_the_wrapper_url_and_frame_id():
    html = _build_hcaptcha_bridge_html("fid-1", "https://cdn.example/w", "sk", "rq")
    assert 'src="https://cdn.example/w"' in html
    assert '"fid-1"' in html


def test_build_hcaptcha_bridge_html_embeds_the_site_key_and_rqdata():
    html = _build_hcaptcha_bridge_html("f", "u", "site-key-9", "rqdata-9")
    assert '"sitekey": "site-key-9"' in html
    assert '"rqdata": "rqdata-9"' in html


def test_build_hcaptcha_bridge_html_embeds_the_merchant_and_locale():
    html = _build_hcaptcha_bridge_html("f", "u", "sk", "rq", merchant_id="m-1", locale="de-DE")
    assert '"merchant_id": "m-1"' in html
    assert '"locale": "de-DE"' in html


def test_build_hcaptcha_bridge_html_escapes_quotes_in_the_js_payloads():
    """json.dumps escapes the double quote, so the JS string literal survives."""
    hostile = '"><script>alert(1)</script>'
    html = _build_hcaptcha_bridge_html("f", "u", hostile, "rq")
    assert json.dumps(hostile) in html


def test_build_hcaptcha_bridge_html_does_not_neutralise_a_script_close_tag():
    """AUDIT POINT (confirmed defect): `</script>` is injected verbatim.

    ``site_key`` / ``rqdata`` are scraped from the page being solved and are
    embedded into an *inline* ``<script>`` block via ``json.dumps``, which
    escapes quotes but not ``</script>`` (or ``<!--``).  A page carrying a
    crafted ``data-sitekey`` therefore terminates the script element early and
    gets its own markup parsed as HTML inside the local bridge page.

    Pinned by counting the closing tags: a well-formed bridge page has exactly
    one.
    """
    clean = _build_hcaptcha_bridge_html("f", "u", "sk", "rq")
    assert clean.count("</script>") == 1

    hostile = _build_hcaptcha_bridge_html("f", "u", 'x</script><img src=y>', "rq")
    assert hostile.count("</script>") == 3
    assert "<img src=y>" in hostile


def test_build_hcaptcha_bridge_html_has_the_same_hole_via_rqdata_and_frame_id():
    """Both other interpolated values share the missing escaping.

    ``site_key`` is embedded twice (init + execute payloads) and ``rqdata`` /
    ``frame_id`` once each, so the tag count grows by that many.
    """
    assert _build_hcaptcha_bridge_html("f", "u", "sk", "a</script>b").count("</script>") == 2
    assert _build_hcaptcha_bridge_html("a</script>b", "u", "sk", "rq").count("</script>") == 2


def test_build_hcaptcha_bridge_html_is_well_formed_for_ordinary_input():
    html = _build_hcaptcha_bridge_html("frame-1", "https://cdn/w", "sk", "rq")
    assert html.count("<script>") == 1 and html.count("</script>") == 1
    assert html.startswith("<!doctype html>")


def test_build_hcaptcha_bridge_html_escapes_a_hostile_wrapper_url():
    """The wrapper URL is interpolated raw - pin the current (unescaped) state."""
    hostile = '" onload="alert(1)'
    html = _build_hcaptcha_bridge_html("f", hostile, "sk", "rq")
    assert f'src="{hostile}"' in html


def test_build_hcaptcha_bridge_html_reports_to_the_local_bridge_endpoints():
    html = _build_hcaptcha_bridge_html("f", "u", "sk", "rq")
    for endpoint in ("/event", "/result", "/error"):
        assert endpoint in html


def test_build_hcaptcha_bridge_html_is_idempotent():
    a = _build_hcaptcha_bridge_html("f", "u", "sk", "rq")
    b = _build_hcaptcha_bridge_html("f", "u", "sk", "rq")
    assert a == b


# ────────────────────────────── _playwright_proxy ────────────────────────────


def test_playwright_proxy_returns_none_for_a_blank_url():
    assert _playwright_proxy("") is None
    assert _playwright_proxy("   ") is None
    assert _playwright_proxy(None) is None


def test_playwright_proxy_parses_host_port_and_credentials():
    assert _playwright_proxy("http://user:pw@1.2.3.4:8080") == {
        "server": "http://1.2.3.4:8080", "bypass": "127.0.0.1,localhost",
        "username": "user", "password": "pw"}


def test_playwright_proxy_normalizes_socks5h_to_socks5():
    assert _playwright_proxy("socks5h://h:1080")["server"] == "socks5://h:1080"


def test_playwright_proxy_omits_the_port_when_absent():
    assert _playwright_proxy("http://h")["server"] == "http://h"


def test_playwright_proxy_always_bypasses_localhost():
    assert _playwright_proxy("http://h:1")["bypass"] == "127.0.0.1,localhost"


def test_playwright_proxy_omits_absent_credentials():
    out = _playwright_proxy("http://h:1")
    assert "username" not in out and "password" not in out


def test_playwright_proxy_returns_none_when_there_is_no_host():
    assert _playwright_proxy("http:///path") is None


def test_playwright_proxy_swallows_malformed_urls():
    """The bare ``except Exception: return None`` hides the parse failure."""
    assert _playwright_proxy("http://[::1") is None


def test_playwright_proxy_is_idempotent():
    assert _playwright_proxy("http://u:p@h:1") == _playwright_proxy("http://u:p@h:1")


# ──────────────────────── _accept_language_for_locale ────────────────────────


@pytest.mark.parametrize("locale,expected", [
    ("zh-CN", "zh-CN,zh;q=0.9,en;q=0.8"),
    ("zh", "zh-CN,zh;q=0.9,en;q=0.8"),
    ("ZH-TW", "zh-CN,zh;q=0.9,en;q=0.8"),
    ("id-ID", "id-ID,id;q=0.9,en;q=0.8"),
    ("id", "id-ID,id;q=0.9,en;q=0.8"),
    ("fr-FR", "en-US,en;q=0.9"),
    ("en-US", "en-US,en;q=0.9"),
])
def test_accept_language_for_locale(locale, expected):
    assert _accept_language_for_locale(locale) == expected


@pytest.mark.parametrize("locale", [None, "", "   "])
def test_accept_language_for_locale_defaults_to_english(locale):
    assert _accept_language_for_locale(locale) == "en-US,en;q=0.9"


def test_accept_language_for_locale_strips_and_lowercases():
    assert _accept_language_for_locale("  ZH  ") == "zh-CN,zh;q=0.9,en;q=0.8"


def test_accept_language_for_locale_is_idempotent():
    assert _accept_language_for_locale("id-ID") == _accept_language_for_locale("id-ID")


# ────────────────── the missing `raise ... from exc` binding ─────────────────


def test_playwright_import_error_is_reported_without_an_explicit_cause(monkeypatch):
    """AUDIT POINT: `except ImportError:` has no `as exc`, so no `from exc`.

    The handler raises a fresh CaptchaError, so the original ImportError is only
    reachable through the implicit ``__context__``.  ``__cause__`` stays None,
    which means ``raise ... from`` was never used and the traceback does not
    carry a "direct cause" chain.
    """
    missing_playwright(monkeypatch)
    with pytest.raises(CaptchaError) as exc:
        captcha_solver.solve_recaptcha_on_page(page_url="https://example.invalid")
    assert exc.value.__cause__ is None
    assert isinstance(exc.value.__context__, ImportError)


def test_the_chained_import_error_message_is_lost(monkeypatch):
    """Only the generic 'pip install playwright' advice survives."""
    missing_playwright(monkeypatch)
    with pytest.raises(CaptchaError) as exc:
        captcha_solver._solve_hcaptcha(site_key="sk")
    assert "playwright is required" in str(exc.value)
    assert "No module named" not in str(exc.value)


def test_there_are_exactly_three_unbound_re_raise_sites():
    """AUDIT POINT: the audit claimed 5 sites; the real count is 3.

    Pinned with an AST scan (deterministic, no execution) so the number cannot
    drift silently.  Each site is a bare ``except`` whose body raises a brand-new
    exception with no ``from`` clause and no bound name.
    """
    source = open(captcha_solver.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.name:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Raise) and stmt.exc is not None and stmt.cause is None:
                sites.append(stmt.lineno)
    assert sorted(sites) == [324, 490, 606]


def test_captcha_error_is_a_plain_exception_subclass():
    assert issubclass(CaptchaError, Exception)
    assert CaptchaError("boom").args == ("boom",)


def test_captcha_error_message_is_preserved_when_chained():
    try:
        raise CaptchaError("inner")
    except CaptchaError as exc:
        assert str(exc) == "inner"
        assert exc.__cause__ is None
