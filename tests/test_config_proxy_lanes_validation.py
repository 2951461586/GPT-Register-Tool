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
* an unknown lane name is rejected instead of ignored;
* malformed lane / payment shapes are rejected with a path-prefixed message;
* the shipped example still validates, so the guard does not break the template.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sms_tool.config import ConfigError, validate_config

ROOT = Path(__file__).resolve().parents[1]

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
        "payment",
    }
    validate_config(example)
