"""The autouse sandbox must also cover ``storage.sqlite_path`` (the real runtime.json sets it)."""

from __future__ import annotations

from pathlib import Path

from sms_tool import storage
from sms_tool.store import connection

REAL_DB = Path(__file__).resolve().parents[2] / "runtime" / "accounts.sqlite3"


def test_configured_real_database_path_is_redirected_into_the_sandbox(monkeypatch):
    monkeypatch.setattr(
        connection, "current_config_data", lambda: {"storage": {"sqlite_path": "runtime/accounts.sqlite3"}}
    )
    resolved = Path(storage.database_path()).resolve()
    assert resolved != REAL_DB.resolve()
    assert resolved.name == "accounts.sqlite3"


def test_a_test_owned_sqlite_path_is_left_alone(monkeypatch, tmp_path):
    own = tmp_path / "own" / "x.sqlite3"
    monkeypatch.setattr(connection, "current_config_data", lambda: {"storage": {"sqlite_path": str(own)}})
    assert Path(storage.database_path()).resolve() == own.resolve()