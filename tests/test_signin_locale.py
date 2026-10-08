"""P1-12: the password lane's signin ``locale`` (absent vs ``ja-JP``).

SunnyRegister's ``_start_next_auth`` puts ``locale: "ja-JP"`` in the
``/api/auth/signin/openai`` query, alongside ``prompt=login`` and
``screen_hint=signup``; this repo's ``_openai_signin_url`` declares no locale.
It is the only reference that declares one, and it is the last field of the
signin shape.

``registration.signin_locale_ja_jp`` (default **False**) adds **only** the
``locale`` query parameter, on the password lane, uniformly across the attempts.
It is a boolean (not a locale string) so the A/B harness's mechanism gate can
tell which arm the marker belongs to -- a string arm value would set
``mechanism_ok=null``.

This file pins:
1. Off declares no locale on any attempt.
2. On declares ``ja-JP`` on every attempt and changes nothing else.
3. ``_openai_signin_url`` / ``_ensure_authorize_context`` only add ``locale``
   when a non-empty value is passed (backward compatible).
4. The predicate reads a **frozen** section (the 2026-10-07 P1-D class).
5. The mechanism line is emitted only on the password lane with the toggle on,
   and is distinct from the other signin markers.
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from sms_tool import auth_flow
from sms_tool.auth_flow import signup as signup_module

_EMAIL_VERIFICATION = "https://auth.openai.com/email-verification"
_MARKER = "Signin locale=ja-JP"


def _patch_shapes(monkeypatch, *, locale_ja_jp):
    monkeypatch.setattr(auth_flow.steps, "_signin_prompt_login_enabled", lambda: False)
    monkeypatch.setattr(auth_flow.steps, "_signin_screen_hint_login_or_signup_enabled", lambda: False)
    monkeypatch.setattr(auth_flow.steps, "_signin_locale_ja_jp_enabled", lambda: locale_ja_jp)


def test_toggle_off_declares_no_locale(monkeypatch):
    _patch_shapes(monkeypatch, locale_ja_jp=False)
    attempts = auth_flow._signup_signin_attempts()
    assert [a["locale"] for a in attempts] == ["", "", ""]


def test_toggle_on_declares_ja_jp_on_every_attempt_and_nothing_else(monkeypatch):
    _patch_shapes(monkeypatch, locale_ja_jp=True)
    attempts = auth_flow._signup_signin_attempts()
    assert [a["locale"] for a in attempts] == ["ja-JP", "ja-JP", "ja-JP"]
    # The other fields are unchanged: the toggle owns locale only.
    assert attempts[0]["name"] == "signup_screen_hint"
    assert attempts[0]["screen_hint"] == "signup"
    assert attempts[0]["prompt"] == ""


def test_the_signin_url_carries_locale_only_when_passed():
    base = auth_flow.steps._openai_signin_url("https://chatgpt.com", "did", "log", "a@b.com")
    assert "locale" not in parse_qs(urlparse(base).query)
    with_locale = auth_flow.steps._openai_signin_url("https://chatgpt.com", "did", "log", "a@b.com", locale="ja-JP")
    assert parse_qs(urlparse(with_locale).query)["locale"] == ["ja-JP"]


def test_the_authorize_url_carries_locale_only_when_passed():
    url = "https://auth.openai.com/api/accounts/authorize?state=s1"
    plain = auth_flow.steps._ensure_authorize_context(url, "did", "log", "a@b.com")
    assert "locale" not in parse_qs(urlparse(plain).query)
    with_locale = auth_flow.steps._ensure_authorize_context(url, "did", "log", "a@b.com", locale="ja-JP")
    assert parse_qs(urlparse(with_locale).query)["locale"] == ["ja-JP"]


def test_frozen_section_is_read():
    """``current_config_data()`` freezes the section into a ``mappingproxy``."""
    frozen = MappingProxyType({"registration": MappingProxyType({"signin_locale_ja_jp": True})})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_flow.deps, "current_config_data", lambda *a, **kw: frozen)
        assert auth_flow.steps._signin_locale_ja_jp_enabled() is True


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (1, True),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        (False, False),
        (0, False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        (None, False),
        ("", False),
        ("maybe", False),
    ],
)
def test_the_switch_accepts_only_truthy_values(monkeypatch, value, expected):
    monkeypatch.setattr(
        auth_flow.deps,
        "current_config_data",
        lambda *a, **kw: {"registration": {"signin_locale_ja_jp": value}},
    )
    assert auth_flow.steps._signin_locale_ja_jp_enabled() is expected


def test_the_switch_is_off_when_the_section_is_absent(monkeypatch):
    monkeypatch.setattr(auth_flow.deps, "current_config_data", lambda *a, **kw: {})
    assert auth_flow.steps._signin_locale_ja_jp_enabled() is False


def _prepare(*, locale_ja_jp, passwordless_web=False):
    """Run ``_prepare_signup_auth_state`` with the signin/authorize transport mocked."""
    signin_response = Mock(status_code=200, url="https://chatgpt.com/api/auth/signin/openai")
    signin_response.json.return_value = {"url": "https://auth.openai.com/api/accounts/authorize?state=state-1"}
    signin_response.headers = {}
    authorize_response = Mock(status_code=302, url=_EMAIL_VERIFICATION)
    authorize_response.headers = {"location": _EMAIL_VERIFICATION}
    session = Mock()
    session.post.return_value = signin_response
    session.get.return_value = authorize_response
    with (
        patch.object(auth_flow.steps, "_signin_locale_ja_jp_enabled", lambda: locale_ja_jp),
        patch.object(auth_flow.steps, "_signin_prompt_login_enabled", lambda: False),
        patch.object(auth_flow.steps, "_signin_screen_hint_login_or_signup_enabled", lambda: False),
        patch.object(signup_module, "_emit_operator_line") as emit,
    ):
        state = auth_flow._prepare_signup_auth_state(
            session,
            "user@example.com",
            "did-1",
            "logging-1",
            "https://auth.openai.com",
            "https://chatgpt.com",
            {},
            "csrf",
            passwordless_web=passwordless_web,
            attempts=({"name": "signup_screen_hint", "screen_hint": "signup", "prompt": "", "locale": ""},),
        )
    return state, emit


def test_the_mechanism_line_is_emitted_on_the_password_lane():
    _state, emit = _prepare(locale_ja_jp=True)
    lines = [c.args[1] for c in emit.call_args_list]
    assert len(lines) == 1
    assert _MARKER in lines[0]


def test_no_mechanism_line_when_the_toggle_is_off():
    _state, emit = _prepare(locale_ja_jp=False)
    emit.assert_not_called()


def test_no_mechanism_line_on_the_passwordless_lane():
    _state, emit = _prepare(locale_ja_jp=True, passwordless_web=True)
    emit.assert_not_called()
