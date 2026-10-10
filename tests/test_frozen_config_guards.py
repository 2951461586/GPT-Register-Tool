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
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

from sms_tool import config as config_module
from sms_tool import registration_edge_challenge, registration_probe, registration_sentinel_stages
from sms_tool.providers import mailbox_smailr
from sms_tool.registration_handlers import RegistrationEmailWorkflow
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


# ---------------------------------------------------------------------------
# The four non-``auth_flow`` registration toggles (2026-10-10 scan P1-C′)
# ---------------------------------------------------------------------------
#
# ``e94688b`` converged only the eight toggles inside ``auth_flow``.  Four A/B
# toggles outside it kept hand-rolling the parse, with three different
# unknown-value semantics -- ``sentinel_password_bundle`` even failed **open**
# (an unrecognised value turned a default-off switch on).  They now read through
# ``sms_tool.registration_flags.registration_flag``; these tests pin both the
# frozen-section read and the fixed fail-closed polarity.

#: ``(key, callable(self))`` for every toggle that shares the leaf parser.
_CONVERGED_FLAG_SITES = (
    ("sentinel_password_bundle", registration_sentinel_stages.password_sentinel_bundle_enabled),
    ("edge_challenge_rotate_exit", registration_edge_challenge.edge_challenge_rotate_exit_enabled),
    ("prime_about_you_page", RegistrationEmailWorkflow._prime_about_you_page_enabled),
    ("create_account_disallowed_backoff", RegistrationEmailWorkflow._create_account_disallowed_backoff_delays),
)


def _read_converged_gate(reader, config):
    """Call one gate the way production does: ``self.config`` is the frozen root."""
    return reader(SimpleNamespace(config=config))


def test_the_converged_toggles_honour_a_frozen_section():
    """The P1-D class, now through the shared leaf parser for these four too."""
    for key, reader in _CONVERGED_FLAG_SITES:
        frozen = MappingProxyType({"registration": MappingProxyType({key: True})})
        assert bool(_read_converged_gate(reader, frozen)) is True, key
        frozen_off = MappingProxyType({"registration": MappingProxyType({key: False})})
        assert bool(_read_converged_gate(reader, frozen_off)) is False, key
        # string forms a JSON/hand-edited config actually produces
        as_text = MappingProxyType({"registration": MappingProxyType({key: "true"})})
        assert bool(_read_converged_gate(reader, as_text)) is True, key
        as_text_off = MappingProxyType({"registration": MappingProxyType({key: "off"})})
        assert bool(_read_converged_gate(reader, as_text_off)) is False, key


def test_the_converged_toggles_default_off_when_the_key_is_missing():
    for key, reader in _CONVERGED_FLAG_SITES:
        for config in (MappingProxyType({"registration": MappingProxyType({})}), MappingProxyType({}), None):
            assert bool(_read_converged_gate(reader, config)) is False, (key, config)


def test_an_unrecognised_value_is_falsy_for_every_converged_toggle():
    """The fix itself: ``sentinel_password_bundle`` used to fail **open**.

    Its old body was ``value not in (False, 0, "0", ..., "")``, so any
    unrecognised value -- including a typo -- turned the switch on.  The shared
    parser answers the toggle's own default instead, which is ``False`` here.
    """
    section = MappingProxyType({key: "maybe" for key, _reader in _CONVERGED_FLAG_SITES})
    config = MappingProxyType({"registration": section})
    for key, reader in _CONVERGED_FLAG_SITES:
        assert bool(_read_converged_gate(reader, config)) is False, key


def test_the_converged_toggles_and_auth_flow_agree_on_an_unknown_value():
    """One semantics, two hosts: the leaf parser and ``steps._registration_flag``.

    ``auth_flow.steps`` keeps its own copy on purpose (importing the leaf module
    there would grow the import-layer ratchet), so the bodies are pinned to agree
    rather than merged.
    """
    from sms_tool.auth_flow import deps, steps

    frozen = MappingProxyType({"registration": MappingProxyType({"signin_prompt_login": "maybe"})})
    with patch.object(deps, "current_config_data", return_value=frozen):
        assert steps._signin_prompt_login_enabled() is False

    leaf = MappingProxyType({"registration": MappingProxyType({"signin_prompt_login": "maybe"})})
    from sms_tool.registration_flags import registration_flag

    assert registration_flag(leaf, "signin_prompt_login", False) is False
