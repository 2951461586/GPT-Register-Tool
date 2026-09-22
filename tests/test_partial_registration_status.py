import json
from types import SimpleNamespace
from unittest.mock import Mock

from sms_tool import desktop_read, registration_retry_guard
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_retry_guard import RegistrationRetryGuard
from sms_tool.registration_runtime import RegistrationRuntimeState


def test_partial_verdict_survives_failed_recovery(tmp_path):
    guard = RegistrationRetryGuard({}, path=tmp_path / "guard.json")
    guard.mark_dead_end("half@example.test", reason="user_already_exists")
    for failure, error in [("network", "curl: (28) timeout"), ("account", "password_missing")]:
        guard.record("half@example.test", failure_class=failure, error=error)
        assert guard.check("half@example.test")["registration_status"] == "partial_registered"
    guard.record("half@example.test", success=True)
    assert guard.check("half@example.test")["registration_status"] == ""


def test_desktop_pool_reads_legacy_and_new_partial_verdicts(tmp_path, monkeypatch):
    monkeypatch.setattr(registration_retry_guard, "runtime_file", lambda cfg, name: tmp_path / name)
    path = tmp_path / "mailbox_tokens.txt"
    path.write_text("remail://half@example.test---fixture\nremail://fresh@example.test---fixture\n")
    guard = RegistrationRetryGuard({})
    guard.path.write_text(json.dumps({"half@example.test": {"dead_end": True}}))
    cfg = {"chatgpt": {}, "email_registration": {"token_file": str(path)}}
    rows = desktop_read.read_mailbox_pool(cfg, root_dir=tmp_path)["files"][0]["lines"]
    assert rows[0]["registration_status"] == "partial_registered"
    assert rows[0]["registration_eligible"] is False
    assert rows[1]["registration_status"] == "unknown"
    assert rows[1]["registration_eligible"] is True
    guard.record("half@example.test", success=True)
    rows = desktop_read.read_mailbox_pool(cfg, root_dir=tmp_path)["files"][0]["lines"]
    assert rows[0]["registration_status"] == "unknown"


def test_create_response_persists_partial_before_followup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(registration_retry_guard, "runtime_file", lambda cfg, name: tmp_path / name)
    monkeypatch.setenv("SMSWORKBENCH_EVENTS", "1")
    w = object.__new__(RegistrationEmailWorkflow)
    w.config = {}
    w.runtime = RegistrationRuntimeState(username="half@example.test", auth_base="https://auth.example.test")
    w._issue_sentinel = Mock(return_value=SimpleNamespace(token="", so_token=""))
    response = SimpleNamespace(status_code=400, json=lambda: {"error": {"code": "user_already_exists"}})

    def follow(*args, **kwargs):
        assert RegistrationRetryGuard({}).check(w.runtime.username)["registration_status"] == "partial_registered"

    w._operations = SimpleNamespace(
        request_with_retry=Mock(return_value=response), _auth_request_headers=Mock(return_value={}),
        auth_impersonate=Mock(return_value=""), _sanitize_text=str, think_stage=Mock(),
        _is_user_already_exists=lambda data: data["error"]["code"] == "user_already_exists",
        _follow_continue_url=Mock(side_effect=follow), _create_account_continue_url=Mock(return_value=""),
    )
    w.create_account()
    assert w.runtime.existing_account
    assert '"registration_status":"partial_registered"' in capsys.readouterr().out


def test_partial_filter_survives_database_lookup_failure(tmp_path, monkeypatch):
    from sms_tool import batch_runner

    monkeypatch.setattr(registration_retry_guard, "runtime_file", lambda cfg, name: tmp_path / name)
    monkeypatch.setattr(batch_runner, "CFG", {})
    monkeypatch.setattr(batch_runner, "get_account_records", Mock(side_effect=OSError("unavailable")))
    RegistrationRetryGuard({}).mark_dead_end("half@example.test")
    kept, registered, partial = batch_runner._drop_already_registered([
        SimpleNamespace(email="half@example.test"), SimpleNamespace(email="fresh@example.test")
    ])
    assert [row.email for row in kept] == ["fresh@example.test"]
    assert registered == []
    assert partial == ["half@example.test"]


def test_abort_result_carries_the_relogin_cause():
    """``user_already_exists`` outranks the re-login cause, so carry it separately.

    ``_registration_outcome`` prefers the create-account cause -- correctly, that
    *is* the real cause -- and the side effect is that the recovery lane's own
    outcome (and whether it spent an email code) vanished.  Measured 2026-09-15:
    ``existing_login`` appeared in 0 of 4377 ``registration_audit.detail_json``
    values.
    """
    w = object.__new__(RegistrationEmailWorkflow)
    w.runtime = RegistrationRuntimeState(username="half@example.test")
    w.runtime.existing_account = True
    w.runtime.existing_login_error = "existing_login_password_step_unknown"
    w.machine = Mock()
    w._operations = SimpleNamespace(
        _failure_result=Mock(return_value={"error": "existing_account_user_already_exists"})
    )

    result = w._abort_result("existing_account_user_already_exists")

    assert result["registration_state"] == "partial_registered"
    assert result["existing_login_error"] == "existing_login_password_step_unknown"


def test_abort_result_omits_the_relogin_cause_when_the_lane_never_ran():
    """No key means the lane never ran; an empty string would blur that."""
    w = object.__new__(RegistrationEmailWorkflow)
    w.runtime = RegistrationRuntimeState(username="fresh@example.test")
    w.machine = Mock()
    w._operations = SimpleNamespace(
        _failure_result=Mock(return_value={"error": "create_account_failed"})
    )

    result = w._abort_result("create_account_failed")

    assert "existing_login_error" not in result


def test_confirmed_account_takes_precedence_over_stale_partial_verdict():
    from sms_tool.registration_retry_guard import mailbox_registration_status

    assert mailbox_registration_status({"status": "registered"}, known_partial=True) == "registered"
    assert mailbox_registration_status({"status": "partial_registered"}) == "partial_registered"
    assert mailbox_registration_status({"registration_state": "partial_registered"}) == "partial_registered"
    assert mailbox_registration_status({}) == "unknown"
