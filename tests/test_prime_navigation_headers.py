"""P1-A: the password-page prime's navigation headers (``sec-fetch-site`` / ``sec-fetch-user``).

``_prime_create_account_password_page`` follows the password URL through
``http_utils._follow_continue_url``, which sends only ``Accept`` + ``Referer``.
A real top-level navigation also sends ``Sec-Fetch-Site: same-origin`` and
``Sec-Fetch-User: ?1`` -- which is what turb's ``navigate_create_account_password``
sends (and it treats a wrong landing as fatal).  So P1-4's conclusion ("the prime
landed correctly, yet the transaction stayed passwordless") rests on the
*landing*, not on a navigation the server recognised as top-level; the header
gap is an unexcluded explanation.

``registration.prime_navigation_headers`` (default **False**) is a **separate**
toggle from ``prime_create_account_password`` so the P1-4 arm stays reproducible
and the comparison stays single-variable (``prime`` on in both arms).

``test_frozen_section_is_read`` pins the 2026-10-07 P1-D class for this new
predicate: production sections are ``mappingproxy``, not ``dict``.
"""

from __future__ import annotations

from types import MappingProxyType
from unittest.mock import Mock

import pytest

from sms_tool import auth_flow


def _response(status=200, url="https://auth.openai.com/create-account/password"):
    response = Mock()
    response.status_code = status
    response.url = url
    response.headers = {}
    return response


class _FakeFollow:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def __call__(self, session, url, headers, **kwargs):
        self.calls.append({"url": url, "headers": headers, **kwargs})
        return self.response


def test_the_prime_sends_navigation_headers_only_when_its_toggle_is_on(monkeypatch):
    follow = _FakeFollow(_response())
    monkeypatch.setattr(auth_flow.deps, "_follow_continue_url", follow)

    monkeypatch.setattr(auth_flow.steps, "_prime_navigation_headers_enabled", lambda: False)
    auth_flow._prime_create_account_password_page(
        object(),
        "https://auth.openai.com",
        {"X-Base": "1"},
        "https://auth.openai.com/email-verification",
    )
    assert "sec-fetch-site" not in follow.calls[-1]["headers"]
    assert "sec-fetch-user" not in follow.calls[-1]["headers"]

    monkeypatch.setattr(auth_flow.steps, "_prime_navigation_headers_enabled", lambda: True)
    auth_flow._prime_create_account_password_page(
        object(),
        "https://auth.openai.com",
        {"X-Base": "1"},
        "https://auth.openai.com/email-verification",
    )
    assert follow.calls[-1]["headers"]["sec-fetch-site"] == "same-origin"
    assert follow.calls[-1]["headers"]["sec-fetch-user"] == "?1"
    # Base headers are preserved, not replaced.
    assert follow.calls[-1]["headers"]["X-Base"] == "1"


def test_the_navigation_headers_marker_is_emitted_only_inside_the_branch(monkeypatch):
    """P1-9's mechanism line must live inside the toggle's own branch."""
    from sms_tool.auth_flow import signup as signup_module

    follow = _FakeFollow(_response())
    monkeypatch.setattr(auth_flow.deps, "_follow_continue_url", follow)
    emit = Mock()
    monkeypatch.setattr(signup_module, "_emit_operator_line", emit)

    monkeypatch.setattr(auth_flow.steps, "_prime_navigation_headers_enabled", lambda: False)
    auth_flow._prime_create_account_password_page(
        object(),
        "https://auth.openai.com",
        {},
        "https://auth.openai.com/email-verification",
    )
    emit.assert_not_called()

    monkeypatch.setattr(auth_flow.steps, "_prime_navigation_headers_enabled", lambda: True)
    auth_flow._prime_create_account_password_page(
        object(),
        "https://auth.openai.com",
        {},
        "https://auth.openai.com/email-verification",
    )
    emit.assert_called_once()
    assert "Password page navigation headers" in emit.call_args.args[1]


def test_frozen_section_is_read():
    """``current_config_data()`` freezes the section into a ``mappingproxy``."""
    frozen = MappingProxyType({"registration": MappingProxyType({"prime_navigation_headers": True})})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(auth_flow.deps, "current_config_data", lambda *a, **kw: frozen)
        assert auth_flow.steps._prime_navigation_headers_enabled() is True


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
def test_the_navigation_headers_switch_accepts_only_truthy_values(monkeypatch, value, expected):
    monkeypatch.setattr(
        auth_flow.deps,
        "current_config_data",
        lambda *a, **kw: {"registration": {"prime_navigation_headers": value}},
    )
    assert auth_flow.steps._prime_navigation_headers_enabled() is expected


def test_the_navigation_headers_switch_is_off_when_the_section_is_absent(monkeypatch):
    monkeypatch.setattr(auth_flow.deps, "current_config_data", lambda *a, **kw: {})
    assert auth_flow.steps._prime_navigation_headers_enabled() is False
