"""Key-level validation and advisories for the ``account_health`` config section.

Round-3 audit P1-3 claimed "no key-level validation at all". That is wrong --
``config.validate_config`` already type-checks, range-checks and even rejects
unknown keys for ``account_health.proxies`` lanes and ``registration.drivers``.
What was actually missing is narrower:

* ``account_health`` **scalar** keys (``workers`` / ``max_pending`` / timeouts)
  were never type-checked, so ``"workers": "four"`` reached
  ``account_health_queue`` and blew up inside ``int()``.
* ``account_health.workers`` is **silently clamped** to 1..8
  (``account_health_queue.py:121``), so ``"workers": 16`` is accepted, stored,
  and then quietly ignored. That is a real trap, not a validation failure --
  hence a warning rather than an error.

Design rule enforced here: unknown keys are **warnings, never errors**. Turning
them into hard failures would break a working install the moment someone adds a
key this build does not know about yet.
"""

from __future__ import annotations

import pytest

from sms_tool.config import (
    ACCOUNT_HEALTH_KEYS,
    ACCOUNT_HEALTH_WORKER_RANGE,
    ConfigError,
    config_warnings,
    validate_config,
)


def _config(**account_health):
    return {"chatgpt": {}, "account_health": dict(account_health)}


# --------------------------------------------------------------------------
# warnings: unknown keys
# --------------------------------------------------------------------------

def test_unknown_account_health_key_is_reported():
    warnings = config_warnings(_config(workers=2, workes=4))
    assert len(warnings) == 1
    assert "workes" in warnings[0]
    assert "workers" not in warnings[0].split(":", 1)[1]


def test_known_keys_produce_no_warning():
    assert config_warnings(_config(
        use_registration_affinity=True,
        batch_timeout_seconds=900,
        workers=4,
        max_pending=1000,
        proxies={"liveness": ["http://127.0.0.1:7897"]},
    )) == []


def test_every_documented_example_key_is_known():
    """config.example.json must not list a key we would warn about."""
    import json
    from pathlib import Path

    example = json.loads(
        (Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8")
    )
    health = example.get("account_health") or {}
    assert set(health) - ACCOUNT_HEALTH_KEYS == set()


def test_non_mapping_health_section_is_ignored():
    assert config_warnings({"account_health": "nope"}) == []
    assert config_warnings({"account_health": None}) == []
    assert config_warnings({}) == []


# --------------------------------------------------------------------------
# warnings: silently clamped worker count
# --------------------------------------------------------------------------

def test_workers_above_the_effective_range_warns_with_the_clamped_value():
    warnings = config_warnings(_config(workers=16))
    assert len(warnings) == 1
    assert "1..8" in warnings[0]
    assert "runs as 8" in warnings[0]


def test_workers_below_the_effective_range_warns():
    warnings = config_warnings(_config(workers=0))
    assert len(warnings) == 1
    assert "runs as 1" in warnings[0]


def test_workers_inside_the_range_is_silent():
    low, high = ACCOUNT_HEALTH_WORKER_RANGE
    for value in range(low, high + 1):
        assert config_warnings(_config(workers=value)) == []


def test_non_numeric_workers_is_left_to_validation_not_warnings():
    """A type error belongs in validate_config; the clamp warning must not also fire."""
    assert config_warnings(_config(workers="four")) == []


def test_boolean_workers_is_not_treated_as_a_number():
    assert config_warnings(_config(workers=True)) == []


# --------------------------------------------------------------------------
# validation: scalar types
# --------------------------------------------------------------------------

def test_negative_workers_is_an_error():
    with pytest.raises(ConfigError) as excinfo:
        validate_config(_config(workers=-1))
    assert "account_health.workers must be a non-negative number" in str(excinfo.value)


def test_non_numeric_workers_is_an_error():
    with pytest.raises(ConfigError) as excinfo:
        validate_config(_config(workers="four"))
    assert "account_health.workers must be a non-negative number" in str(excinfo.value)


def test_non_numeric_max_pending_is_an_error():
    with pytest.raises(ConfigError) as excinfo:
        validate_config(_config(max_pending="lots"))
    assert "account_health.max_pending must be a non-negative number" in str(excinfo.value)


def test_absent_scalars_are_accepted():
    """A minimal config stays valid -- these keys are optional overrides."""
    validate_config({"chatgpt": {}, "account_health": {"relogin_cooldown_seconds": 300}})


def test_zero_is_allowed_where_zero_means_disabled():
    """relogin_cooldown_seconds=0 disables the cooldown; it must not be rejected."""
    validate_config(_config(relogin_cooldown_seconds=0))


def test_out_of_range_workers_still_validates():
    """The clamp is a trap, not an error -- config stays loadable."""
    validate_config(_config(workers=64))
