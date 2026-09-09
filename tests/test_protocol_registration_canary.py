from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import nullcontext

from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_state import RegistrationState, RegistrationStateMachine


def _operations():
    ops = Mock()
    ops._tl.return_value = []
    ops._sentinel_device_id.return_value = "device-canary"
    ops._normalize_registration_mode.return_value = "passwordless"
    ops._stored_registration_password.return_value = ""
    ops._generate_password.return_value = "GeneratedPassword!1"
    ops._random_name.return_value = ("Ada", "Lovelace")
    ops._random_birthdate.return_value = "1990-01-01"
    ops.openai_auth_headers.return_value = {"oai-device-id": "device-canary"}
    ops._sanitize_text.side_effect = lambda value: str(value or "")
    ops.assert_sentinel_device_id = lambda *_args: None
    return ops


def test_protocol_identity_initialization_uses_mock_persistence_and_network():
    mailbox = SimpleNamespace(email="canary@example.com")
    persistence = Mock()
    persistence.get_device_context.return_value = {
        "device_id": "device-canary",
        "auth_session_logging_id": "logging-canary",
    }
    operations = _operations()
    operations.runtime_config_scope.return_value = nullcontext()
    config = {"email_registration": {}, "chatgpt": {}}
    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None),
        proxy="http://proxy.invalid:8080",
        mailbox=mailbox,
        config=config,
        operations=operations,
        persistence=persistence,
    )
    workflow.runtime.mailbox = mailbox
    workflow.runtime.proxy = "http://proxy.invalid:8080"
    workflow.runtime.auth_base = "https://auth.example"
    workflow.runtime.chat_base = "https://chat.example"
    workflow.runtime.sentinel_data = {"oai_did": "device-canary"}

    with patch("sms_tool.registration_handlers._new_registration_session", return_value=Mock()), \
         patch("sms_tool.registration_handlers._apply_protocol_fingerprint"):
        workflow.prepare_identity()

    persistence.get_device_context.assert_called_with("canary@example.com")
    assert workflow.runtime.device_id == "device-canary"
    assert workflow.runtime.session_logging_id == "logging-canary"
    operations.openai_auth_headers.assert_called_once()


def test_protocol_identity_bootstrap_canary_is_offline():
    mailbox = SimpleNamespace(email="bootstrap@example.com")
    persistence = Mock()
    persistence.get_device_context.return_value = {"device_id": "device-bootstrap"}
    operations = _operations()
    operations._sentinel_device_id.return_value = "device-bootstrap"
    operations.openai_auth_headers.return_value = {"oai-device-id": "device-bootstrap"}
    operations.current_config_data.return_value = {"email_registration": {}, "chatgpt": {}}
    operations._resolve_proxy_scheme.return_value = "http://proxy.invalid:8080"
    operations.registration_network_preflight.return_value = {
        "ok": True,
        "proxy": "http://proxy.invalid:8080",
    }
    operations.validate_config.return_value = None
    operations._ensure_mailbox_account.return_value = mailbox
    operations.think_stage.return_value = None
    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None),
        proxy="http://proxy.invalid:8080",
        mailbox=mailbox,
        config={"email_registration": {}, "chatgpt": {}},
        operations=operations,
        persistence=persistence,
    )
    workflow._run_stage = lambda _state, _label, handler: handler()

    with patch("sms_tool.sentinel.sentinel_backend", return_value="node_sdk"), \
         patch("sms_tool.mailbox_service.MailboxService.create", return_value=Mock()), \
         patch("sms_tool.registration_handlers._new_registration_session", return_value=Mock()), \
         patch("sms_tool.registration_handlers._apply_protocol_fingerprint"):
        workflow._bootstrap()

    assert workflow.runtime.username == "bootstrap@example.com"
    assert workflow.runtime.device_id == "device-bootstrap"
    operations.registration_network_preflight.assert_called_once()


def test_programming_error_inside_identity_stage_is_classified_as_internal():
    operations = _operations()
    operations.runtime_config_scope.return_value = nullcontext()
    operations._failure_result.side_effect = lambda error, **_kwargs: {
        "success": False,
        "error": error,
        "failure_class": "internal" if "NameError" in error else "unknown",
        "retryable": False,
    }
    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *_: None),
        config={"registration": {}},
        operations=operations,
    )
    workflow._bootstrap = lambda: workflow._run_stage(
        RegistrationState.IDENTITY_READY,
        "identity",
        lambda: (_ for _ in ()).throw(NameError("missing callback")),
    )
    workflow._close_sessions = Mock()

    result = workflow.run()

    assert result["failure_class"] == "internal"
    assert "identity_ready_internal:NameError" in result["error"]
