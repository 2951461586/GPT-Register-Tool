import json

from sms_tool import desktop_ipc
from sms_tool.registration_progress import RegistrationProgress


def test_emit_event_is_opt_in_and_sanitized(monkeypatch, capsys):
    monkeypatch.delenv(desktop_ipc.EVENT_ENV, raising=False)
    assert desktop_ipc.emit_event({"stage": "checkout"}) is False
    assert capsys.readouterr().out == ""

    monkeypatch.setenv(desktop_ipc.EVENT_ENV, "1")
    assert desktop_ipc.emit_event({"stage": "checkout", "access_token": "secret"}) is True
    line = capsys.readouterr().out.strip()
    assert line.startswith(desktop_ipc.EVENT_PREFIX)
    envelope = json.loads(line[len(desktop_ipc.EVENT_PREFIX):])
    assert envelope["schema"] == "smsworkbench.ipc.v2"
    assert envelope["version"] == 2
    assert envelope["type"] == "event"
    assert envelope["payload"]["stage"] == "checkout"
    assert envelope["payload"]["access_token"] == "[REDACTED]"


def test_registration_progress_emits_realtime_stage(monkeypatch, capsys):
    monkeypatch.setenv(desktop_ipc.EVENT_ENV, "1")
    progress = RegistrationProgress("user@example.com")
    progress.stage("email_otp_wait", detail="waiting")
    lines = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(lines[-1][len(desktop_ipc.EVENT_PREFIX):])["payload"]
    assert payload["domain"] == "registration"
    assert payload["account_ref"] == "user@example.com"
    assert payload["stage"] == "email_otp_wait"


def test_payment_method_field_survives_event_sanitization(monkeypatch, capsys):
    monkeypatch.setenv(desktop_ipc.EVENT_ENV, "1")
    desktop_ipc.emit_event({"domain": "payment", "payment_method": "qris", "method": "qris", "stage": "checkout"})
    payload = json.loads(capsys.readouterr().out.strip()[len(desktop_ipc.EVENT_PREFIX):])["payload"]
    assert payload["method"] == "qris"
    assert payload["payment_method"] == "qris"
