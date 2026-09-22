"""Permanent dead ends in the retry guard.

``user_already_exists`` is not a cooldown.  The server has already stated the
address is a registered account and we hold no credentials for it, so retrying
can only burn another email OTP on the way to ``/about-you``.  A cooldown
expires; this must not.

Measured 2026-09-14: three such addresses cost 25 attempts / 2595s / ~25 OTPs
in one day, and the guard remembered none of them -- the unretryable branch
deleted its own evidence on every failure.

2026-09-16（P1-2）: the same bug had a second entrance.  ``_resume_post_create``
aborts an unrecoverable ``auth_session_pending`` checkpoint with
``auth_session_recovery_*``; those were non-retryable, so ``record`` deleted the
row and the address came back on **every** batch -- one address 8 times, another
3 times, each time claiming a mailbox slot and aborting before any stage ran.
"""
from __future__ import annotations

import time

import pytest

from sms_tool import registration_checkpoint
from sms_tool.registration_policy import registration_retry_decision
from sms_tool.registration_retry_guard import RegistrationRetryGuard


def _guard(tmp_path, **kwargs):
    return RegistrationRetryGuard({}, path=tmp_path / "guard.json", **kwargs)


# --------------------------------------------------------------------------
# the flag survives the clock
# --------------------------------------------------------------------------

def test_mark_dead_end_survives_a_reload(tmp_path):
    guard = _guard(tmp_path)
    guard.mark_dead_end("dead@example.com", reason="user_already_exists")

    state = _guard(tmp_path).check("dead@example.com")
    assert state["dead_end"] is True
    assert state["dead_end_reason"] == "user_already_exists"


def test_a_dead_end_also_reports_deferred(tmp_path):
    """Fail-safe: a caller that only understands the cooldown contract skips it."""
    guard = _guard(tmp_path)
    guard.mark_dead_end("dead@example.com", reason="user_already_exists")

    assert guard.check("dead@example.com")["deferred"] is True


def test_a_dead_end_is_not_released_by_the_clock(tmp_path):
    """The whole point: no amount of waiting makes a used address free."""
    guard = _guard(tmp_path, cooldown_seconds=60)
    guard.mark_dead_end("dead@example.com", reason="user_already_exists")

    row = guard._read()["dead@example.com"]
    assert row["cooldown_until"] == 0

    state = guard.check("dead@example.com")
    assert state["remaining_seconds"] == 0
    assert state["dead_end"] is True


def test_a_fresh_address_is_not_a_dead_end(tmp_path):
    assert _guard(tmp_path).check("fresh@example.com")["dead_end"] is False


# --------------------------------------------------------------------------
# record() must not delete its own evidence
# --------------------------------------------------------------------------

def test_record_keeps_a_user_already_exists_failure_as_a_dead_end(tmp_path):
    guard = _guard(tmp_path)
    guard.record(
        "dead@example.com",
        failure_class="account",
        error="existing_account_user_already_exists:continue_to_login",
        success=False,
    )

    assert _guard(tmp_path).check("dead@example.com")["dead_end"] is True
    assert "dead@example.com" in _guard(tmp_path).dead_end_emails()


def test_record_still_clears_an_ordinary_unretryable_failure(tmp_path):
    """The dead-end branch must not swallow every non-retryable class."""
    guard = _guard(tmp_path)
    guard.record("x@example.com", failure_class="account", error="account_deactivated", success=False)

    assert _guard(tmp_path).check("x@example.com")["dead_end"] is False
    assert _guard(tmp_path).dead_end_emails() == set()


def test_a_successful_attempt_clears_the_dead_end(tmp_path):
    guard = _guard(tmp_path)
    guard.mark_dead_end("dead@example.com", reason="user_already_exists")
    guard.record("dead@example.com", success=True)

    assert _guard(tmp_path).check("dead@example.com")["dead_end"] is False


# --------------------------------------------------------------------------
# the bulk lookup the pool filter uses
# --------------------------------------------------------------------------

def test_dead_end_emails_is_case_insensitive(tmp_path):
    guard = _guard(tmp_path)
    guard.mark_dead_end("A@Example.com", reason="user_already_exists")
    guard.mark_dead_end("b@example.com", reason="user_already_exists")
    # A retryable class accumulates a cooldown and must NOT become a dead end.
    guard.record("c@example.com", failure_class="auth_state", error="invalid_state", success=False)

    assert _guard(tmp_path).dead_end_emails() == {"a@example.com", "b@example.com"}


def test_an_empty_guard_reports_no_dead_ends(tmp_path):
    assert _guard(tmp_path).dead_end_emails() == set()


def test_an_unreadable_file_is_not_a_dead_end(tmp_path):
    """A corrupt guard file must not be read as "every address is dead"."""
    path = tmp_path / "guard.json"
    path.write_text("{not json", encoding="utf-8")
    guard = RegistrationRetryGuard({}, path=path)

    assert guard.dead_end_emails() == set()
    assert guard.check("x@example.com")["dead_end"] is False


# --------------------------------------------------------------------------
# P1-2: 恢复检查点的窗口判决也必须是死路，而不是「查完就忘」
# --------------------------------------------------------------------------

def _recovery_payload(**overrides):
    """``auth_session_pending`` 检查点的形状（``create_ok`` + 可用 cookie）。"""
    return {
        "registration_state": "auth_session_pending",
        "create_ok": True,
        "session_recovery_started_at": int(time.time()),
        "session_recovery_attempts": 0,
        "session_cookies": [{"name": "fixture", "value": "synthetic",
                             "domain": "auth.example.test", "path": "/", "secure": True}],
        **overrides,
    }


#: ``session_recovery_error`` 的**每一个**非空出口。判据要双向自证：这个清单
#: 少一项，就意味着某个恢复窗口判决会重新掉进「查完就忘」的循环。
RECOVERY_VERDICTS = [
    ({"session_recovery_attempts": "invalid"}, "auth_session_recovery_context_missing"),
    ({"session_cookies": []}, "auth_session_recovery_context_missing"),
    ({"session_recovery_attempts": 2}, "auth_session_recovery_exhausted"),
    ({"session_recovery_started_at": 1}, "auth_session_recovery_expired"),
]


@pytest.mark.parametrize("overrides, expected", RECOVERY_VERDICTS)
def test_every_recovery_checkpoint_verdict_is_a_permanent_dead_end(tmp_path, overrides, expected):
    payload = _recovery_payload(**overrides)
    assert registration_checkpoint.session_recovery_error(payload) == expected
    # 前置条件：非可重试 ⇒ ``record`` 走 else 分支（否则测的就不是这条路径）。
    assert registration_retry_decision(expected).retryable is False

    RegistrationRetryGuard({}, path=tmp_path / "guard.json").record(
        "stale@example.com", failure_class="account", error=expected, success=False,
    )

    state = _guard(tmp_path).check("stale@example.com")
    assert state["dead_end"] is True
    assert state["dead_end_reason"] == expected
    assert state["remaining_seconds"] == 0
    assert "stale@example.com" in _guard(tmp_path).dead_end_emails()


def test_an_expired_checkpoint_is_not_re_attempted_on_the_next_batch(tmp_path):
    """原始症状：同一地址每个批次重来一次（实测 8 次 / 3 次）。"""
    error = registration_checkpoint.session_recovery_error(
        _recovery_payload(session_recovery_started_at=1)
    )
    RegistrationRetryGuard({}, path=tmp_path / "guard.json").record(
        "stale@example.com", failure_class="account", error=error, success=False,
    )

    # 下一个批次从磁盘重读同一份账本 —— 必须直接跳过，不再消费邮箱槽。
    next_batch = _guard(tmp_path).check("stale@example.com")
    assert next_batch["deferred"] is True
    assert next_batch["dead_end"] is True


def test_a_recovery_marker_does_not_swallow_neighbouring_failures(tmp_path):
    """反向自证：判据是**精确名**，不是 ``auth_session_recovery_`` 前缀。"""
    guard = _guard(tmp_path)
    for index, error in enumerate(
        ("auth_session_recovery_retry_later", "auth_session_recovery", "session_recovery_error")
    ):
        guard.record(f"n{index}@example.com", failure_class="account", error=error, success=False)

    assert guard.dead_end_emails() == set()


# --------------------------------------------------------------------------
# OTP dispatch pending: cool down once, quarantine on the second batch
# --------------------------------------------------------------------------

def test_first_otp_send_stuck_enters_cross_batch_cooldown(tmp_path):
    guard = _guard(tmp_path, cooldown_seconds=600)

    guard.record(
        "pending@example.com",
        failure_class="mailbox",
        error="email_otp_send_stuck",
    )

    state = _guard(tmp_path, cooldown_seconds=600).check("pending@example.com")
    assert state["deferred"] is True
    assert state["quarantined"] is False
    assert state["otp_pending_count"] == 1
    assert state["remaining_seconds"] > 0


def test_second_otp_send_stuck_quarantines_without_claiming_partial_registration(tmp_path):
    guard = _guard(tmp_path, cooldown_seconds=600)
    for _ in range(2):
        guard.record(
            "pending@example.com",
            failure_class="mailbox",
            error="email_otp_send_stuck",
        )

    state = _guard(tmp_path, cooldown_seconds=600).check("pending@example.com")
    assert state["deferred"] is True
    assert state["quarantined"] is True
    assert state["quarantine_reason"] == "email_otp_send_stuck"
    assert state["dead_end"] is False
    assert state["registration_status"] == ""
    assert "pending@example.com" in guard.quarantined_emails()
    assert "pending@example.com" not in guard.dead_end_emails()


def test_success_clears_otp_pending_quarantine(tmp_path):
    guard = _guard(tmp_path)
    guard.record("pending@example.com", failure_class="mailbox", error="email_otp_send_stuck")
    guard.record("pending@example.com", failure_class="mailbox", error="email_otp_send_stuck")

    guard.record("pending@example.com", success=True)

    state = guard.check("pending@example.com")
    assert state["deferred"] is False
    assert state["quarantined"] is False
    assert state["otp_pending_count"] == 0


def test_mailbox_side_no_code_does_not_increment_otp_pending_escalation(tmp_path):
    guard = _guard(tmp_path)

    guard.record(
        "mailbox@example.com",
        failure_class="mailbox",
        error="email_otp_poll_timeout:mailbox_side_no_code",
    )

    state = guard.check("mailbox@example.com")
    assert state["otp_pending_count"] == 0
    assert state["quarantined"] is False

