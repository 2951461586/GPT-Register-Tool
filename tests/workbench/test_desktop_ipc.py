import json
from types import SimpleNamespace

import pytest

from sms_tool.desktop_ipc import IPC_PREFIX, emit_result
from sms_tool import cli


def test_emit_result_uses_versioned_single_line_contract(capsys):
    emit_result({"ok": True, "value": "中文"}, enabled=True)

    line = capsys.readouterr().out.strip()
    assert line.startswith(IPC_PREFIX)
    envelope = json.loads(line[len(IPC_PREFIX):])
    assert envelope["schema"] == "smsworkbench.ipc.v2"
    assert envelope["version"] == 2
    assert envelope["type"] == "result"
    assert envelope["terminal"] is True
    assert envelope["sequence"] == 1
    assert envelope["run_id"]
    assert envelope["timestamp_ms"] > 0
    assert envelope["payload"] == {"ok": True, "value": "中文"}


def test_emit_result_preserves_normal_cli_json(capsys):
    emit_result({"ok": True}, enabled=False)

    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_emit_result_redacts_sensitive_values(capsys):
    emit_result({"access_token": "at-visible-prefix", "totp_secret": "totp-visible-prefix"}, enabled=True)
    output = capsys.readouterr().out
    assert "at-visible-prefix" not in output
    assert "totp-visible-prefix" not in output


def test_emit_result_keeps_token_presence_flags(capsys):
    emit_result({
        "has_access_token": True,
        "has_refresh_token": True,
        "access_token_present": True,
        "refresh_token_present": True,
    }, enabled=True)

    line = capsys.readouterr().out.strip()
    envelope = json.loads(line[len(IPC_PREFIX):])
    payload = envelope["payload"]
    assert payload["has_access_token"] == "[REDACTED]"
    assert payload["has_refresh_token"] == "[REDACTED]"
    assert payload["access_token_present"] is True
    assert payload["refresh_token_present"] is True


def test_emit_result_keeps_at_probe_status_code(capsys):
    emit_result({"at_probe_status_code": "401"}, enabled=True)

    line = capsys.readouterr().out.strip()
    envelope = json.loads(line[len(IPC_PREFIX):])
    assert envelope["payload"]["at_probe_status_code"] == "401"


def test_view_inbox_failure_uses_desktop_ipc_envelope(monkeypatch, capsys):
    import sms_tool.codex_oauth as codex_oauth
    import sms_tool.mailbox as mailbox
    import sms_tool.session_refresh as session_refresh

    monkeypatch.setattr(session_refresh, "_load_seed_session", lambda **_: ({}, ""))
    monkeypatch.setattr(codex_oauth, "_mailbox_from_data", lambda _: None)
    monkeypatch.setattr(mailbox, "_mailbox_from_config", lambda _: None)
    monkeypatch.setattr(cli, "_mailbox_from_explicit_args", lambda _: None)
    args = SimpleNamespace(
        desktop_ipc=True,
        email="missing@example.com",
        session_file="",
        remail_token="",
    )

    with pytest.raises(SystemExit) as exit_info:
        cli._view_inbox(args)

    assert exit_info.value.code == 2
    line = capsys.readouterr().out.strip()
    assert line.startswith(IPC_PREFIX)
    envelope = json.loads(line[len(IPC_PREFIX):])
    assert envelope["payload"] == {
        "ok": False,
        "email": "missing@example.com",
        "error": "missing_mailbox_credentials",
    }
