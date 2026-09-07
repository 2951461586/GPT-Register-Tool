from unittest.mock import Mock

from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_runtime import RegistrationRuntimeState
from sms_tool.registration_state import RegistrationStateMachine


def test_grouped_state_and_flat_compatibility_share_storage():
    state = RegistrationRuntimeState(password="test-private", email_cfg={"otp_timeout": 15})
    assert state.identity.password == "test-private"
    state.otp.email_cfg["otp_timeout"] = 25
    assert state.email_cfg == {"otp_timeout": 25}
    state.access_token = "test-access"
    assert state.account.access_token == "test-access"
    assert "test-private" not in repr(state.identity)
    assert "test-access" not in repr(state.account)
    assert "test-private" not in repr(state)


def test_group_mutable_containers_are_per_workflow():
    first, second = RegistrationRuntimeState(), RegistrationRuntimeState()
    first.otp.otp_data["changed"] = True
    assert second.otp.otp_data == {}


def test_shared_session_closes_once_and_distinct_sessions_both_close():
    workflow = RegistrationEmailWorkflow(RegistrationStateMachine(lambda *_: None), operations=Mock())
    shared = Mock()
    workflow.runtime.resources.session = shared
    workflow.runtime.resources.login_session = shared
    workflow._close_sessions()
    shared.close.assert_called_once()
    second = Mock()
    workflow.runtime.resources.login_session = second
    workflow._close_sessions()
    assert shared.close.call_count == 2
    second.close.assert_called_once()
