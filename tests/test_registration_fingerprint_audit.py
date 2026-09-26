"""Registration reports the selected fingerprint and independently observed exit."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from sms_tool.registration_handlers import RegistrationEmailWorkflow, _apply_protocol_fingerprint
from sms_tool.registration_result import build_registration_failure_result
from sms_tool.registration_state import RegistrationStateMachine


def test_protocol_country_policy_mismatch_remains_nonfatal_and_visible():
    operations = Mock()
    operations._failure_result.return_value = build_registration_failure_result(
        error="email_otp_poll_timeout", registration_mode="protocol"
    )
    config = {"registration": {"fingerprint_pool": {"allowed_countries": ["VN"]}}}
    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None),
        operations=operations, config=config,
        proxy_metadata={"actual_country": "DE"},
    )
    pool = Mock()
    pool.size = 1
    pool.next.return_value = SimpleNamespace(
        country="US", timezone="America/New_York", lang="en-US",
        lang_full="en-US,en;q=0.9", name="chrome",
    )
    with patch("sms_tool.fingerprint_pool.shared_fingerprint_pool", return_value=pool):
        workflow.fingerprint_country = _apply_protocol_fingerprint(
            operations, config, "http://proxy.invalid:8080"
        )
    result = workflow._abort_result("email_otp_poll_timeout")
    assert result["fingerprint_geo_audit"] == {
        "status": "mismatch", "fingerprint_country": "US",
        "exit_country": "DE", "source": "preflight",
        "allowed_status": "mismatch",
    }
    assert "fingerprint_geo_mismatch" in result["registration_warning"]
    assert result["success"] is False  # Original failure, not a new geo gate.


def test_resumed_attempt_without_reselected_fingerprint_does_not_invent_alignment():
    operations = Mock()
    operations._failure_result.return_value = build_registration_failure_result(
        error="auth_session_pending", registration_mode="protocol"
    )
    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None), operations=operations,
        proxy_metadata={"actual_country": "DE"},
    )
    result = workflow._abort_result("auth_session_pending")
    assert result["fingerprint_geo_audit"]["status"] == "unknown"
    assert result["fingerprint_geo_audit"]["fingerprint_country"] == ""
