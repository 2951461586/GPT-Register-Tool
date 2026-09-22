"""Tests for pulse-wave batch scheduling (``sms_tool/registration_pulse.py``)."""

from __future__ import annotations

import pytest
import threading

from sms_tool import registration_pulse as pulse_module
from sms_tool.registration_pulse import (
    PulseConfig,
    _detect_ip_ban,
    _is_otp_ban_signal,
    run_pulse_batch,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Record sleeps instead of actually waiting."""
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(pulse_module.time, "sleep", fake_sleep)
    return calls


def _ok(index):
    return {"success": True, "email": f"user{index}@example.com"}


def _fail(error, failure_class="unknown"):
    return {"success": False, "error": error, "failure_class": failure_class}


# --------------------------------------------------------------------------
# PulseConfig
# --------------------------------------------------------------------------

def test_defaults_are_disabled_with_sane_wave_shape():
    cfg = PulseConfig()
    assert cfg.enabled is False
    assert cfg.wave_size == 4
    assert cfg.wave_delay_seconds == 5.0
    assert cfg.ban_threshold == 2
    assert cfg.ban_pause_seconds == 60.0
    assert cfg.max_waves == 0
    assert cfg.canary_enabled is True


def test_from_config_reads_nested_registration_pulse():
    cfg = PulseConfig.from_config({
        "registration": {
            "pulse": {
                "enabled": True,
                "wave_size": 3,
                "wave_delay_seconds": 1.5,
                "ban_threshold": 5,
                "ban_pause_seconds": 12,
                "max_waves": 7,
                "canary_enabled": False,
            }
        }
    })
    assert cfg.enabled is True
    assert (cfg.wave_size, cfg.wave_delay_seconds) == (3, 1.5)
    assert (cfg.ban_threshold, cfg.ban_pause_seconds, cfg.max_waves) == (5, 12.0, 7)
    assert cfg.canary_enabled is False


@pytest.mark.parametrize("payload", [None, {}, {"registration": None}, {"registration": {"pulse": "yes"}}])
def test_from_config_falls_back_on_missing_or_malformed_sections(payload):
    cfg = PulseConfig.from_config(payload)
    assert cfg.enabled is False
    assert cfg.wave_size == 4


def test_from_config_clamps_degenerate_values():
    cfg = PulseConfig.from_config({"registration": {"pulse": {"wave_size": 0, "ban_threshold": 0}}})
    assert cfg.wave_size == 1
    assert cfg.ban_threshold == 1


# --------------------------------------------------------------------------
# ban detection
# --------------------------------------------------------------------------

def test_success_is_never_a_ban_signal():
    assert _is_otp_ban_signal(_ok(0)) is False


def test_otp_markers_in_error_are_ban_signals():
    assert _is_otp_ban_signal(_fail("otp_not_received after 3 polls")) is True
    assert _is_otp_ban_signal(_fail("EMAIL_OTP_TIMEOUT")) is True


def test_non_otp_errors_are_not_ban_signals():
    assert _is_otp_ban_signal(_fail("connection reset by peer", "network")) is False


def test_rate_limit_and_account_failures_are_excluded():
    # Both classes are terminal per-account outcomes; treating them as an
    # IP ban would pause the whole batch for no reason.
    assert _is_otp_ban_signal(_fail("otp_timeout", "rate_limit")) is False
    assert _is_otp_ban_signal(_fail("otp_timeout", "account")) is False


def test_detect_ip_ban_requires_threshold():
    """阈值仍然要拦得住：wave_size=1 时「整轮一致」是白送的。

    2026-09-16 P0-2 起 ``_detect_ip_ban`` 还要求**整轮一致**（混合结局说明
    出口是通的，见 ``test_registration_otp_timeout_discrimination``），所以这里
    用整轮失败的 wave 来单独检验阈值这一维。
    """
    wave = [_fail("otp_timeout"), _fail("otp_timeout")]
    assert _detect_ip_ban(wave, threshold=3) is False
    assert _detect_ip_ban(wave, threshold=2) is True


def test_detect_ip_ban_ignores_a_wave_with_a_success():
    """混合结局不是出口封禁 —— 每个账号钉在池里各自的出口上。"""
    assert _detect_ip_ban([_fail("otp_timeout"), _ok(1)], threshold=1) is False


# --------------------------------------------------------------------------
# run_pulse_batch
# --------------------------------------------------------------------------

def test_runs_every_account_once_and_preserves_order():
    seen = []

    def run_one(idx):
        seen.append(idx)
        return idx, _ok(idx)

    results = run_pulse_batch(
        6,
        run_one_fn=run_one,
        workers=3,
        pulse_config=PulseConfig(enabled=True, wave_size=3, wave_delay_seconds=0),
    )

    assert len(results) == 6
    assert sorted(seen) == list(range(6))
    assert [r["email"] for r in results] == [f"user{i}@example.com" for i in range(6)]


def test_first_wave_is_a_single_account_canary():
    waves = []
    active_wave = []

    def run_one(idx):
        active_wave.append(idx)
        return idx, _ok(idx)

    run_pulse_batch(
        5,
        run_one_fn=run_one,
        on_wave_complete=lambda indices, _results: waves.append(list(indices)),
        workers=4,
        pulse_config=PulseConfig(
            enabled=True, wave_size=4, wave_delay_seconds=0, canary_enabled=True,
        ),
    )

    assert waves == [[0], [1, 2, 3, 4]]
    assert active_wave == [0, 1, 2, 3, 4]


def test_failed_canary_rotates_then_keeps_the_next_wave_as_a_canary(no_sleep):
    waves = []
    rotations = []

    run_pulse_batch(
        3,
        run_one_fn=lambda idx: (idx, _fail("email_otp_send_stuck", "mailbox")),
        on_dispatch_block=lambda: rotations.append("rotate") or True,
        on_wave_complete=lambda indices, _results: waves.append(list(indices)),
        workers=3,
        pulse_config=PulseConfig(
            enabled=True, wave_size=3, wave_delay_seconds=0,
            ban_threshold=2, ban_pause_seconds=10, canary_enabled=True,
        ),
    )

    assert waves == [[0], [1], [2]]
    assert rotations == ["rotate", "rotate"]
    assert abs(sum(no_sleep) - 20) < 0.01


def test_rotation_message_is_truthful_when_no_alternate_slot_exists(capsys, no_sleep):
    run_pulse_batch(
        2,
        run_one_fn=lambda idx: (idx, _fail("email_otp_send_stuck", "mailbox")),
        on_dispatch_block=lambda: False,
        workers=1,
        pulse_config=PulseConfig(
            enabled=True, wave_size=2, wave_delay_seconds=0,
            ban_pause_seconds=10, canary_enabled=True,
        ),
    )

    out = capsys.readouterr().out
    assert "no alternate proxy slot" in out.lower()
    assert "proxy pool cursor rotated" not in out.lower()


def test_on_result_fires_for_every_account():
    notified = {}
    run_pulse_batch(
        4,
        run_one_fn=lambda idx: (idx, _ok(idx)),
        on_result=lambda idx, result: notified.__setitem__(idx, result),
        workers=2,
        pulse_config=PulseConfig(enabled=True, wave_size=2, wave_delay_seconds=0),
    )
    assert sorted(notified) == [0, 1, 2, 3]


def test_wave_delay_is_applied_between_waves_only(no_sleep):
    run_pulse_batch(
        4,
        run_one_fn=lambda idx: (idx, _ok(idx)),
        workers=1,
        pulse_config=PulseConfig(
            enabled=True, wave_size=2, wave_delay_seconds=9, canary_enabled=False,
        ),
    )
    # 4 accounts / wave_size 2 => 2 waves => exactly one inter-wave gap.
    # The gap sleeps in bounded cancellable slices, so assert the total.
    assert abs(sum(no_sleep) - 9) < 0.01


def test_ip_ban_pauses_before_next_wave(no_sleep):
    def run_one(idx):
        # Every account in the first wave reports an OTP block.
        return idx, _fail("otp_not_received")

    run_pulse_batch(
        4,
        run_one_fn=run_one,
        workers=2,
        pulse_config=PulseConfig(
            enabled=True, wave_size=2, wave_delay_seconds=3,
            ban_threshold=2, ban_pause_seconds=30, canary_enabled=False,
        ),
    )
    # The blocked full wave forces the remaining accounts back through
    # one-account canaries. Both remaining canaries fail, so two 30s cooldowns
    # plus two 3s inter-wave gaps are expected.
    assert abs(sum(no_sleep) - 66) < 0.01


def test_no_pause_when_below_threshold(no_sleep):
    def run_one(idx):
        return idx, _fail("otp_not_received") if idx == 0 else _ok(idx)

    run_pulse_batch(
        4,
        run_one_fn=run_one,
        workers=2,
        pulse_config=PulseConfig(
            enabled=True, wave_size=2, wave_delay_seconds=3,
            ban_threshold=2, ban_pause_seconds=30, canary_enabled=False,
        ),
    )
    # Below the threshold no 30s ban pause is inserted: only the 3s wave gap.
    assert abs(sum(no_sleep) - 3) < 0.01


def test_max_waves_reports_skipped_accounts_instead_of_dropping_them():
    seen = []

    def run_one(idx):
        seen.append(idx)
        return idx, _ok(idx)

    results = run_pulse_batch(
        10,
        run_one_fn=run_one,
        workers=2,
        pulse_config=PulseConfig(
            enabled=True, wave_size=2, wave_delay_seconds=0,
            max_waves=1, canary_enabled=False,
        ),
    )

    assert seen == [0, 1]
    # Callers size their bookkeeping from the returned list, so the skipped
    # accounts must still be present as terminal failures.
    assert len(results) == 10
    assert all(r["success"] for r in results[:2])
    assert all(r == pulse_skipped() for r in results[2:])


def pulse_skipped():
    return {
        "success": False,
        "error": "pulse_max_waves_reached",
        "failure_class": "skipped",
        "dropped": False,
        "registration_attempts": 0,
    }


def test_max_waves_notifies_callback_for_skipped_accounts():
    notified = {}
    run_pulse_batch(
        6,
        run_one_fn=lambda idx: (idx, _ok(idx)),
        on_result=lambda idx, result: notified.__setitem__(idx, result),
        workers=2,
        pulse_config=PulseConfig(
            enabled=True, wave_size=2, wave_delay_seconds=0,
            max_waves=1, canary_enabled=False,
        ),
    )
    assert sorted(notified) == [0, 1, 2, 3, 4, 5]


def test_serial_path_used_for_single_worker():
    # workers=1 must not spin up a pool; results are produced in order.
    results = run_pulse_batch(
        3,
        run_one_fn=lambda idx: (idx, _ok(idx)),
        workers=1,
        pulse_config=PulseConfig(enabled=True, wave_size=1, wave_delay_seconds=0),
    )
    assert [r["email"] for r in results] == ["user0@example.com", "user1@example.com", "user2@example.com"]


def test_zero_count_returns_empty():
    assert run_pulse_batch(
        0,
        run_one_fn=lambda idx: (idx, _ok(idx)),
        workers=2,
        pulse_config=PulseConfig(enabled=True),
    ) == []


def test_cancel_event_marks_remaining_accounts_cancelled():
    event = threading.Event()
    event.set()
    results = run_pulse_batch(
        3,
        run_one_fn=lambda idx: (idx, _ok(idx)),
        workers=2,
        pulse_config=PulseConfig(enabled=True, wave_size=2, wave_delay_seconds=0),
        cancel_event=event,
    )
    assert len(results) == 3
    assert all(item["registration_state"] == "cancelled" for item in results)
    assert all(item["failure_class"] == "cancelled" for item in results)
