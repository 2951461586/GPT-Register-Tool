import json
import logging

from sms_tool.logging_setup import CorrelatedJsonFormatter
from sms_tool.registration_progress import RegistrationProgress, registration_quality_metrics
from sms_tool.telemetry import current_run_id


def test_logs_carry_command_and_run_ids_without_proxy_credentials(monkeypatch):
    monkeypatch.setenv("SMS_TOOL_COMMAND_ID", "test-command")
    token = current_run_id.set("test-run")
    try:
        record = logging.LogRecord("test", logging.ERROR, "", 0, "url=%s", ("http://private:password@proxy.example:80",), None)
        data = json.loads(CorrelatedJsonFormatter().format(record))
        assert data["run_id"] == "test-run"
        assert data["command_id"] == "test-command"
        assert data["schema_version"] == 1
        assert "private:password" not in data["message"]
    finally:
        current_run_id.reset(token)


def test_test_records_are_excluded_from_quality_metrics():
    metrics = registration_quality_metrics([
        {"source": "test", "success": False},
        {"source": "live", "success": True},
    ])
    assert metrics["runs"] == 1
    assert metrics["success_rate"] == 1


def test_progress_persists_driver_even_without_a_result(tmp_path, monkeypatch):
    from sms_tool import registration_progress

    path = tmp_path / "progress.jsonl"
    monkeypatch.setattr(registration_progress, "runtime_file", lambda *a: path)
    progress = RegistrationProgress(driver="camoufox")
    progress.persist(None, "mailbox_endpoint_unavailable")
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["registration_driver"] == "camoufox"
    assert row["failure_class"] == "mailbox"
    assert row["source"] == "test"
    assert row["run_id"] == progress.run_id
