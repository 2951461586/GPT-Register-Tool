"""Tests for the randomized humanized dwell (registration think_stage)."""

from unittest.mock import patch

from sms_tool import utils


def test_humanize_delays_map_samples_stage_range():
    cfg = {"registration": {"humanize_delays": {"post_create_account": [100, 100]}}}
    with patch.object(utils.time, "sleep") as sleep:
        utils.think_stage("post_create_account", cfg)
    sleep.assert_called_once()
    assert abs(sleep.call_args[0][0] - 0.1) < 1e-6


def test_legacy_fixed_think_time_is_jittered():
    cfg = {"registration": {"think_time_ms": 1000}}
    with patch.object(utils.time, "sleep") as sleep:
        utils.think_stage("post_sentinel", cfg)
    sleep.assert_called_once()
    value = sleep.call_args[0][0]
    assert 0.65 <= value <= 1.35  # +/-35% band around 1000ms, not a constant


def test_disabled_by_default():
    with patch.object(utils.time, "sleep") as sleep:
        utils.think_stage("post_sentinel", {"registration": {}})
    sleep.assert_not_called()


def test_builtin_ranges_require_humanize_enabled():
    with patch.object(utils.time, "sleep") as sleep:
        utils.think_stage("post_sentinel", {"registration": {"humanize": {"enabled": True, "factor": 1.0}}})
    sleep.assert_called_once()
    assert sleep.call_args[0][0] > 0


def test_a_disabled_humanize_object_never_sleeps():
    """2026-10-11 scan P1-A: a non-empty object is truthy.

    The documented shape is ``{"enabled": false, "factor": 1.0}``; reading the
    raw value for truthiness meant the built-in ranges ran even though the
    operator disabled them.
    """
    with patch.object(utils.time, "sleep") as sleep:
        utils.think_stage("post_sentinel", {"registration": {"humanize": {"enabled": False, "factor": 1.0}}})
    sleep.assert_not_called()


def test_legacy_scalar_humanize_still_enables():
    """The pre-object ``"humanize": true`` form must keep working."""
    with patch.object(utils.time, "sleep") as sleep:
        utils.think_stage("post_sentinel", {"registration": {"humanize": True}})
    sleep.assert_called_once()
    assert sleep.call_args[0][0] > 0


def test_humanize_factor_scales_the_dwell():
    """``humanize.factor`` is the documented scaler; ``utils`` must read it."""
    cfg = {"registration": {"humanize": {"enabled": True, "factor": 2.0}}}
    with (
        patch.object(utils.random, "uniform", return_value=1000.0),
        patch.object(utils.time, "sleep") as sleep,
    ):
        utils.think_stage("post_sentinel", cfg)
    sleep.assert_called_once()
    assert abs(sleep.call_args[0][0] - 2.0) < 1e-6


def test_no_stage_label_is_noop():
    with patch.object(utils.time, "sleep") as sleep:
        utils.think_stage("", {"registration": {"humanize": True}})
    sleep.assert_not_called()
