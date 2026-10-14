"""P1-10: the password lane's signin ``screen_hint`` (``signup`` vs ``login_or_signup``).

The password lane's first signin attempt declares ``screen_hint=signup``.  Two
2026-10-07/08 live runs -- the P1-8 hint arm on the lajiao pool and the default
arm on the fireside pool, with different ReMail iCloud mailboxes -- produced the
**same** transaction arm (``email_verification_mode=passwordless_signup``,
``passwordless_signup_from_default_redirect=true``) and the same
``email_otp_send_stuck``.  Exit, mailbox and the declared continue screen are
therefore excluded, which leaves the signin shape.

``registration.signin_screen_hint_login_or_signup`` (default **False**) changes
**only** the first attempt's ``screen_hint``, so the A/B keeps the lane, the exit
pool and the ``authorize/continue`` behaviour constant.

This file pins:
1. Off keeps the current attempt shape byte-for-byte (the fallbacks included).
2. On switches the first attempt only; the fallbacks keep ``signup``.
3. The predicate reads a **frozen** section (the 2026-10-07 P1-D class).
4. The mechanism line is emitted only on the password lane with the toggle on.
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import Mock, patch

import pytest

from sms_tool import auth_flow
from sms_tool.auth_flow import signup as signup_module

_EMAIL_VERIFICATION = "https://auth.openai.com/email-verification"
_MARKER = "Signin screen_hint=login_or_signup"


def test_toggle_off_keeps_the_first_attempt_signup(monkeypatch):
    monkeypatch.setattr(auth_flow.steps, "_signin_screen_hint_login_or_signup_enabled", lambda: False)
    monkeypatch.setattr(auth_flow.steps, "_signin_locale_ja_jp_enabled", lambda: False)
    attempts = auth_flow._signup_signin_attempts()
    assert attempts[0] == {
        "name": "signup_screen_hint",
        "screen_hint": "signup",
        "prompt": "",
        "locale": "",
    }
    assert [a["screen_hint"] for a in attempts] == ["signup", "signup", "signup"]


def test_toggle_on_switches_only_the_first_attempt(monkeypatch):
    monkeypatch.setattr(auth_flow.steps, "_signin_screen_hint_login_or_signup_enabled", lambda: True)
    monkeypatch.setattr(auth_flow.steps, "_signin_locale_ja_jp_enabled", lambda: False)
    attempts = auth_flow._signup_signin_attempts()
    assert attempts[0] == {
        "name": "login_or_signup_screen_hint",
        "screen_hint": "login_or_signup",
        "prompt": "",
        "locale": "",
    }
    # Fallbacks are untouched: they are only reached when the first shape does
    # not land on a recognised step, and changing them would add a second
    # variable to the comparison.
    assert attempts[1]["screen_hint"] == "signup"
    assert attempts[2]["screen_hint"] == "signup"


def test_frozen_section_is_read():
    """``current_config_data()`` freezes the section into a ``mappingproxy``."""
    frozen = MappingProxyType({"registration": MappingProxyType({"signin_screen_hint_login_or_signup": True})})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_flow.deps, "current_config_data", lambda *a, **kw: frozen)
        assert auth_flow.steps._signin_screen_hint_login_or_signup_enabled() is True


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
        lambda *a, **kw: {"registration": {"signin_screen_hint_login_or_signup": value}},
    )
    assert auth_flow.steps._signin_screen_hint_login_or_signup_enabled() is expected


def test_the_switch_is_off_when_the_section_is_absent(monkeypatch):
    monkeypatch.setattr(auth_flow.deps, "current_config_data", lambda *a, **kw: {})
    assert auth_flow.steps._signin_screen_hint_login_or_signup_enabled() is False


def _prepare(*, enabled, passwordless_web=False):
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
        patch.object(auth_flow.steps, "_signin_screen_hint_login_or_signup_enabled", lambda: enabled),
        patch.object(auth_flow.steps, "_signin_prompt_login_enabled", lambda: False),
        patch.object(auth_flow.steps, "_signin_locale_ja_jp_enabled", lambda: False),
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
            attempts=({"name": "signup_screen_hint", "screen_hint": "signup", "prompt": ""},),
        )
    return state, emit


def test_the_mechanism_line_is_emitted_on_the_password_lane():
    _state, emit = _prepare(enabled=True)
    emit.assert_called_once()
    assert _MARKER in emit.call_args.args[1]


def test_no_mechanism_line_when_the_toggle_is_off():
    _state, emit = _prepare(enabled=False)
    emit.assert_not_called()


def test_no_mechanism_line_on_the_passwordless_lane():
    """The passwordless lane sends ``login_or_signup`` too; it must not satisfy P1-10."""
    _state, emit = _prepare(enabled=True, passwordless_web=True)
    emit.assert_not_called()
