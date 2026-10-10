"""F3: the transaction-arm mismatch must not be read as an egress block.

The 2026-10-07/10-08 signin series measured that the stuck shape is the server
arming a **passwordless** OTP arm while the client posts the password lane.  The
pulse scheduler used to read every ``email_otp_send_stuck`` as an egress-level
block and rotate the proxy pool -- the wrong lever, because no exit changes which
arm the server picks (``docs/audits/scan-2026-10-07-protocol-registration.md``
P2-2, ``docs/audits/landing-2026-10-08-signin-*.md``).

Three pieces must line up or the change is inert:

1. ``auth_state.passwordless_arm_armed`` reads the dump;
2. ``registration_otp_stages.wait_email_otp`` writes the ``arm_mismatch`` suffix;
3. ``registration_pulse._is_otp_ban_signal`` short-circuits on that suffix
   **before** the ``otp_send_stuck`` substring it contains.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from sms_tool.auth_state import passwordless_arm_armed
from sms_tool.error_classification import classify_error
from sms_tool.failure_registry import OTP_ARM_MISMATCH_MARKER, OTP_UNDISPATCHED_MARKER
from sms_tool.registration_handlers import RegistrationAbort, RegistrationEmailWorkflow
from sms_tool.registration_pulse import _detect_ip_ban, _is_otp_ban_signal
from sms_tool.registration_state import RegistrationStateMachine


def _summary(*, mode, pending=True):
    keys = ["email", "email_verification_mode", "original_screen_hint", "signup_mode"]
    if pending:
        keys.append("passwordless_email_otp_send_pending")
    return {
        "top_keys": ["client_auth_session", "session_id"],
        "client_auth_session_keys": keys,
        "signals": {"client_auth_session.email_verification_mode": mode},
    }


# --------------------------------------------------------------------------
# 1) The dump reader
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["passwordless_login", "passwordless_signup"])
def test_a_passwordless_mode_is_read_from_the_dump(mode):
    assert passwordless_arm_armed(_summary(mode=mode)) is True


@pytest.mark.parametrize("mode", ["login_challenge", "login", ""])
def test_a_non_passwordless_mode_is_not_an_arm_mismatch(mode):
    assert passwordless_arm_armed(_summary(mode=mode)) is False


def test_a_redacted_mode_is_unknown_not_a_mismatch():
    """Only low-cardinality enums survive redaction; a length is not evidence."""
    assert passwordless_arm_armed(_summary(mode="[REDACTED](len=10)")) is False


@pytest.mark.parametrize("dump", [None, {}, "not a summary", {"signals": {}}, {"signals": "x"}])
def test_an_unusable_dump_refuses_to_guess(dump):
    assert passwordless_arm_armed(dump) is False


# --------------------------------------------------------------------------
# 2) The stage writes the suffix
# --------------------------------------------------------------------------


def _ops_stub():
    ops = Mock()
    ops.otp_poll.poll = Mock(return_value="")
    # ``_password_lane_active`` falls back to this predicate on the passwordless
    # lane; a bare Mock returns a truthy Mock and would silently put every run on
    # the password lane.
    ops._is_signup_password_step = Mock(return_value=False)
    return ops


def _workflow(registration_mode):
    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None),
        operations=_ops_stub(),
        config={},
    )
    workflow.runtime.resources.mailbox_service = Mock()
    workflow.runtime.registration_mode = registration_mode
    return workflow


def _wait(dump, registration_mode):
    workflow = _workflow(registration_mode)
    workflow.runtime.otp_send_dump = dump
    with pytest.raises(RegistrationAbort) as excinfo:
        workflow.wait_email_otp()
    return str(excinfo.value)


def test_the_password_lane_flags_the_arm_mismatch():
    error = _wait(_summary(mode="passwordless_login"), "password")
    assert error == f"email_otp_send_stuck:{OTP_ARM_MISMATCH_MARKER}"


def test_the_passwordless_lane_keeps_the_plain_name():
    """On the passwordless lane a ``passwordless_*`` arm is what was asked for."""
    error = _wait(_summary(mode="passwordless_signup"), "passwordless")
    assert error == "email_otp_send_stuck"


def test_an_unreadable_arm_keeps_the_plain_name():
    error = _wait(_summary(mode="login_challenge"), "password")
    assert error == "email_otp_send_stuck"


def test_the_suffix_does_not_change_the_failure_class():
    """A suffix must not re-classify -- same rule as the edge-challenge label."""
    assert classify_error("email_otp_send_stuck") == "mailbox"
    assert classify_error(f"email_otp_send_stuck:{OTP_ARM_MISMATCH_MARKER}") == "mailbox"


# --------------------------------------------------------------------------
# 3) The pulse short-circuit
# --------------------------------------------------------------------------


def test_the_pulse_does_not_read_an_arm_mismatch_as_an_egress_block():
    result = {
        "success": False,
        "error": f"email_otp_send_stuck:{OTP_ARM_MISMATCH_MARKER}",
        "failure_class": "mailbox",
    }
    assert _is_otp_ban_signal(result) is False


def test_a_plain_stuck_send_is_still_a_dispatch_side_candidate():
    result = {"success": False, "error": "email_otp_send_stuck", "failure_class": "mailbox"}
    assert _is_otp_ban_signal(result) is True
    assert OTP_UNDISPATCHED_MARKER in result["error"]


def test_a_wave_of_arm_mismatches_does_not_trip_the_ip_ban_verdict():
    wave = [
        {
            "success": False,
            "error": f"email_otp_send_stuck:{OTP_ARM_MISMATCH_MARKER}",
            "failure_class": "mailbox",
        }
        for _ in range(4)
    ]
    assert _detect_ip_ban(wave, threshold=2) is False
