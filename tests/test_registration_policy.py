import pytest

from sms_tool.backoff import transport_backoff
from sms_tool.registration_policy import registration_retry_decision
from sms_tool.registration_retry_guard import RegistrationRetryGuard


@pytest.mark.parametrize("error", [
    "manual_challenge_required: timeout",
    "browser_proxy_blocked",
    "mailbox_auth_invalid: connection rejected",
    "mailbox_endpoint_unavailable",
    "session_circuit_open:http_429",
    "stage_budget_exceeded:email_otp_wait",
])
def test_terminal_failures_never_retry_even_with_transport_keywords(error, tmp_path):
    decision = registration_retry_decision(error)
    assert not decision.retryable
    guard = RegistrationRetryGuard(path=tmp_path / "guard.json", threshold=1)
    guard.record("test@example.com", failure_class="network", error=error)
    assert not guard.check("test@example.com")["deferred"]


def test_transport_and_auth_state_remain_retryable():
    assert registration_retry_decision("connection reset").retryable
    assert registration_retry_decision("browser_registration_state_unknown").retryable
    assert transport_backoff(1, 2) == 2
    assert transport_backoff(20, 2) == 15
