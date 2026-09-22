import json

from sms_tool import desktop_ipc
from sms_tool.registration_progress import RegistrationProgress
from sms_tool.sanitizer import account_reference
from sms_tool.commands.one_click import _emit_one_click_event


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
    assert payload["account_ref"] == account_reference("user@example.com")
    assert payload["stage"] == "email_otp_wait"


def test_payment_method_field_survives_event_sanitization(monkeypatch, capsys):
    monkeypatch.setenv(desktop_ipc.EVENT_ENV, "1")
    desktop_ipc.emit_event({"domain": "payment", "payment_method": "qris", "method": "qris", "stage": "checkout"})
    payload = json.loads(capsys.readouterr().out.strip()[len(desktop_ipc.EVENT_PREFIX):])["payload"]
    assert payload["method"] == "qris"
    assert payload["payment_method"] == "qris"


def test_one_click_sms_event_contains_terminal_failure_class(monkeypatch, capsys):
    monkeypatch.setenv(desktop_ipc.EVENT_ENV, "1")
    _emit_one_click_event(
        run_id="batch:acct",
        batch_id="batch",
        stage="failed",
        status="failed",
        email="user@example.com",
        detail="email_otp_poll_timeout",
        account_terminal=True,
        failure_class="mailbox",
    )
    payload = json.loads(capsys.readouterr().out.strip()[len(desktop_ipc.EVENT_PREFIX):])["payload"]
    assert payload["domain"] == "one_click_sms"
    assert payload["account_ref"] == account_reference("user@example.com")
    assert payload["failure_class"] == "mailbox"
    assert payload["account_terminal"] is True


def test_mailboxes_skipped_event_is_emitted_with_masked_list(monkeypatch, capsys):
    """The backend's pre-attempt drop must surface to the desktop grid.

    Before 2026-09-19 the skip lived only in the console line, so a row the
    backend never attempted kept its stale pre-batch state in the grid.  The
    event carries the masked list so the WPF side can refresh those rows.
    """
    from sms_tool import batch_runner

    monkeypatch.setenv(desktop_ipc.EVENT_ENV, "1")
    batch_runner._emit_mailboxes_skipped(
        ["alpha.one@icloud.com", "beta.two@icloud.com"], reason="dead_end_or_quarantined"
    )
    out = capsys.readouterr().out.strip()
    assert out.startswith(desktop_ipc.EVENT_PREFIX)
    payload = json.loads(out[len(desktop_ipc.EVENT_PREFIX):])["payload"]
    assert payload["stage"] == "mailboxes_skipped"
    assert payload["skipped_count"] == 2
    assert payload["reason"] == "dead_end_or_quarantined"
    # masked, never the raw addresses
    assert "alpha.one@icloud.com" not in out
    assert "beta.two@icloud.com" not in out
    assert payload["detail"]


def test_mailboxes_skipped_is_silent_without_desktop_events(monkeypatch, capsys):
    from sms_tool import batch_runner

    monkeypatch.delenv(desktop_ipc.EVENT_ENV, raising=False)
    batch_runner._emit_mailboxes_skipped(["a@b.com"], reason="already_registered")
    assert capsys.readouterr().out == ""
