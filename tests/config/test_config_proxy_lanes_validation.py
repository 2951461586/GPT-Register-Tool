"""``proxy.lanes`` must be a validated config surface, not a silent fallback.

Why this exists
---------------
``proxy_routing.canonical_lane_pool`` treats ``proxy.lanes`` as *authoritative*
when it is present (the documented single declaration point), but
``validate_config`` only checked ``proxy.pool`` / ``proxy.health`` and a handful
of legacy ``*_pool`` keys.  A misspelled lane name or a malformed pool therefore
sailed through validation and silently fell back to the legacy keys -- the
operator's declared lane was dead and nothing said so.

These tests pin the behaviour:

* a well-formed ``proxy.lanes`` block (bare list, string, mapping and the
  payment ``{pools, default}`` shape) validates;
* the lanes the desktop's 基础网络 page writes -- including
  ``payment_qualification`` -- validate, because a desktop save must never
  produce a config the backend then refuses to load;
* an unknown lane name is rejected instead of ignored;
* malformed lane / payment shapes are rejected with a path-prefixed message;
* the shipped example still validates, so the guard does not break the template.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sms_tool.config import ConfigError, validate_config

ROOT = Path(__file__).resolve().parents[2]

_BASE = {
    "chatgpt": {
        "auth_base_url": "https://auth.openai.com",
        "chat_base_url": "https://chatgpt.com",
    }
}


def _config(lanes: object) -> dict[str, object]:
    config: dict[str, object] = dict(_BASE)
    config["proxy"] = {"lanes": lanes}
    return config


@pytest.mark.parametrize(
    "lanes",
    [
        {"protocol_registration": ["http://u:p@reg.example:1"]},
        {"browser_registration": "http://browser.example:2"},
        {"mailbox": {"pool": ["http://mail.example:3"]}},
        {"mailbox": {"proxies": "http://mail.example:3", "default": ["http://mail.example:4"]}},
        # The four lanes 设置 → 基础网络 writes, in the shape SettingsService.Save
        # produces (``proxy.lanes.<lane>.pool``). payment_qualification used to be
        # missing from supported_lanes, so a desktop save made every backend
        # command fail validation with "unsupported proxy lane".
        {
            "protocol_registration": {"pool": ["http://reg.example:1"]},
            "browser_registration": {"pool": ["http://browser.example:2"]},
            "mailbox": {"pool": ["http://mail.example:3"]},
            "payment_qualification": {"pool": ["http://stripe.example:4"]},
        },
        {"payment_qualification": ["http://stripe.example:4"]},
        {"payment_qualification": "http://stripe.example:4"},
        # payment: bare pool, named region pools, and an optional default.
        {"payment": ["http://pay.example:5"]},
        {"payment": {"pools": {"US": ["http://us.example:6"], "JP": "http://jp.example:7"}}},
        {
            "payment": {
                "pools": {"US": ["http://us.example:6"]},
                "default": ["http://pay.example:5"],
            }
        },
        {
            "protocol_registration": ["http://reg.example:1"],
            "browser_registration": ["http://browser.example:2"],
            "mailbox": ["http://mail.example:3"],
            "payment": {"pools": {"US": ["http://us.example:6"]}},
        },
    ],
)
def test_well_formed_proxy_lanes_validate(lanes):
    validate_config(_config(lanes))


def test_absent_proxy_lanes_is_valid():
    validate_config(dict(_BASE))


def test_unknown_proxy_lane_is_rejected():
    with pytest.raises(ConfigError, match="unsupported proxy lane: protocol_registraion"):
        validate_config(_config({"protocol_registraion": ["http://reg.example:1"]}))


@pytest.mark.parametrize(
    "lanes, message",
    [
        (["http://reg.example:1"], "proxy.lanes must be an object"),
        ({"mailbox": 123}, "proxy.lanes.mailbox must be a proxy list"),
        ({"payment": 123}, "proxy.lanes.payment must be a proxy pool or an object"),
        ({"payment": {"pools": ["http://x:1"]}}, "proxy.lanes.payment.pools must be an object"),
        ({"payment": {"pools": {"US": 123}}}, "proxy.lanes.payment.pools.US must be a proxy list"),
        ({"payment": {"pools": {"": ["http://x:1"]}}}, "proxy.lanes.payment.pools names must not be blank"),
        ({"payment": {"default": 123}}, "proxy.lanes.payment.default must be a proxy list"),
        ({"mailbox": {"pool": 123}}, "proxy.lanes.mailbox.pool must be a proxy list"),
        (
            {"payment_qualification": {"pool": 123}},
            "proxy.lanes.payment_qualification.pool must be a proxy list",
        ),
    ],
)
def test_malformed_proxy_lanes_are_rejected(lanes, message):
    with pytest.raises(ConfigError) as excinfo:
        validate_config(_config(lanes))
    assert message in str(excinfo.value)


def test_example_config_documents_and_validates_proxy_lanes():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    lanes = example["proxy"]["lanes"]
    assert set(lanes) == {
        "protocol_registration",
        "browser_registration",
        "mailbox",
        "payment_qualification",
        "payment",
    }
    validate_config(example)


# ── proxy.allow_lane_overlap ─────────────────────────────────────────────────
#
# The key records "this lane may reuse a registration exit on purpose". It is
# validated for the same reason the lane names are: the reader
# (``proxy.registry.acknowledged_lane_overlaps``) ignores names it does not
# recognise, so a typo would leave the operator believing an overlap was
# acknowledged while the census kept flagging it. A missed flag here is not
# cosmetic -- a shared exit that cannot reach Stripe is what
# ``abort_reason=stripe_unreachable`` looks like from the outside.


def _overlap_config(value: object) -> dict[str, object]:
    config: dict[str, object] = dict(_BASE)
    config["proxy"] = {"pool": ["http://u:p@reg.example:1"], "allow_lane_overlap": value}
    return config


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["mailbox"],
        ["payment_qualification", "payment"],
        "mailbox,payment_qualification",
        {"payment_qualification": "same residential pool by design"},
    ],
)
def test_allow_lane_overlap_accepts_every_documented_shape(value):
    validate_config(_overlap_config(value))


@pytest.mark.parametrize("value", [["protocol_registration_typo"], ["protocol_registration"], ["nonsense"]])
def test_allow_lane_overlap_rejects_an_unknown_lane(value):
    """Registration is not overlappable: it owns the exits the others compare against."""
    with pytest.raises(ConfigError) as excinfo:
        validate_config(_overlap_config(value))
    assert "unsupported proxy.allow_lane_overlap lane" in str(excinfo.value)


def test_allow_lane_overlap_must_be_a_lane_name_container():
    with pytest.raises(ConfigError) as excinfo:
        validate_config(_overlap_config(7))
    assert "proxy.allow_lane_overlap must be an array of lane names" in str(excinfo.value)


def test_overlappable_lanes_match_the_registry_reader():
    """The schema's deny-list and the registry's allow-list must be one set.

    They are declared twice on purpose (``config`` sits below ``proxy`` in the
    layer manifest, so importing the registry from the schema would invert that
    edge). This is the pin that keeps the duplication honest: a lane the reader
    honours but the schema rejects -- or the other way round -- fails here.
    """
    from sms_tool.proxy import registry as proxy_registry
    from sms_tool.config import schema

    for lane in ("mailbox", "payment", "payment_qualification"):
        assert lane in schema._OVERLAPPABLE_PROXY_LANES
        assert lane in proxy_registry.acknowledged_lane_overlaps({"proxy": {"allow_lane_overlap": [lane]}})
    assert "protocol_registration" not in schema._OVERLAPPABLE_PROXY_LANES
    assert (
        proxy_registry.acknowledged_lane_overlaps({"proxy": {"allow_lane_overlap": ["protocol_registration"]}})
        == frozenset()
    )


def test_example_config_ships_an_empty_allow_lane_overlap():
    """The template must not acknowledge an overlap it does not have."""
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["proxy"]["allow_lane_overlap"] == []
    validate_config(example)
