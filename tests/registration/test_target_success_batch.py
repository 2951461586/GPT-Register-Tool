"""Tests for generic ``--target-success`` early-stop batch registration."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from sms_tool import cli
from sms_tool.registration import batch_runner
from sms_tool.commands.registration import build_registration_batch_summary
from sms_tool.registration.pulse import PulseConfig


def _fake_mailboxes(count: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(email=f"target{i}@example.test") for i in range(count)]


def _patch_batch_runner_env(monkeypatch, *, pulse_cfg: PulseConfig | None = None) -> None:
    monkeypatch.setattr(batch_runner, "ensure_batch_sentinel_readiness", lambda _cfg, _drv: "local")
    monkeypatch.setattr(batch_runner, "select_registration_proxy_pool", lambda pool, _proxy: list(pool))
    monkeypatch.setattr(
        batch_runner,
        "CFG",
        {
            "email_registration": {"sentinel_prewarm_window": 0},
            "registration": {"batch_circuit_breaker_enabled": False},
        },
    )
    if pulse_cfg is not None:
        monkeypatch.setattr(
            "sms_tool.registration.pulse.PulseConfig.from_config", classmethod(lambda cls, _cfg: pulse_cfg)
        )
    else:
        monkeypatch.setattr(
            "sms_tool.registration.pulse.PulseConfig.from_config",
            classmethod(lambda cls, _cfg: PulseConfig(enabled=False)),
        )


def test_target_success_sequential_stops_without_consuming_extra_mailboxes(monkeypatch):
    _patch_batch_runner_env(monkeypatch)
    mailboxes = _fake_mailboxes(5)
    attempted: list[str] = []

    def fake_run_email(**kwargs):
        email = kwargs["mailbox"].email
        attempted.append(email)
        if email == "target0@example.test":
            return {"success": False, "email": email, "error": "otp_timeout", "retryable": False}
        return {"success": True, "email": email}

    results = batch_runner.run_batch_impl(
        count=5,
        mailboxes=mailboxes,
        workers=1,
        max_attempts=1,
        retry_delay_seconds=0.0,
        run_email_func=fake_run_email,
        registration_driver="protocol",
        target_success=2,
    )

    assert attempted == [
        "target0@example.test",
        "target1@example.test",
        "target2@example.test",
    ]
    assert len(results) == 3
    assert sum(1 for item in results if item.get("success")) == 2


def test_target_success_threaded_caps_in_flight_to_remaining_needed(monkeypatch):
    _patch_batch_runner_env(monkeypatch)
    mailboxes = _fake_mailboxes(6)
    attempted: list[str] = []
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_run_email(**kwargs):
        nonlocal active, max_active
        email = kwargs["mailbox"].email
        with lock:
            attempted.append(email)
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        if email == "target0@example.test":
            return {"success": False, "email": email, "error": "signup_failed", "retryable": False}
        return {"success": True, "email": email}

    results = batch_runner.run_batch_impl(
        count=6,
        mailboxes=mailboxes,
        workers=4,
        max_attempts=1,
        retry_delay_seconds=0.0,
        run_email_func=fake_run_email,
        registration_driver="protocol",
        target_success=2,
    )

    assert max_active <= 2
    assert len(attempted) == 3
    assert "target3@example.test" not in attempted
    assert sum(1 for item in results if item.get("success")) == 2


def test_target_success_pulse_stops_after_wave_without_inter_wave_sleep(monkeypatch):
    _patch_batch_runner_env(
        monkeypatch,
        pulse_cfg=PulseConfig(
            enabled=True,
            wave_size=2,
            wave_delay_seconds=60.0,
            canary_enabled=False,
        ),
    )
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "sms_tool.registration.pulse.cancellable_sleep",
        lambda seconds, **_kwargs: sleep_calls.append(seconds) or False,
    )
    mailboxes = _fake_mailboxes(6)
    attempted: list[str] = []

    def fake_run_email(**kwargs):
        email = kwargs["mailbox"].email
        attempted.append(email)
        return {"success": True, "email": email}

    results = batch_runner.run_batch_impl(
        count=6,
        mailboxes=mailboxes,
        workers=2,
        max_attempts=1,
        retry_delay_seconds=0.0,
        run_email_func=fake_run_email,
        registration_driver="protocol",
        target_success=2,
    )

    assert len(results) == 2
    assert len(attempted) == 2
    assert sleep_calls == []


def test_target_success_summary_and_exit_code_contract():
    met = build_registration_batch_summary(
        effective_count=3,
        success_count=2,
        batch_id="batch_ok",
        saved_count=2,
        db_saved_count=2,
        quality=None,
        promotion_report=None,
        health_job_ids=[],
        results=[{"success": False}, {"success": True}, {"success": True}],
        target_success=2,
    )
    assert met["ok"] is True
    assert met["total"] == 3
    assert met["success"] == 2
    assert met["failed"] == 1
    assert cli._registration_exit_code(met, target_success=2) == 0

    missed = build_registration_batch_summary(
        effective_count=3,
        success_count=1,
        batch_id="batch_short",
        saved_count=1,
        db_saved_count=1,
        quality=None,
        promotion_report=None,
        health_job_ids=[],
        results=[{"success": False}, {"success": False}, {"success": True}],
        target_success=2,
    )
    assert missed["ok"] is False
    assert cli._registration_exit_code(missed, target_success=2) == 3


@pytest.mark.parametrize(
    ("count", "target_success", "pool_len", "expected_effective"),
    [
        (1, 2, 5, 5),
        (2, 2, 5, 5),
        (4, 2, 6, 4),
        (3, 0, 6, 3),
    ],
)
def test_resolve_target_success_plan(count, target_success, pool_len, expected_effective):
    parser = cli.build_parser()
    args = parser.parse_args(["--count", str(count), "--target-success", str(target_success)])
    effective, target = cli._resolve_target_success_plan(args, _fake_mailboxes(pool_len), count)
    assert effective == expected_effective
    assert target == target_success
