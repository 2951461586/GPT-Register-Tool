import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from sms_tool import account_health_queue, storage
from sms_tool.account_health import (
    HealthState,
    liveness_health_result,
    plan_health_result,
)


def test_health_contract_normalizes_plan_and_redacts_credentials():
    result = plan_health_result(
        "User@Example.com",
        {
            "ok": True,
            "status_code": 200,
            "current_plan_type": "plus",
            "promotion_status": "已订阅·Plus",
            "access_token": "must-not-persist",
        },
    ).to_dict()

    assert result["email"] == "user@example.com"
    assert result["state"] == HealthState.HEALTHY.value
    assert result["plan_type"] == "plus"
    assert "access_token" not in result["details"]


def test_health_contract_records_recovery_chain_and_final_verification():
    result = liveness_health_result(
        "user@example.com",
        {"ok": False, "status": "token_invalid", "status_code": 401},
        recovery={
            "ok": True,
            "mode": "chatgpt_email_otp",
            "attempts": [{"mode": "oauth_refresh_token"}, {"mode": "web_session"}],
        },
        final_probe={"ok": True, "status": "active", "status_code": 200, "quota_status": "可用"},
    )

    assert result.ok is True
    assert result.recovered is True
    assert result.state == HealthState.RECOVERED.value
    assert result.attempts == ("oauth_refresh_token", "web_session")


def test_queue_deduplicates_active_jobs_and_persists_status():
    with tempfile.TemporaryDirectory() as tmp, patch.object(
        account_health_queue,
        "queue_path",
        return_value=Path(tmp) / "queue.json",
    ):
        first = account_health_queue.enqueue_account_health(
            "User@Example.com", "plan", auto_start=False
        )
        duplicate = account_health_queue.enqueue_account_health(
            "user@example.com", "plan", auto_start=False
        )
        summary = account_health_queue.process_account_health_jobs(
            handler=lambda job: {
                "ok": True,
                "email": job["email"],
                "check": job["kind"],
                "state": "healthy",
            }
        )

        assert duplicate["id"] == first["id"]
        assert summary["completed"] == 1
        saved = json.loads((Path(tmp) / "queue.json").read_text(encoding="utf-8"))
        assert saved[0]["status"] == "completed"
        assert saved[0]["attempts"] == 1


def test_queue_worker_concurrency_is_bounded():
    active = 0
    maximum = 0
    lock = threading.Lock()

    def handler(job):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {"ok": True, "email": job["email"], "check": job["kind"], "state": "healthy"}

    with tempfile.TemporaryDirectory() as tmp, patch.object(
        account_health_queue,
        "queue_path",
        return_value=Path(tmp) / "queue.json",
    ):
        for index in range(6):
            account_health_queue.enqueue_account_health(
                f"user{index}@example.com", "deep_liveness", auto_start=False
            )
        summary = account_health_queue.process_account_health_jobs(workers=2, handler=handler)

    assert summary["processed"] == 6
    assert maximum == 2


def test_queue_serializes_jobs_for_the_same_account():
    with tempfile.TemporaryDirectory() as tmp, patch.object(
        account_health_queue,
        "queue_path",
        return_value=Path(tmp) / "queue.json",
    ):
        account_health_queue.enqueue_account_health(
            "same@example.com", "plan", auto_start=False
        )
        account_health_queue.enqueue_account_health(
            "same@example.com", "deep_liveness", auto_start=False
        )
        first = account_health_queue.process_account_health_jobs(
            workers=2,
            handler=lambda job: {
                "ok": True,
                "email": job["email"],
                "check": job["kind"],
                "state": "healthy",
            },
        )
        second = account_health_queue.process_account_health_jobs(
            workers=2,
            handler=lambda job: {
                "ok": False,
                "email": job["email"],
                "check": job["kind"],
                "state": "token_invalid",
            },
        )

    assert first["processed"] == 1
    assert second["processed"] == 1
    assert second["completed"] == 1
    assert second["results"][0]["result"]["state"] == "token_invalid"


def test_pid_alive_never_emits_console_ctrl_events():
    """os.kill(pid, 0) aliases signal.CTRL_C_EVENT (which is 0) on Windows.

    Probing our own worker pid therefore broadcast a Ctrl+C to the process
    group. CI runners create step processes with CREATE_NEW_PROCESS_GROUP, so
    the current pid is also a group id and the event came straight back and
    aborted the pytest session with KeyboardInterrupt.
    """
    calls = []
    with patch.object(os, "kill", side_effect=lambda *args: calls.append(args)):
        assert account_health_queue._pid_alive(os.getpid()) is True

    if os.name == "nt":
        assert calls == []
    else:
        assert calls == [(os.getpid(), 0)]


def test_pid_alive_rejects_invalid_pids():
    assert account_health_queue._pid_alive(0) is False
    assert account_health_queue._pid_alive(-1) is False


def test_running_item_owned_by_current_process_is_not_recovered():
    with tempfile.TemporaryDirectory() as tmp, patch.object(
        account_health_queue,
        "queue_path",
        return_value=Path(tmp) / "queue.json",
    ):
        account_health_queue._write_unlocked(
            [
                {
                    "id": "job-owned-by-us",
                    "email": "owner@example.com",
                    "kind": "plan",
                    "status": "running",
                    "worker_pid": os.getpid(),
                    "updated_at": int(time.time()),
                    "attempts": 1,
                    "last_error": "",
                }
            ]
        )
        items = account_health_queue._load_unlocked()

    assert len(items) == 1
    assert items[0]["status"] == "running"
    assert items[0]["last_error"] == ""


def test_storage_persists_unified_health_result_without_token():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "accounts.sqlite3"
        with patch.object(storage, "database_path", return_value=db_path):
            assert storage.upsert_account(
                {"email": "health@example.com", "success": True, "access_token": "secret-at"}
            )
            assert storage.mark_account_health_result(
                "health@example.com",
                {
                    "ok": True,
                    "check": "plan",
                    "state": "healthy",
                    "plan_type": "plus",
                    "access_token": "must-not-persist-in-health",
                },
            )
            record = storage.get_account_record("health@example.com")
            raw = json.loads(record["raw_json"])

    assert record["plan_type"] == "plus"
    assert raw["account_health"]["checks"]["plan"]["state"] == "healthy"
    assert "access_token" not in raw["account_health"]["checks"]["plan"]


def test_deep_liveness_runs_light_probe_recovery_and_final_probe():
    records = [
        {"email": "recover@example.com", "access_token": "old-at", "raw_json": "{}"},
        {"email": "recover@example.com", "access_token": "new-at", "raw_json": "{}"},
    ]
    probes = [
        {"ok": False, "status": "token_invalid", "status_code": 401, "quota_status": "401失效"},
        {"ok": True, "status": "active", "status_code": 200, "quota_status": "可用"},
    ]
    with (
        patch("sms_tool.storage.get_account_record", side_effect=records),
        patch("sms_tool.storage.mark_quota_status", return_value=True),
        patch("sms_tool.storage.mark_account_health_result", return_value=True) as persist,
        patch("sms_tool.account_recovery.is_permanently_deactivated", return_value=False),
        patch(
            "sms_tool.account_recovery.relogin_codex_account",
            return_value={"ok": True, "mode": "chatgpt_email_otp"},
        ) as recover,
        patch("sms_tool.account_liveness.probe_account_liveness", side_effect=probes) as probe,
    ):
        result = account_health_queue._handle_job(
            {
                "id": "job-id",
                "email": "recover@example.com",
                "kind": "deep_liveness",
            }
        )

    assert result.ok is True
    assert result.recovered is True
    assert result.state == "recovered"
    assert probe.call_count == 2
    recover.assert_called_once()
    persist.assert_called_once()
