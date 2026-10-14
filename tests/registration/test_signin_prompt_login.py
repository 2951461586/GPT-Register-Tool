"""P1-11: the password lane's signin ``prompt`` (``""`` vs ``login``).

turb's ``signin_openai`` sends ``prompt=login`` *and* ``screen_hint=login_or_signup``.
Four 2026-10-07/08 live runs -- lajiao/fireside pools, burned/brand-new mailboxes,
``signup``/``login_or_signup``/a re-declared continue -- all landed on
``email_verification_mode=passwordless_signup`` with
``passwordless_signup_from_default_redirect=true`` and a non-dispatching OTP.
The declared screen *is* recorded (``original_screen_hint`` follows it) but does
not pick the transaction arm, so ``prompt`` is the next untested field.

``registration.signin_prompt_login`` (default **False**) changes **only** the
first attempt's ``prompt``.  The ``p1-11`` comparison holds
``signin_screen_hint_login_or_signup=true`` in both arms, so the pair is
``login_or_signup`` vs ``login_or_signup + prompt=login`` (turb's shape as the
treatment).

This file pins:
1. Off keeps the first attempt's ``prompt`` empty (the P1-10 names included).
2. On sets ``prompt=login`` on the first attempt only; fallbacks unchanged.
3. Both toggles together produce turb's exact first attempt.
4. The predicate reads a **frozen** section (the 2026-10-07 P1-D class).
5. The mechanism line is emitted only on the password lane with the toggle on,
   and is distinct from P1-10's.
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import Mock, patch

import pytest

from sms_tool import auth_flow
from sms_tool.auth_flow import signup as signup_module

_EMAIL_VERIFICATION = "https://auth.openai.com/email-verification"
_MARKER = "Signin prompt=login"
_P1_10_MARKER = "Signin screen_hint=login_or_signup"


def _patch_shapes(monkeypatch, *, prompt_login, screen_hint_login_or_signup, locale_ja_jp=False):
    monkeypatch.setattr(auth_flow.steps, "_signin_prompt_login_enabled", lambda: prompt_login)
    monkeypatch.setattr(
        auth_flow.steps, "_signin_screen_hint_login_or_signup_enabled", lambda: screen_hint_login_or_signup
    )
    monkeypatch.setattr(auth_flow.steps, "_signin_locale_ja_jp_enabled", lambda: locale_ja_jp)


def test_toggle_off_keeps_the_prompt_empty(monkeypatch):
    _patch_shapes(monkeypatch, prompt_login=False, screen_hint_login_or_signup=False)
    attempts = auth_flow._signup_signin_attempts()
    assert attempts[0] == {
        "name": "signup_screen_hint",
        "screen_hint": "signup",
        "prompt": "",
        "locale": "",
    }


def test_toggle_on_sets_prompt_login_on_the_first_attempt_only(monkeypatch):
    _patch_shapes(monkeypatch, prompt_login=True, screen_hint_login_or_signup=False)
    attempts = auth_flow._signup_signin_attempts()
    assert attempts[0] == {
        "name": "signup_prompt_login",
        "screen_hint": "signup",
        "prompt": "login",
        "locale": "",
    }
    # Fallbacks are untouched.
    assert attempts[1] == {
        "name": "signup_prompt_signup",
        "screen_hint": "signup",
        "prompt": "signup",
        "locale": "",
    }
    assert attempts[2] == {
        "name": "signup_legacy_prompt_login",
        "screen_hint": "signup",
        "prompt": "login",
        "locale": "",
    }


def test_both_toggles_produce_turbs_first_attempt(monkeypatch):
    """turb pairs ``prompt=login`` with ``screen_hint=login_or_signup``."""
    _patch_shapes(monkeypatch, prompt_login=True, screen_hint_login_or_signup=True)
    attempts = auth_flow._signup_signin_attempts()
    assert attempts[0] == {
        "name": "login_or_signup_prompt_login",
        "screen_hint": "login_or_signup",
        "prompt": "login",
        "locale": "",
    }


def test_p1_10_alone_keeps_the_prompt_empty(monkeypatch):
    _patch_shapes(monkeypatch, prompt_login=False, screen_hint_login_or_signup=True)
    attempts = auth_flow._signup_signin_attempts()
    assert attempts[0] == {
        "name": "login_or_signup_screen_hint",
        "screen_hint": "login_or_signup",
        "prompt": "",
        "locale": "",
    }


def test_frozen_section_is_read():
    """``current_config_data()`` freezes the section into a ``mappingproxy``."""
    frozen = MappingProxyType({"registration": MappingProxyType({"signin_prompt_login": True})})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_flow.deps, "current_config_data", lambda *a, **kw: frozen)
        assert auth_flow.steps._signin_prompt_login_enabled() is True


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
        lambda *a, **kw: {"registration": {"signin_prompt_login": value}},
    )
    assert auth_flow.steps._signin_prompt_login_enabled() is expected


def test_the_switch_is_off_when_the_section_is_absent(monkeypatch):
    monkeypatch.setattr(auth_flow.deps, "current_config_data", lambda *a, **kw: {})
    assert auth_flow.steps._signin_prompt_login_enabled() is False


def _prepare(*, prompt_login, screen_hint_login_or_signup=False, passwordless_web=False):
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
        patch.object(auth_flow.steps, "_signin_prompt_login_enabled", lambda: prompt_login),
        patch.object(
            auth_flow.steps,
            "_signin_screen_hint_login_or_signup_enabled",
            lambda: screen_hint_login_or_signup,
        ),
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
    _state, emit = _prepare(prompt_login=True)
    lines = [c.args[1] for c in emit.call_args_list]
    assert len(lines) == 1
    assert _MARKER in lines[0]


def test_no_mechanism_line_when_the_toggle_is_off():
    _state, emit = _prepare(prompt_login=False)
    emit.assert_not_called()


def test_no_mechanism_line_on_the_passwordless_lane():
    _state, emit = _prepare(prompt_login=True, passwordless_web=True)
    emit.assert_not_called()


def test_the_two_signin_markers_are_both_emitted_when_both_toggles_are_on():
    """The p1-10 and p1-11 lines are distinct, so both can be read independently."""
    _state, emit = _prepare(prompt_login=True, screen_hint_login_or_signup=True)
    lines = [c.args[1] for c in emit.call_args_list]
    assert any(_MARKER in line for line in lines)
    assert any(_P1_10_MARKER in line for line in lines)
    assert _MARKER != _P1_10_MARKER
