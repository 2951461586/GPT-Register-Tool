from types import SimpleNamespace
from http.cookiejar import CookieJar
import time
from unittest.mock import Mock

import pytest

from sms_tool import mailbox, registration_checkpoint, storage
from sms_tool.mailbox_errors import MailboxEndpointUnavailableError
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_runtime import RegistrationRuntimeState
from sms_tool.registration_state import RegistrationStateMachine


class MemoryPersistence:
    def __init__(self, payload=None):
        self.row = {"state": "at_probe_transport_unknown", "payload": payload or {}}

    def get_checkpoint(self, *args, **kwargs):
        return self.row

    def save_checkpoint(self, email, state, payload, **kwargs):
        self.row = {"state": state, "payload": dict(payload)}

    def upsert_account(self, *args, **kwargs):
        pass


def workflow(monkeypatch, payload=None):
    from sms_tool.mailbox_service import MailboxService

    w = object.__new__(RegistrationEmailWorkflow)
    w.config = {}
    w.input_proxy = None
    w.input_mailbox = SimpleNamespace(email="resume@example.test", provider="icloud_url")
    w.runtime = RegistrationRuntimeState()
    w.persistence = MemoryPersistence(payload)
    w.machine = RegistrationStateMachine(lambda *args: None)
    w._operations = SimpleNamespace(
        validate_config=Mock(), _resolve_proxy_scheme=Mock(return_value=""),
        registration_network_preflight=Mock(return_value={"proxy": ""}),
        _ensure_mailbox_account=Mock(return_value=w.input_mailbox),
        _mailbox_snapshot=Mock(return_value={}), set_fingerprint_geo=Mock(),
        _snapshot_mailbox_message=Mock(), _sanitize_text=str,
    )
    w._run_stage = Mock()
    monkeypatch.setattr(MailboxService, "create", Mock())
    monkeypatch.setattr("sms_tool.paypal_proxy.infer_proxy_country", lambda value: "")
    return w


def test_bootstrap_preserves_token_checkpoint_and_skips_mailbox(monkeypatch):
    payload = {"registration_state": "at_probe_transport_unknown", "access_token": "synthetic"}
    w = workflow(monkeypatch, payload)
    w._bootstrap()
    assert w.persistence.row["payload"] == payload
    assert w._has_resume_checkpoint()
    w.r._snapshot_mailbox_message.assert_not_called()
    w._run_stage.assert_not_called()


@pytest.mark.parametrize("status", [404, 410])
def test_terminal_snapshot_stops_before_auth_or_otp(monkeypatch, status):
    w = workflow(monkeypatch)
    monkeypatch.setattr(mailbox.mailbox_icloud_url, "snapshot_icloud_url_messages",
                        Mock(side_effect=MailboxEndpointUnavailableError(status)))
    w.r._snapshot_mailbox_message = mailbox._snapshot_mailbox_message
    with pytest.raises(MailboxEndpointUnavailableError):
        w._bootstrap()
    w._run_stage.assert_not_called()


def test_transient_snapshot_remains_distinct_from_missing_mailbox(monkeypatch):
    w = workflow(monkeypatch)
    monkeypatch.setattr(mailbox.mailbox_icloud_url, "snapshot_icloud_url_messages",
                        Mock(side_effect=RuntimeError("HTTP 503")))
    assert mailbox._snapshot_mailbox_message(w.input_mailbox) == ""


def test_storage_rejects_checkpoint_downgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "database_path", lambda *args, **kwargs: tmp_path / "accounts.sqlite3")
    storage.save_registration_checkpoint("resume@example.test", "at_probe_pending", {"access_token": "synthetic"})
    storage.save_registration_checkpoint("resume@example.test", "mailbox_ready", {"access_token": ""})
    saved = storage.get_registration_checkpoint("resume@example.test")
    assert saved["state"] == "at_probe_pending"
    assert saved["payload"]["access_token"] == "synthetic"


def test_created_account_without_at_is_resumable():
    p = MemoryPersistence({"registration_state": "auth_session_pending", "create_ok": True})
    assert registration_checkpoint.load_resumable_checkpoint(p, "resume@example.test", {}) is not None


def session_payload(**overrides):
    return {
        "registration_state": "auth_session_pending", "create_ok": True,
        "session_recovery_started_at": int(time.time()), "session_recovery_attempts": 0,
        "session_cookies": [{"name": "fixture", "value": "synthetic", "domain": "auth.example.test",
                             "path": "/", "secure": True}], **overrides,
    }


@pytest.mark.parametrize("overrides,expected", [
    ({"session_recovery_attempts": 2}, "auth_session_recovery_exhausted"),
    ({"session_recovery_started_at": 1}, "auth_session_recovery_expired"),
    ({"session_cookies": []}, "auth_session_recovery_context_missing"),
    ({"session_recovery_attempts": "invalid"}, "auth_session_recovery_context_missing"),
])
def test_unusable_session_checkpoint_stops_without_new_signup(monkeypatch, overrides, expected):
    from sms_tool.registration_handlers import RegistrationAbort
    from sms_tool.registration_policy import registration_retry_decision

    w = workflow(monkeypatch, session_payload(**overrides))
    w._bootstrap()
    with pytest.raises(RegistrationAbort, match=expected):
        w._resume_post_create()
    w.r._snapshot_mailbox_message.assert_not_called()
    w._run_stage.assert_not_called()
    assert not registration_retry_decision(expected).retryable


def test_session_recovery_only_fetches_session_then_probes_and_finalizes(monkeypatch):
    from sms_tool import registration_handlers

    w = workflow(monkeypatch, session_payload())
    w._bootstrap()
    session = SimpleNamespace(cookies=SimpleNamespace(jar=CookieJar()))
    monkeypatch.setattr(registration_handlers, "_new_registration_session", Mock(return_value=session))
    w.r.openai_auth_headers = Mock(return_value={})
    w._set_outcome = Mock()
    w.obtain_oauth_refresh_token = Mock()
    w._run_stage = Mock(side_effect=lambda state, label, handler: None)
    w._resume_post_create()
    assert [call.args[0].value for call in w._run_stage.call_args_list] == ["auth_session", "access_token_probe", "finalize"]
    assert w.persistence.row["payload"]["session_recovery_attempts"] == 1
    assert list(session.cookies.jar)[0].secure is True
    assert list(session.cookies.jar)[0].domain == "auth.example.test"
    w.r._snapshot_mailbox_message.assert_not_called()


def test_session_recovery_does_not_restore_expired_cookie():
    session = SimpleNamespace(cookies=SimpleNamespace(jar=CookieJar()))
    payload = session_payload()
    payload["session_cookies"][0]["expires"] = 1
    registration_checkpoint.restore_session_cookies(session, payload)
    assert list(session.cookies.jar) == []


def test_checkpoint_roundtrip_keeps_session_identity_and_headers():
    state = RegistrationRuntimeState(username="resume@example.test", session_logging_id="original-session",
                                     device_id="original-device", base_headers={"user-agent": "original-agent"})
    payload = registration_checkpoint.build_checkpoint_payload(state, lambda: {})
    restored = RegistrationRuntimeState()
    registration_checkpoint.apply_resume_payload(restored, payload)
    assert restored.session_logging_id == "original-session"
    assert restored.device_id == "original-device"
    assert payload["auth_headers"]["user-agent"] == "original-agent"


def test_checkpoint_does_not_persist_an_unknown_password():
    state = RegistrationRuntimeState(password="generated-fixture", password_unknown=True)
    assert registration_checkpoint.build_checkpoint_payload(state, lambda: {})["password"] == ""


def test_first_post_create_checkpoint_excludes_an_unowned_password(monkeypatch):
    """建号成功后落 checkpoint：我们生成、但**用户没拥有**的密码不许写进去。

    集成层（真的跑 ``create_account`` + ``_persist_checkpoint``）与
    ``test_checkpoint_does_not_persist_an_unknown_password`` 的纯函数层互补：
    这里顺带钉住「建号路径**不会**把 ``password_unknown`` 抹掉再落库」。

    🔴 本用例原先靠手工置位 ``resume_email_verification`` 来间接得到
    ``password_unknown=True``（当时 ``create_account`` 里有一条
    ``password_unknown = resume_email_verification and ...`` 的装配）。该标志
    2026-09-16 已删除，装配语句随之消失 ⇒ 现在**直接**置位，语义反而更清楚。
    """
    w = workflow(monkeypatch)
    w.runtime.username = "unowned@example.test"
    w.runtime.password = "generated-fixture"
    w.runtime.password_unknown = True
    w.runtime.context = SimpleNamespace(explicit_password=False, password_from_storage=False)
    w._issue_sentinel = Mock(return_value=SimpleNamespace(token="", so_token=""))
    w.r.request_with_retry = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {}))
    w.r._auth_request_headers = Mock(return_value={})
    w.r.auth_impersonate = Mock(return_value="")
    w.r.think_stage = Mock()
    w.r._is_user_already_exists = Mock(return_value=False)
    w.r._create_account_continue_url = Mock(return_value="")
    w.r._follow_continue_url = Mock()
    w.create_account()
    assert w.persistence.row["state"] == "auth_session_pending"
    assert w.persistence.row["payload"]["password"] == ""
