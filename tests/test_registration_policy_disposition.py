from sms_tool.registration_policy import registration_retry_decision


def test_network_failure_allows_attempt_and_future_batch_retry():
    decision = registration_retry_decision("auth_flow_transport:connection reset")

    assert decision.attempt_retryable is True
    assert decision.future_batch_eligible is True
    assert decision.guard_action == "cooldown"
    assert decision.retryable is True  # compatibility alias


def test_otp_pending_skips_immediate_retry_but_escalates_across_batches():
    decision = registration_retry_decision("email_otp_send_stuck")

    assert decision.attempt_retryable is False
    assert decision.future_batch_eligible is True
    assert decision.guard_action == "otp_pending"


def test_mailbox_auth_failure_is_terminal_at_both_retry_levels():
    decision = registration_retry_decision("mailbox_auth_invalid")

    assert decision.attempt_retryable is False
    assert decision.future_batch_eligible is False
    assert decision.guard_action == "none"


def test_expired_recovery_is_a_permanent_dead_end():
    decision = registration_retry_decision("auth_session_recovery_expired")

    assert decision.attempt_retryable is False
    assert decision.future_batch_eligible is False
    assert decision.guard_action == "dead_end"


def test_rate_limit_is_future_batch_only():
    decision = registration_retry_decision("http_429")

    assert decision.attempt_retryable is False
    assert decision.future_batch_eligible is True
    assert decision.guard_action == "cooldown"
