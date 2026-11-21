"""Tests for callback interstitial resolution and registration_disallowed reauthorize (P0-3)."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sms_tool.transport import http_utils
from sms_tool.registration.handlers import RegistrationEmailWorkflow


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", url="https://auth.test/ok", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.url = url
        self.headers = dict(headers or {})

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Recorder:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, session, method, url, *, label="", **kwargs):
        self.calls.append({"method": method, "url": url, "label": label, **kwargs})
        return self._responses.pop(0)


class _Session:
    def __init__(self, cookies=None):
        self.cookies = SimpleNamespace(get_dict=lambda: dict(cookies or {}))


def _cfg():
    return {"chatgpt": {"auth_base_url": "https://auth.test", "chat_base_url": "https://chat.test"}}


def test_follow_continue_resolves_choose_an_account_single_session():
    rec = _Recorder(
        _FakeResponse(200, text="<div>us_1234567890abcdef12</div>", url="https://auth.test/choose-an-account"),
        _FakeResponse(200, payload={"continue_url": "https://chat.test/api/auth/callback/openai?code=abc"}),
        _FakeResponse(200, text="ok", url="https://chat.test/"),
    )
    with (
        patch("sms_tool.transport.http_utils.CFG", _cfg()),
        patch("sms_tool.transport.http_utils.auth_impersonate", lambda: "chrome146"),
        patch("sms_tool.transport.http_utils.request_with_retry", rec),
    ):
        resp = http_utils._follow_continue_url(_Session(), "/authorize/continue", {"X-Base": "1"}, label="Create")

    assert resp is not None and resp.url == "https://chat.test/"
    assert [(c["method"], c["url"]) for c in rec.calls] == [
        ("get", "https://auth.test/authorize/continue"),
        ("post", "https://auth.test/api/accounts/session/select"),
        ("get", "https://chat.test/api/auth/callback/openai?code=abc"),
    ]
    assert rec.calls[1]["json"] == {"session_id": "us_1234567890abcdef12"}


def test_follow_continue_choose_an_account_falls_back_to_form_on_404():
    rec = _Recorder(
        _FakeResponse(200, text="<span>us_abcdef1234567890ab</span>", url="https://auth.test/choose-an-account"),
        _FakeResponse(404, payload={}),
        _FakeResponse(303, headers={"Location": "https://chat.test/api/auth/callback/openai?code=ok"}),
        _FakeResponse(200, text="done", url="https://chat.test/"),
    )
    with (
        patch("sms_tool.transport.http_utils.CFG", _cfg()),
        patch("sms_tool.transport.http_utils.auth_impersonate", lambda: "chrome146"),
        patch("sms_tool.transport.http_utils.request_with_retry", rec),
    ):
        resp = http_utils._follow_continue_url(_Session(), "/start", {}, label="Create")

    assert resp is not None and resp.url == "https://chat.test/"
    assert rec.calls[2]["url"] == "https://auth.test/choose-an-account"
    assert "intent=select" in rec.calls[2]["data"]
    assert "us_abcdef1234567890ab" in rec.calls[2]["data"]


def test_follow_continue_refuses_ambiguous_choose_an_account():
    rec = _Recorder(
        _FakeResponse(
            200,
            text="us_111111111111111111 and us_222222222222222222",
            url="https://auth.test/choose-an-account",
        ),
    )
    with (
        patch("sms_tool.transport.http_utils.CFG", _cfg()),
        patch("sms_tool.transport.http_utils.auth_impersonate", lambda: "chrome146"),
        patch("sms_tool.transport.http_utils.request_with_retry", rec),
    ):
        resp = http_utils._follow_continue_url(_Session(), "/start", {}, label="Create")

    assert resp is not None and resp.url == "https://auth.test/choose-an-account"
    assert len(rec.calls) == 1


def test_follow_continue_resolves_workspace_from_cookie_and_rejects_external_host():
    cookie_payload = (
        base64.urlsafe_b64encode(json.dumps({"workspaces": [{"id": "ws-99"}]}).encode()).decode().rstrip("=")
    )
    rec = _Recorder(
        _FakeResponse(200, text="<html>workspace</html>", url="https://auth.test/workspace"),
        _FakeResponse(200, payload={"continue_url": "https://chat.test/api/auth/callback/openai?code=w"}),
        _FakeResponse(200, text="ok", url="https://chat.test/"),
    )
    with (
        patch("sms_tool.transport.http_utils.CFG", _cfg()),
        patch("sms_tool.transport.http_utils.auth_impersonate", lambda: "chrome146"),
        patch("sms_tool.transport.http_utils.request_with_retry", rec),
    ):
        resp = http_utils._follow_continue_url(
            _Session({"oai-client-auth-session": cookie_payload}),
            "/start",
            {},
            label="Create",
        )
    assert resp is not None and resp.url == "https://chat.test/"
    assert rec.calls[1]["url"] == "https://auth.test/api/accounts/workspace/select"
    assert rec.calls[1]["json"] == {"workspace_id": "ws-99"}

    # Off-host redirect must be refused without making a GET to evil.example.
    rec_evil = _Recorder(
        _FakeResponse(200, text='{"workspace_id":"ws-1"}', url="https://auth.test/consent"),
        _FakeResponse(200, payload={"continue_url": "https://evil.example/steal"}),
    )
    with (
        patch("sms_tool.transport.http_utils.CFG", _cfg()),
        patch("sms_tool.transport.http_utils.auth_impersonate", lambda: "chrome146"),
        patch("sms_tool.transport.http_utils.request_with_retry", rec_evil),
    ):
        resp2 = http_utils._follow_continue_url(_Session(), "/start", {}, label="Create")
    assert resp2 is not None and resp2.url == "https://auth.test/consent"
    assert len(rec_evil.calls) == 2


def test_fetch_auth_session_reauthorizes_once_on_registration_disallowed():
    state = SimpleNamespace(
        session=_Session(),
        auth_base="https://auth.test",
        chat_base="https://chat.test",
        base_headers={"X-Base": "1"},
        device_id="did-1",
        session_logging_id="sid-1",
        csrf_token="",
        username="user@example.com",
        create_ok=False,
        create_data={"error": {"code": "registration_disallowed"}},
        existing_account=False,
        access_token="",
        auth_session={},
        auth_body={},
    )
    w = object.__new__(RegistrationEmailWorkflow)
    w.config = {}
    object.__setattr__(w, "runtime", state)
    w._persist_checkpoint = Mock()

    followed_urls: list[str] = []
    fetch_sessions = [
        {"status": 200, "body": {}},
        {"status": 200, "body": {"accessToken": "recovered-at"}},
    ]
    rec = _Recorder(
        _FakeResponse(200, payload={"csrfToken": "csrf-123"}),
        _FakeResponse(200, payload={"url": "https://auth.test/authorize?client_id=abc&prompt=login&state=s1"}),
    )
    ops = SimpleNamespace(
        _fetch_auth_session=Mock(side_effect=fetch_sessions),
        _auth_session_access_token=lambda body: str((body or {}).get("accessToken") or ""),
        nextauth_headers=Mock(return_value={"X-NextAuth": "1"}),
        request_with_retry=rec,
        _json_or_raw=lambda resp, limit=500: resp.json(),
        auth_impersonate=Mock(return_value="chrome146"),
        _follow_continue_url=Mock(side_effect=lambda _s, url, *_a, **_kw: followed_urls.append(url)),
    )
    object.__setattr__(w, "_operations", ops)

    w.fetch_auth_session()

    assert state.access_token == "recovered-at"
    assert state.create_ok is True
    assert len(followed_urls) == 1
    assert "prompt=" not in followed_urls[0]
    assert "client_id=abc" in followed_urls[0]
    assert "state=s1" in followed_urls[0]
    assert rec.calls[0]["url"] == "https://chat.test/api/auth/csrf"
    assert rec.calls[1]["url"].startswith("https://chat.test/api/auth/signin/openai?login_hint=")


def test_fetch_auth_session_does_not_reauthorize_on_other_create_errors():
    state = SimpleNamespace(
        session=_Session(),
        auth_base="https://auth.test",
        chat_base="https://chat.test",
        base_headers={},
        device_id="did-1",
        session_logging_id="sid-1",
        csrf_token="",
        username="user@example.com",
        create_ok=False,
        create_data={"error": {"code": "invalid_auth_step"}},
        existing_account=False,
        access_token="",
        auth_session={},
        auth_body={},
    )
    w = object.__new__(RegistrationEmailWorkflow)
    w.config = {}
    object.__setattr__(w, "runtime", state)
    w._persist_checkpoint = Mock()
    ops = SimpleNamespace(
        _fetch_auth_session=Mock(return_value={"status": 200, "body": {}}),
        _auth_session_access_token=lambda body: "",
        nextauth_headers=Mock(),
        request_with_retry=Mock(),
        _json_or_raw=Mock(),
        auth_impersonate=Mock(return_value="chrome146"),
        _follow_continue_url=Mock(),
    )
    object.__setattr__(w, "_operations", ops)

    w.fetch_auth_session()

    ops.request_with_retry.assert_not_called()
    ops._follow_continue_url.assert_not_called()
    assert state.create_ok is False
