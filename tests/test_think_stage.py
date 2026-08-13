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


def test_builtin_ranges_require_humanize_flag():
    with patch.object(utils.time, "sleep") as sleep:
        utils.think_stage("post_sentinel", {"registration": {"humanize": True}})
    sleep.assert_called_once()
    assert sleep.call_args[0][0] > 0


def test_no_stage_label_is_noop():
    with patch.object(utils.time, "sleep") as sleep:
        utils.think_stage("", {"registration": {"humanize": True}})
    sleep.assert_not_called()
