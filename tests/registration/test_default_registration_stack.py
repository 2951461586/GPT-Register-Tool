"""Pin the shipped default registration stack (2026-10-12 survival measurements).

The stack that kept every measured access token alive for 60 minutes (audit §12) is:
passwordless ``login_or_signup`` registration on Sentinel SDK ``20260810913b``, which
sends the SO header, with the reference lane and the post-registration warm-up off.
These tests fail if a default drifts away from it without a deliberate decision.
"""

from __future__ import annotations

import json
from pathlib import Path

from sms_tool.registration import reference_lane as lane
from sms_tool.registration.state import _normalize_registration_mode
from sms_tool.sentinel import bundle

ROOT = Path(__file__).resolve().parents[2]


def _example() -> dict:
    return json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))


def test_shipped_mode_is_passwordless():
    assert _example()["email_registration"]["registration_mode"] == "passwordless"
    assert _normalize_registration_mode("garbage") == "passwordless"
    assert _normalize_registration_mode("login_or_signup") == "passwordless"


def test_reference_lane_and_warm_up_ship_off():
    registration = _example()["registration"]
    assert registration["reference_lane"] is False
    assert lane.reference_lane_enabled(_example()) is False
    assert registration["chatgpt_bootstrap"]["enabled"] is False


def test_default_sentinel_sdk_is_the_one_that_sends_the_so_header():
    assert bundle.DEFAULT_SENTINEL_VERSION == "20260810913b"
