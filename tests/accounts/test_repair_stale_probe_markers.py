"""Tests for ``scripts/repair_stale_probe_markers.py``.

The script retires two duplicated facts from stored account payloads:

* ``at_probe_status_code`` — a derived cache the read path recomputes;
* the top-level ``token_probe`` — the same lane's record as ``quota.last_result``,
  refreshed only on a 200, which is how a 401 outlived the 200 that healed the
  account (measured 2026-09-04: five rows, while the read path still ranked by
  timestamp and displayed 200).

The invariant under test is that the cleanup never moves a verdict: a row whose
derived code would change must be skipped and reported.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.repair_stale_probe_markers import repair, strip_stale_probe_fields
from sms_tool.desktop.read import _access_token_probe_status_code


def _database(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "accounts.sqlite3"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT, json_path TEXT, raw_json TEXT)")
        for index, row in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO accounts (id, email, json_path, raw_json) VALUES (?,?,?,?)",
                (index, row["email"], str(row.get("json_path") or ""), json.dumps(row["payload"])),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _stored(path: Path, email: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        raw = conn.execute("SELECT raw_json FROM accounts WHERE email=?", (email,)).fetchone()[0]
    finally:
        conn.close()
    return json.loads(raw)


def _session(tmp_path: Path, payload: dict) -> Path:
    session_path = tmp_path / "session.json"
    session_path.write_text(json.dumps(payload), encoding="utf-8")
    return session_path


def _healthy_account_with_stale_401(tmp_path: Path) -> tuple[Path, Path, dict]:
    """The measured shape: a 200 quota probe next to a stale 401 copy."""
    payload = {
        "email": "stale@example.test",
        "at_probe_status_code": "401",
        "token_probe": {"ok": False, "status": "token_invalid", "status_code": 401, "updated_at": 100},
        "quota": {"updated_at": 200, "last_result": {"status": "active", "status_code": 200}},
        "quota_updated_at": 200,
    }
    session_path = _session(tmp_path, payload)
    database = _database(tmp_path, [{"email": payload["email"], "json_path": session_path, "payload": payload}])
    return database, session_path, payload


def test_dry_run_reports_the_duplicates_without_writing(tmp_path: Path):
    database, session_path, payload = _healthy_account_with_stale_401(tmp_path)
    original_file = session_path.read_text(encoding="utf-8")

    report = repair(database, apply=False)

    assert report["rows_scanned"] == 1
    assert report["db_rows_changed"] == 1
    assert report["session_files_changed"] == 1
    assert report["dropped"] == {"at_probe_status_code": 1, "token_probe": 1}
    assert _stored(database, payload["email"]) == payload
    assert session_path.read_text(encoding="utf-8") == original_file


def test_apply_drops_both_copies_and_keeps_the_derived_verdict(tmp_path: Path):
    database, session_path, payload = _healthy_account_with_stale_401(tmp_path)
    before = _access_token_probe_status_code(payload)

    report = repair(database, apply=True)

    assert report["skipped_verdict_changed"] == []
    stored = _stored(database, payload["email"])
    assert "at_probe_status_code" not in stored
    assert "token_probe" not in stored
    assert _access_token_probe_status_code(stored) == before == "200"

    session = json.loads(session_path.read_text(encoding="utf-8"))
    assert "at_probe_status_code" not in session
    assert "token_probe" not in session
    assert _access_token_probe_status_code(session) == "200"
    # The lane's own probe record is what the verdict is read from now.
    assert session["quota"]["last_result"]["status_code"] == 200


def test_row_whose_derived_verdict_would_change_is_skipped(tmp_path: Path):
    """The 401 copy must survive when dropping it would flip the answer.

    A legacy row can hold the *newer* verdict in ``token_probe`` and the older
    one in ``quota.last_result``. Removing the copy would then read 401 where
    the grid reads 200, so the script refuses the row and reports it.
    """
    payload = {
        "email": "newer-in-token-probe@example.test",
        "token_probe": {"status": "active", "status_code": 200, "updated_at": 500},
        "quota": {"updated_at": 100, "last_result": {"status": "token_invalid", "status_code": 401}},
    }
    database = _database(tmp_path, [{"email": payload["email"], "payload": payload}])
    assert _access_token_probe_status_code(payload) == "200"

    report = repair(database, apply=True)

    assert report["skipped_verdict_changed"] == [payload["email"]]
    assert report["db_rows_changed"] == 0
    stored = _stored(database, payload["email"])
    assert stored["token_probe"]["status_code"] == 200
    assert _access_token_probe_status_code(stored) == "200"


def test_probe_is_kept_when_it_is_the_only_record(tmp_path: Path):
    payload = {
        "email": "only-token-probe@example.test",
        "at_probe_status_code": "401",
        "token_probe": {"status": "token_invalid", "status_code": 401},
    }
    database = _database(tmp_path, [{"email": payload["email"], "payload": payload}])

    repair(database, apply=True)

    stored = _stored(database, payload["email"])
    assert "at_probe_status_code" not in stored
    assert stored["token_probe"]["status_code"] == 401
    assert _access_token_probe_status_code(stored) == "401"


def test_strip_helper_ignores_payloads_without_the_duplicates():
    assert strip_stale_probe_fields({}) == []
    assert strip_stale_probe_fields({"quota": {"last_result": {"status_code": 200}}}) == []
    data = {"at_probe_status_code": "200", "quota": {"last_result": {"status_code": 200}}, "token_probe": {}}
    assert strip_stale_probe_fields(data) == ["at_probe_status_code", "token_probe"]
    assert data == {"quota": {"last_result": {"status_code": 200}}}
