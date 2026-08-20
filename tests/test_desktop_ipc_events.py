from sms_tool.desktop_ipc import normalize_stage_event


def test_stage_event_normalizer_fills_uniform_contract():
    event = normalize_stage_event({"state": "failed", "message": "x", "stage": "auth", "account_terminal": True})
    assert event["domain"] == "workflow"
    assert event["status"] == "failed"
    assert event["detail"] == "x"
    assert event["duration_ms"] == 0
    assert event["last_failed_stage"] == ""
    assert event["batch_terminal"] is False
