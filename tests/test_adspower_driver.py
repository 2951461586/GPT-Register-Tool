"""Tests for the AdsPower browser driver (``AdsPowerBrowserSession``)."""

from __future__ import annotations

import pytest

from sms_tool.registration_drivers import external_sessions as ext
from sms_tool.registration_drivers.base import (
    BROWSER_REGISTRATION_DRIVERS,
    BrowserRegistrationError,
    normalize_registration_driver,
)
from sms_tool.registration_drivers.external_sessions import (
    AdsPowerBrowserSession,
    create_browser_session,
)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _StubBrowser:
    contexts = []

    def new_context(self, **_kw):
        return _StubBrowser()

    def close(self):
        pass


class _StubPW:
    class chromium:
        @staticmethod
        def connect_over_cdp(addr, timeout=0):
            return _StubBrowser()


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(
        AdsPowerBrowserSession, "_start_playwright",
        lambda self: setattr(self, "_playwright", _StubPW()),
    )
    monkeypatch.setattr(
        AdsPowerBrowserSession, "_adopt_browser", lambda self, _b: None,
    )


def _make_session(monkeypatch, config):
    return AdsPowerBrowserSession(
        config=config,
        proxy=None, headless=False, timeout_ms=1000,
        locale="en-US", timezone_id="UTC",
    )


# --------------------------------------------------------------------------
# registration wiring
# --------------------------------------------------------------------------

def test_adspower_is_a_supported_browser_driver():
    assert "adspower" in BROWSER_REGISTRATION_DRIVERS


@pytest.mark.parametrize("name", ["adspower", "adsp", "ap", "adspower_browser"])
def test_normalize_accepts_adspower_aliases(name):
    assert normalize_registration_driver(name) == "adspower"


def test_create_browser_session_dispatches_adspower():
    session = create_browser_session(
        "adspower",
        config={"registration": {"drivers": {"adspower": {"user_id": "x"}}}},
        proxy=None, headless=False, timeout_ms=1000,
        locale="en-US", timezone_id="UTC",
    )
    assert isinstance(session, AdsPowerBrowserSession)


# --------------------------------------------------------------------------
# API plumbing
# --------------------------------------------------------------------------

def test_api_get_returns_data_on_success(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=0):
        captured["url"] = url
        captured["params"] = params
        return _Resp({"code": 0, "msg": "ok", "data": {"ws": {"playwright": "ws://1.2.3.4:9999/x"}}})

    monkeypatch.setattr(ext.curl_requests, "get", fake_get)
    session = _make_session(monkeypatch, {"registration": {"drivers": {"adspower": {}}}})
    result = session._api_get("api/v1/browser/start", {"user_id": "abc"})
    assert result["code"] == 0
    assert captured["params"]["user_id"] == "abc"
    assert captured["url"].endswith("/api/v1/browser/start")


def test_api_get_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(ext.curl_requests, "get", lambda *a, **k: _Resp({"code": 0}, status=500))
    session = _make_session(monkeypatch, {"registration": {"drivers": {"adspower": {}}}})
    with pytest.raises(BrowserRegistrationError) as exc:
        session._api_get("api/v1/browser/start", {"user_id": "abc"})
    assert "adspower_api_error" in str(exc.value)


def test_api_get_raises_on_nonzero_code(monkeypatch):
    monkeypatch.setattr(ext.curl_requests, "get", lambda *a, **k: _Resp({"code": 1, "msg": "user not found"}))
    session = _make_session(monkeypatch, {"registration": {"drivers": {"adspower": {}}}})
    with pytest.raises(BrowserRegistrationError) as exc:
        session._api_get("api/v1/browser/start", {"user_id": "abc"})
    assert "user not found" in str(exc.value)


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def test_enter_without_user_id_fails(monkeypatch, patched):
    session = _make_session(monkeypatch, {"registration": {"drivers": {"adspower": {}}}})
    with pytest.raises(BrowserRegistrationError) as exc:
        with session:
            pass
    assert "adspower_user_id_missing" in str(exc.value)


def test_enter_starts_and_connects_via_cdp(monkeypatch, patched):
    connected = {}

    def fake_get(url, params=None, timeout=0):
        return _Resp({"code": 0, "data": {"ws": {"playwright": "ws://127.0.0.1:5500/devtools/browser/abc"}}})

    monkeypatch.setattr(ext.curl_requests, "get", fake_get)
    monkeypatch.setattr(
        _StubPW.chromium, "connect_over_cdp",
        staticmethod(lambda addr, timeout=0: connected.setdefault("addr", addr) or _StubBrowser()),
    )

    config = {"registration": {"drivers": {"adspower": {"user_id": "env-1", "headless": True}}}}
    session = _make_session(monkeypatch, config)
    with session:
        assert connected["addr"] == "ws://127.0.0.1:5500/devtools/browser/abc"
        assert session.user_id == "env-1"
        assert session.debugger_address == "ws://127.0.0.1:5500/devtools/browser/abc"
    # keep_browser_open defaults to False so close() must stop the environment.
    assert session.user_id == ""


def test_enter_falls_back_to_debugger_address(monkeypatch, patched):
    def fake_get(url, params=None, timeout=0):
        return _Resp({"code": 0, "data": {"debuggerAddress": "127.0.0.1:5505"}})

    monkeypatch.setattr(ext.curl_requests, "get", fake_get)
    monkeypatch.setattr(
        _StubPW.chromium, "connect_over_cdp",
        staticmethod(lambda addr, timeout=0: _StubBrowser()),
    )
    session = _make_session(
        monkeypatch, {"registration": {"drivers": {"adspower": {"user_id": "env-2"}}}}
    )
    with session:
        assert session.debugger_address == "http://127.0.0.1:5505"


def test_enter_missing_debug_address_raises(monkeypatch, patched):
    monkeypatch.setattr(ext.curl_requests, "get", lambda *a, **k: _Resp({"code": 0, "data": {}}))
    session = _make_session(
        monkeypatch, {"registration": {"drivers": {"adspower": {"user_id": "env-3"}}}}
    )
    with pytest.raises(BrowserRegistrationError) as exc:
        with session:
            pass
    assert "adspower_debug_address_missing" in str(exc.value)


def test_close_calls_stop_endpoint(monkeypatch, patched):
    calls = []

    def fake_get(url, params=None, timeout=0):
        calls.append((url, params))
        return _Resp({"code": 0, "data": {"ws": {"playwright": "ws://127.0.0.1:5500/b"}}})

    monkeypatch.setattr(ext.curl_requests, "get", fake_get)
    monkeypatch.setattr(_StubPW.chromium, "connect_over_cdp", staticmethod(lambda addr, timeout=0: _StubBrowser()))
    session = _make_session(
        monkeypatch, {"registration": {"drivers": {"adspower": {"user_id": "env-9"}}}}
    )
    with session:
        pass
    stop_calls = [c for c in calls if c[0].endswith("/api/v1/browser/stop")]
    assert stop_calls, "stop endpoint should be called on close"
    assert stop_calls[0][1]["user_id"] == "env-9"


def test_close_skips_stop_when_keep_open(monkeypatch, patched):
    calls = []

    def fake_get(url, params=None, timeout=0):
        calls.append(url)
        return _Resp({"code": 0, "data": {"ws": {"playwright": "ws://127.0.0.1:5500/b"}}})

    monkeypatch.setattr(ext.curl_requests, "get", fake_get)
    monkeypatch.setattr(_StubPW.chromium, "connect_over_cdp", staticmethod(lambda addr, timeout=0: _StubBrowser()))
    session = _make_session(
        monkeypatch, {"registration": {"drivers": {"adspower": {"user_id": "env-9", "keep_browser_open": True}}}}
    )
    with session:
        pass
    assert not any(c.endswith("/api/v1/browser/stop") for c in calls)
