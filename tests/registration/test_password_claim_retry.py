"""Failures after ``user/register`` accepted the password are never retried.

Measured 2026-10-12 (lane5/lane6): once the password POST returned 200, every new
session for the same address answered ``invalid_auth_step`` (4/4 retries), whether the
run died at the OTP gate or at ``create_account``.  Before the POST, retrying is fine.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from sms_tool.kernel.failure_registry import PASSWORD_CLAIMED_MARKER
from sms_tool.registration.handlers import RegistrationEmailWorkflow
from sms_tool.registration.policy import registration_retry_decision
from sms_tool.registration.retry_guard import RegistrationRetryGuard
from sms_tool.registration.state import RegistrationState, RegistrationStateMachine

TRANSPORT = "create_account_transport:Failed to perform, curl: (28) Operation timed out"


def test_policy_stops_retry_and_marks_dead_end_after_the_password_was_claimed():
    plain = registration_retry_decision(TRANSPORT)
    claimed = registration_retry_decision(f"{TRANSPORT}|{PASSWORD_CLAIMED_MARKER}")

    assert plain.attempt_retryable is True
    assert plain.future_batch_eligible is True
    assert claimed.attempt_retryable is False
    assert claimed.future_batch_eligible is False
    assert claimed.guard_action == "dead_end"
    # The marker must not change what kind of failure it was.
    assert claimed.failure_class == plain.failure_class


def test_guard_records_a_permanent_dead_end_for_a_claimed_address(tmp_path):
    guard = RegistrationRetryGuard({}, path=tmp_path / "guard.json")
    guard.record(
        "claimed@example.com",
        failure_class="network",
        error=f"{TRANSPORT}|{PASSWORD_CLAIMED_MARKER}",
    )
    state = guard.check("claimed@example.com")
    assert state["dead_end"] is True
    assert state["dead_end_reason"] == PASSWORD_CLAIMED_MARKER
    assert "claimed@example.com" in guard.dead_end_emails()


def test_guard_still_cools_down_a_failure_before_the_password_post(tmp_path):
    guard = RegistrationRetryGuard({}, path=tmp_path / "guard.json", threshold=1)
    guard.record("early@example.com", failure_class="network", error="auth_flow_transport:curl: (28) timed out")
    state = guard.check("early@example.com")
    assert state["dead_end"] is False
    assert state["deferred"] is True


def _workflow(registration_mode="password", *, reached=(), **runtime):
    machine = RegistrationStateMachine(lambda *_: None)
    workflow = RegistrationEmailWorkflow(machine, operations=Mock(), config={})
    workflow.runtime.registration_mode = registration_mode
    workflow.runtime.access_token = runtime.get("access_token", "")
    workflow.runtime.existing_account = runtime.get("existing_account", False)
    workflow.runtime.signup_state = {}
    workflow.r._is_signup_password_step = Mock(return_value=False)
    for state in reached:
        machine.transition(state)
    return workflow


PAST_REGISTER = (RegistrationState.AUTH_FLOW, RegistrationState.USER_REGISTER, RegistrationState.EMAIL_OTP_SEND)


def test_password_lane_failure_after_user_register_is_tagged():
    workflow = _workflow(reached=PAST_REGISTER)
    assert workflow._with_password_claim(TRANSPORT) == f"{TRANSPORT}|{PASSWORD_CLAIMED_MARKER}"


def test_tagging_is_idempotent():
    workflow = _workflow(reached=PAST_REGISTER)
    once = workflow._with_password_claim(TRANSPORT)
    assert workflow._with_password_claim(once) == once


@pytest.mark.parametrize(
    "reached",
    [(), (RegistrationState.AUTH_FLOW,), (RegistrationState.AUTH_FLOW, RegistrationState.USER_REGISTER)],
)
def test_failure_before_the_password_post_completed_is_not_tagged(reached):
    assert _workflow(reached=reached)._with_password_claim(TRANSPORT) == TRANSPORT


def test_passwordless_run_never_posted_a_password_so_it_is_not_tagged():
    workflow = _workflow("passwordless", reached=PAST_REGISTER)
    assert workflow._with_password_claim(TRANSPORT) == TRANSPORT


def test_otp_stuck_keeps_its_own_guard_semantics():
    workflow = _workflow(reached=PAST_REGISTER)
    assert workflow._with_password_claim("email_otp_send_stuck") == "email_otp_send_stuck"


@pytest.mark.parametrize("runtime", [{"access_token": "tok"}, {"existing_account": True}])
def test_runs_that_hold_a_token_or_an_existing_account_are_left_to_recovery(runtime):
    workflow = _workflow(reached=PAST_REGISTER, **runtime)
    assert workflow._with_password_claim("missing_auth_session_access_token") == "missing_auth_session_access_token"


def test_abort_result_carries_the_tag_into_the_failure_result():
    workflow = _workflow(reached=PAST_REGISTER)
    workflow.r._failure_result = Mock(side_effect=lambda error, **kw: {"error": error, "success": False})
    result = workflow._abort_result(TRANSPORT)
    assert result["error"].endswith(PASSWORD_CLAIMED_MARKER)


def test_a_cancelled_run_keeps_its_exact_error():
    workflow = _workflow(reached=PAST_REGISTER)
    workflow.r._failure_result = Mock(side_effect=lambda error, **kw: {"error": error, "success": False})
    assert workflow._abort_result("registration_cancelled", cancelled=True)["error"] == "registration_cancelled"
