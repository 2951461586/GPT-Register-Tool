"""The shipped config example must name the SDK build that ships with it.

``email_registration.sentinel_version`` is not decoration: it builds the
``frame.html?sv=`` URL, the Sentinel ``Referer`` and ``p[5]``.  On 2026-11-19 the
example still said ``20260219f9f6`` while ``runtime/sdk.js`` was the 2026-08-10
build, so a machine bootstrapped with ``--init-config`` (``cli._init_config_command``
→ ``initialize_config_shards``) claimed a February version while executing August
bytes -- a proof that contradicts itself in three places, for every account.

Nothing caught it: the config-key ratchet only checks that a *key* is legal, and
``config.example.json`` is not compared to any code constant.  This module makes
the re-vendor step loud: bumping ``DEFAULT_SENTINEL_VERSION`` without re-vendoring
the example fails here, and so does the reverse.
"""

from __future__ import annotations

import json
from pathlib import Path

from sms_tool.sentinel import bundle

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config.example.json"


def _email_registration() -> dict:
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8-sig"))
    section = raw.get("email_registration")
    assert isinstance(section, dict), "config.example.json lost its email_registration section"
    return section


def test_example_sentinel_version_is_the_vendored_build() -> None:
    assert _email_registration()["sentinel_version"] == bundle.DEFAULT_SENTINEL_VERSION


def test_a_missing_version_key_would_be_caught() -> None:
    """The assertion above is only a guard if the key is present."""
    assert "sentinel_version" in _email_registration()


def test_example_auto_update_is_off_and_typed() -> None:
    """The refresh switch ships off.

    A config template that turns remote code fetch on by default would make the
    first registration of a new machine execute bytes this repository never
    measured -- through a ``bool("false") == True`` style default, no less.
    """
    section = _email_registration()
    assert section["sentinel_sdk_auto_update"] is False
    assert isinstance(section["sentinel_sdk_refresh_seconds"], int)
    assert section["sentinel_sdk_refresh_seconds"] == bundle.DEFAULT_REFRESH_SECONDS


def test_pin_covers_the_runtime_asset_that_is_actually_shipped() -> None:
    """``DEFAULT_SENTINEL_VERSION`` and the pinned native VM digest describe the shipped runtime."""
    assert bundle.NATIVE_LIB_PATH.is_file()
    assert bundle._binary_digest(bundle.NATIVE_LIB_PATH) == bundle.NATIVE_LIB_SHA256


def test_upi_sentinel_uses_unified_sentinel_client() -> None:
    """``upi_link`` must not ship a duplicate vendored Node Sentinel bridge."""
    assert not (ROOT / "sms_tool" / "upi_link" / "_vendor").exists()
