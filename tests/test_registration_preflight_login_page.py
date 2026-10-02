"""Preflight login entry page + Cloudflare classification (2026-10-01 scan P0-1/P0-2).

P0-1: the preflight must probe the page a real browser navigates to
(``chatgpt.com/auth/login?next=%2F``) instead of the standalone
``auth.openai.com/log-in`` the protocol used to hit, which packet captures show
a browser never requests outside an in-flow authorize redirect.

P0-2: an edge refusal must be classified as a Cloudflare challenge and named with
:data:`CLOUDFLARE_CHALLENGE_MARKER`, so the preflight caller can drop the host
instead of counting it as a generic failure.  These tests pin the boundary only;
they do not claim the change was validated on a live batch.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sms_tool import cli
from sms_tool import registration_preflight as rp

_CHATGPT_BASE = "https://chatgpt.com"
_AUTH_BASE = "https://auth.openai.com"
_CFG = {
    "chatgpt": {"chat_base_url": _CHATGPT_BASE, "auth_base_url": _AUTH_BASE},
    "registration": {},
}


class _Resp:
    def __init__(self, status_code: int, text: str = "", url: str = "", headers=None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers or {}


def _install_probe(monkeypatch, responder, *, config=None):
    monkeypatch.setattr(rp, "CFG", config or _CFG)
    monkeypatch.setattr(rp, "curl_cffi_capabilities", lambda: {"version_ok": True})
    monkeypatch.setattr(rp, "auth_fingerprint_capabilities", lambda: {"missing": []})
    monkeypatch.setattr(rp, "current_auth_fingerprint", lambda: {"impersonate": "chrome146"})
    calls: list[str] = []

    def fake_request(_session, _method, url, **_kwargs):
        calls.append(url)
        return responder(url)

    monkeypatch.setattr(rp, "request_with_retry", fake_request)
    return calls


def _login_url(calls: list[str]) -> str:
    return next(url for url in calls if "sentinel" not in url and "backend-api" not in url)


def test_default_preflight_probes_the_browser_login_page(monkeypatch):
    calls = _install_probe(monkeypatch, lambda url: _Resp(200 if "sentinel" not in url else 200))

    result = rp.registration_network_preflight(None)

    assert result["ok"] is True
    assert _login_url(calls) == f"{_CHATGPT_BASE}/auth/login?next=%2F"
    assert not any(_AUTH_BASE + "/log-in" in url for url in calls)


def test_legacy_switch_restores_the_standalone_auth_login_probe(monkeypatch):
    calls = _install_probe(
        monkeypatch,
        lambda url: _Resp(200),
        config={**_CFG, "registration": {"preflight_login_page": "legacy"}},
    )

    rp.registration_network_preflight(None)

    assert _login_url(calls) == f"{_AUTH_BASE}/log-in"


def test_cloudflare_refusal_is_named_with_the_challenge_marker(monkeypatch):
    def responder(url):
        if "sentinel" in url or "backend-api" in url:
            return _Resp(200)
        return _Resp(403, text="<html>cf-mitigated: challenge</html>")

    _install_probe(monkeypatch, responder)

    with pytest.raises(RuntimeError) as excinfo:
        rp.registration_network_preflight(None)

    assert rp.CLOUDFLARE_CHALLENGE_MARKER in str(excinfo.value)
    assert "chatgpt-login" in str(excinfo.value)


def test_plain_4xx_is_not_reported_as_a_cloudflare_challenge(monkeypatch):
    """Only an edge *refusal* (403/challenge) is Cloudflare; a 404 is a plain failure.

    A bare 403 at the Cloudflare-fronted login page is treated as an edge refusal
    by design (``proxy_edge_probe.classify_edge_response``), so this test uses a
    404 to separate "refused the exit" from "the request was wrong".
    """

    def responder(url):
        if "sentinel" in url or "backend-api" in url:
            return _Resp(200)
        return _Resp(404, text="Not Found")

    _install_probe(monkeypatch, responder)

    with pytest.raises(RuntimeError) as excinfo:
        rp.registration_network_preflight(None)

    assert rp.CLOUDFLARE_CHALLENGE_MARKER not in str(excinfo.value)
    assert "http_404" in str(excinfo.value)


def test_bare_edge_403_is_treated_as_a_blocked_exit(monkeypatch):
    """Any 403 at the login edge refuses the exit, challenge page or not."""

    def responder(url):
        if "sentinel" in url or "backend-api" in url:
            return _Resp(200)
        return _Resp(403, text="Forbidden")

    _install_probe(monkeypatch, responder)

    with pytest.raises(RuntimeError) as excinfo:
        rp.registration_network_preflight(None)

    assert rp.CLOUDFLARE_CHALLENGE_MARKER in str(excinfo.value)


def _args(**overrides):
    values = {"proxy": "http://first.example:8080", "proxy_explicit": False, "proxy_pool": ""}
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(**registration):
    return {"registration": {"driver": "protocol", **registration}, "proxy": {}}


def test_preflight_command_reports_a_cloudflare_challenge(monkeypatch, capsys):
    """The command layer must surface the Cloudflare verdict, not a generic 失败."""
    pool = ["http://blocked.example:8080"]

    def probe(proxy, **_kwargs):
        raise RuntimeError(f"registration_preflight_failed:chatgpt-login:{rp.CLOUDFLARE_CHALLENGE_MARKER}")

    monkeypatch.setattr(cli, "CFG", _config())
    with (
        patch.object(cli, "_proxy_pool_values", return_value=pool),
        patch("sms_tool.registration.registration_network_preflight", side_effect=probe),
    ):
        with pytest.raises(RuntimeError):
            cli._preflight_registration_before_mailbox(_args())

    assert "被 Cloudflare 挑战" in capsys.readouterr().out
