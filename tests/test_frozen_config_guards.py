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
