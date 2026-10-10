"""Frozen-config regression: ``isinstance(section, Mapping)``, never ``dict``.

``config._freeze`` turns every config mapping into a ``MappingProxyType``, which
is a ``Mapping`` but **not** a ``dict``. A ``dict`` guard therefore reads as
"section missing" in production while plain-``dict`` test fixtures keep it green
-- the 2026-10-07 P1-D class (three registration toggles silently disabled, then
the smailr section silently empty, ``email_registration.sentinel_version``
ignored, and the probe's ``chatgpt`` base URLs ignored).

These tests pass the section the way production does -- **frozen** -- so the
class cannot come back through a patch that hands the function a plain dict.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from types import MappingProxyType
from unittest.mock import Mock, patch

from sms_tool import config as config_module
from sms_tool import registration_probe
from sms_tool.providers import mailbox_smailr
from sms_tool.sentinel import bundle


def _frozen_email_registration() -> MappingProxyType:
    return MappingProxyType(
        {
            "smailr": MappingProxyType(
                {
                    "api_key": "cfg-key",
                    "base_url": "https://smailr.example.test/api/v1",
                    "default_domain": "nodeloc.cc",
                    "domain_ids": MappingProxyType({"nodeloc.cc": "domain-uuid-1"}),
                }
            ),
            "sentinel_version": "20990101frozen",
        }
    )


def test_frozen_sections_are_mapping_but_not_dict():
    """Pin the premise the whole file rests on."""
    frozen = config_module._freeze({"a": {"b": 1}, "list": [{"c": 2}]})
    assert isinstance(frozen["a"], MappingProxyType)
    assert isinstance(frozen["a"], Mapping)
    assert not isinstance(frozen["a"], dict)
    assert frozen["list"] == (MappingProxyType({"c": 2}),)


def test_smailr_section_survives_freezing():
    """``_smailr_cfg`` must read a frozen section, not just a plain dict.

    Before the fix this returned ``{}`` against ``current_config_data()``: the
    configured api_key / base_url / default_domain / domain_ids were all
    replaced by env-or-defaults (``default_domain`` fell back to ``smailr.com``
    although the config said ``nodeloc.cc``).
    """
    config = MappingProxyType({"email_registration": _frozen_email_registration()})
    with (
        patch.object(mailbox_smailr, "current_config_data", return_value=config),
        patch.dict(os.environ, {"SMAILR_API_KEY": ""}),
    ):
        assert mailbox_smailr._smailr_cfg()["api_key"] == "cfg-key"
        assert mailbox_smailr._smailr_api_key() == "cfg-key"
        assert mailbox_smailr._smailr_default_domain() == "nodeloc.cc"
        assert mailbox_smailr._smailr_domain_id("nodeloc.cc") == "domain-uuid-1"


def test_sentinel_version_reads_a_frozen_email_section():
    """``email_registration.sentinel_version`` is a frozen section read."""
    config = MappingProxyType(
        {"email_registration": _frozen_email_registration(), "sentinel_version": "top-level-wins-not"}
    )
    with (
        patch.object(config_module, "current_config_data", return_value=config),
        patch.dict(os.environ, {"OPENAI_SENTINEL_VERSION": ""}),
    ):
        assert bundle.sentinel_version() == "20990101frozen"


def test_probe_reads_frozen_chatgpt_base_urls():
    """The default (``config=None``) branch of ``probe_registration`` is frozen.

    A ``dict`` guard dropped the configured base URLs and used the hard-coded
    endpoints, so a deployment pointing the probe at another host silently kept
    calling ``auth.openai.com``.
    """
    config = MappingProxyType(
        {
            "chatgpt": MappingProxyType(
                {
                    "auth_base_url": "https://auth.example.test",
                    "chat_base_url": "https://chat.example.test",
                }
            )
        }
    )
    landing = {"url": "https://auth.openai.com/log-in"}
    with (
        patch.object(registration_probe, "_run_read_only_handshake", return_value=landing) as handshake,
        patch.object(config_module, "current_config_data", return_value=config),
    ):
        result = registration_probe.probe_registration("probe@example.com", session=Mock())

    assert handshake.call_args.kwargs["auth_base"] == "https://auth.example.test"
    assert handshake.call_args.kwargs["chat_base"] == "https://chat.example.test"
    assert result["status"] == registration_probe.STATUS_REGISTERED


# ---------------------------------------------------------------------------
# The shared registration-toggle parser
# ---------------------------------------------------------------------------
#
# The eight ``registration.*`` toggles used to carry their own copy of the
# frozen-section guard, which is how the P1-D defect reached three of them at
# once (2026-10-07).  They now share ``steps._registration_flag``.  These tests
# pin the semantics that consolidation had to preserve exactly -- in particular
# that the toggles are deliberately **two-sided** about an unrecognised value.

# ``(toggle, key, default)`` for every switch the shared parser serves.
_REGISTRATION_TOGGLES = (
    ("_existing_login_continue_enabled", "existing_login_continue_on_verified_page", True),
    ("_signup_continue_screen_hint_enabled", "signup_continue_screen_hint", False),
    ("_signup_email_verification_continue_hint_enabled", "signup_email_verification_continue_hint", False),
    ("_prime_create_account_password_page_enabled", "prime_create_account_password", False),
    ("_prime_navigation_headers_enabled", "prime_navigation_headers", False),
    ("_signin_screen_hint_login_or_signup_enabled", "signin_screen_hint_login_or_signup", False),
    ("_signin_prompt_login_enabled", "signin_prompt_login", False),
    ("_signin_locale_ja_jp_enabled", "signin_locale_ja_jp", False),
)


def _read_toggle(name: str, section: object) -> bool:
    from sms_tool.auth_flow import deps, steps

    config = MappingProxyType({"registration": section})
    with patch.object(deps, "current_config_data", return_value=config):
        return getattr(steps, name)()


def test_registration_toggles_honour_a_frozen_section():
    """The P1-D class, now through the single shared guard."""
    for name, key, _default in _REGISTRATION_TOGGLES:
        assert _read_toggle(name, MappingProxyType({key: True})) is True, name
        assert _read_toggle(name, MappingProxyType({key: False})) is False, name
        # string forms a JSON/hand-edited config actually produces
        assert _read_toggle(name, MappingProxyType({key: "true"})) is True, name
        assert _read_toggle(name, MappingProxyType({key: "off"})) is False, name


def test_registration_toggles_fall_back_to_their_own_default():
    """A missing key, a non-mapping section and an exploding config all use ``default``.

    The default is not uniform: ``existing_login_continue_on_verified_page`` is
    default-**on**, every later toggle is default-**off**.  That is why the
    shared parser takes ``default`` rather than hard-coding one polarity.
    """
    from sms_tool.auth_flow import deps, steps

    for name, _key, default in _REGISTRATION_TOGGLES:
        assert _read_toggle(name, MappingProxyType({})) is default, name
        assert _read_toggle(name, None) is default, name
        assert _read_toggle(name, "not-a-mapping") is default, name

    def boom(*_args, **_kwargs):
        raise RuntimeError("config exploded")

    with patch.object(deps, "current_config_data", side_effect=boom):
        for name, _key, default in _REGISTRATION_TOGGLES:
            assert getattr(steps, name)() is default, name


def test_an_unrecognised_value_is_two_sided_on_purpose():
    """Unknown -> the toggle's own default, which differs by polarity.

    This is the exact behaviour the eight hand-written bodies had before the
    2026-10-10 consolidation (2488 differential checks, zero divergences).  A
    naive ``bool(value)`` rewrite would flip it: ``"maybe"`` would become truthy
    for the seven default-off toggles.
    """
    for name, key, default in _REGISTRATION_TOGGLES:
        section = MappingProxyType({key: "maybe"})
        assert _read_toggle(name, section) is default, name

    # and the two polarities really are different for the same input
    assert _read_toggle("_existing_login_continue_enabled", MappingProxyType({"x": "maybe"})) is True
    assert _read_toggle("_signin_prompt_login_enabled", MappingProxyType({"x": "maybe"})) is False
