from sms_tool.registration_progress import registration_quality_metrics
from sms_tool.session_builder import build_session_file


def test_quality_metrics_reports_auth_session_and_retries():
    rows = [
        {"success": True, "events": [
            {"stage": "auth_session", "duration_ms": 100},
            {"stage": "auth_flow_retry", "duration_ms": 20},
        ]},
        {"success": False, "events": [{"stage": "auth_session", "duration_ms": 300}]},
    ]
    metrics = registration_quality_metrics(rows)
    assert metrics["runs"] == 2
    assert metrics["failure_count"] == 1
    assert metrics["retry_count"] == 1
    assert metrics["auth_session"]["average_ms"] == 200.0
    assert metrics["auth_session"]["p95_ms"] == 300.0


def test_session_builder_normalizes_success_state():
    result = build_session_file({"email": "a@example.com", "success": True, "access_token": "at"})
    assert result["success"] is True
    assert result["status"] == "registered"
    assert result["registration_state"] == "active"
