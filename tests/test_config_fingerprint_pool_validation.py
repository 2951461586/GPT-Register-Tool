"""``registration.fingerprint_pool`` must be a first-class config surface.

Why this exists
---------------
``FingerprintPool.from_config`` (``sms_tool/fingerprint_pool.py``) has always
read ``registration.fingerprint_pool`` (``mode`` / ``verify_hint`` /
``allowed_countries``), but the key appeared in **neither** ``config.example.json``
nor ``validate_config`` nor ``config_key_baseline.json``. An operator could not
discover it, and a typo or wrong type silently fell back to the default pool.

These tests pin the three legs of the fix:

* the template documents the key with a shape ``FingerprintPool`` accepts;
* ``validate_config`` rejects malformed values instead of ignoring them;
* the config-key ratchet sees the key as intentional.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sms_tool.config import ConfigError, validate_config
from sms_tool.fingerprint_pool import FingerprintPool

ROOT = Path(__file__).resolve().parents[1]

_BASE = {
    "chatgpt": {
        "auth_base_url": "https://auth.openai.com",
        "chat_base_url": "https://chatgpt.com",
    }
}


def _config(fingerprint_pool: object) -> dict[str, object]:
    config: dict[str, object] = dict(_BASE)
    config["registration"] = {"fingerprint_pool": fingerprint_pool}
    return config


def test_valid_fingerprint_pool_config_passes():
    validate_config(
        _config(
            {
                "mode": "round_robin",
                "verify_hint": True,
                "allowed_countries": ["VN", "us"],
            }
        )
    )


def test_absent_fingerprint_pool_is_valid():
    validate_config(dict(_BASE))


@pytest.mark.parametrize(
    "value, message",
    [
        (["not", "an", "object"], "fingerprint_pool must be an object"),
        ({"mode": "deterministic"}, "fingerprint_pool.mode must be"),
        ({"verify_hint": "yes"}, "fingerprint_pool.verify_hint must be a boolean"),
        ({"allowed_countries": "VN"}, "fingerprint_pool.allowed_countries must be an array"),
        ({"allowed_countries": ["VNM"]}, "two-letter ISO country codes"),
        ({"allowed_countries": ["U1"]}, "two-letter ISO country codes"),
    ],
)
def test_malformed_fingerprint_pool_is_rejected(value, message):
    with pytest.raises(ConfigError) as excinfo:
        validate_config(_config(value))
    assert message in str(excinfo.value)


def test_example_config_documents_fingerprint_pool():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    fp_cfg = example["registration"]["fingerprint_pool"]
    assert set(fp_cfg) == {"mode", "verify_hint", "allowed_countries"}
    # The shipped default must be the pool's own no-op behaviour, not an
    # accidental restriction: no country filter, random selection.
    assert fp_cfg["mode"] == "random"
    assert fp_cfg["verify_hint"] is False
    assert fp_cfg["allowed_countries"] == []
    validate_config(example)


def test_example_fingerprint_pool_round_trips_through_the_pool():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    pool = FingerprintPool.from_config(example)
    assert pool.size > 0
    # from_config must not have narrowed the pool; the example is unfiltered.
    assert pool.next().name
