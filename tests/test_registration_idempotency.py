"""Durable cross-process idempotency for registration persistence.

The wrapper in ``sms_tool.commands.registration.persist_registration_result`` must
block a second persist of the same ``email|batch`` via ``PaymentOperationStore``,
so a restart or a concurrent process cannot double-write the session file, upsert
the row, or re-enqueue the health check.
"""

from __future__ import annotations

import types

import pytest

import sms_tool.storage as storage_mod
from sms_tool.commands.registration import persist_registration_result


@pytest.fixture(autouse=True)
def _patch_storage_audit(monkeypatch):
    # The core imports ``record_registration_audit`` from sms_tool.storage at call
    # time; neutralize it so the test exercises the durable boundary, not storage.
    monkeypatch.setattr(storage_mod, "record_registration_audit", lambda *a, **k: None)


def _make_ctx(tmp_path, upsert_calls):
    runtime_config = {"runtime": {"directory": str(tmp_path)}}

    def build_session_file(data):
        return {
            "email": data.get("email") or data.get("phone") or "unknown",
            "access_token": data.get("access_token") or "tok",
        }

    def upsert_account(session_data, json_path=None):
        upsert_calls.append(session_data.get("email"))
        return True

    def enqueue_post_registration_checks(session_data, source="registration", config=None):
        return []

    ctx = types.SimpleNamespace(
        runtime_config=runtime_config,
        build_session_file=build_session_file,
        upsert_account=upsert_account,
        enqueue_post_registration_checks=enqueue_post_registration_checks,
        record_registration_audit=lambda *a, **k: None,
        check_registered_promotions=lambda *a, **k: None,
        import_registered_accounts=lambda *a, **k: None,
    )
    return ctx


def _fresh_data(email="a@example.com"):
    return {
        "success": True,
        "email": email,
        "access_token": "tok",
        "phone": "+10000000000",
    }


def test_same_email_batch_is_persisted_once_across_durable_boundary(tmp_path):
    upsert_calls: list[str] = []
    ctx = _make_ctx(tmp_path, upsert_calls)
    args = types.SimpleNamespace(registration_batch_id="b1")
    base_dir = tmp_path / "sessions"
    base_dir.mkdir(parents=True, exist_ok=True)

    data1 = _fresh_data()
    out1 = persist_registration_result(args, data1, str(base_dir), ctx)
    assert out1["db_saved"] == 1
    assert out1["session_saved"] == 1
    assert "durable_conflict" not in out1

    # Fresh data object (no in-memory marker) for the same email+batch: the durable
    # guard must intercept it before the core runs, so upsert is NOT called again.
    data2 = _fresh_data()
    out2 = persist_registration_result(args, data2, str(base_dir), ctx)
    assert out2.get("durable_conflict") is True
    assert upsert_calls == ["a@example.com"], upsert_calls


def test_different_email_same_batch_both_persist(tmp_path):
    upsert_calls: list[str] = []
    ctx = _make_ctx(tmp_path, upsert_calls)
    args = types.SimpleNamespace(registration_batch_id="b1")
    base_dir = tmp_path / "sessions"
    base_dir.mkdir(parents=True, exist_ok=True)

    out1 = persist_registration_result(args, _fresh_data("x@e.com"), str(base_dir), ctx)
    out2 = persist_registration_result(args, _fresh_data("y@e.com"), str(base_dir), ctx)
    assert out1["db_saved"] == 1 and out2["db_saved"] == 1
    assert "durable_conflict" not in out1 and "durable_conflict" not in out2
    assert sorted(upsert_calls) == ["x@e.com", "y@e.com"]


def test_failed_registration_does_not_hard_block_inprocess_retry(tmp_path):
    # A failed (no access_token) registration leaves the durable record running so the
    # in-process finalization retry can still re-acquire the lock.
    upsert_calls: list[str] = []
    ctx = _make_ctx(tmp_path, upsert_calls)
    args = types.SimpleNamespace(registration_batch_id="b1")
    base_dir = tmp_path / "sessions"
    base_dir.mkdir(parents=True, exist_ok=True)

    failed = {"success": False, "email": "f@e.com", "error": "boom"}
    out = persist_registration_result(args, failed, str(base_dir), ctx)
    assert out["status"] == "complete"
    assert "durable_conflict" not in out
    # A later successful attempt for the same email+batch must still be allowed to persist.
    out2 = persist_registration_result(args, _fresh_data("f@e.com"), str(base_dir), ctx)
    assert out2["db_saved"] == 1
    assert "durable_conflict" not in out2
