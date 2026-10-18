"""Incremental account reads for the desktop resident channel.

The full account payload is ~623 KB for 434 rows and the desktop re-reads it
after every saved session. `read_accounts_delta` keeps the full read available
while adding the two things an incremental refresh needs: the rows whose
`updated_at` moved past a cursor, and the complete id set so a row deleted by
another process can be dropped without a second round trip.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from sms_tool import desktop_serve
from sms_tool.desktop_read import read_accounts_delta
from sms_tool.storage import upsert_account


def _config(tmp_path: Path) -> dict:
    return {
        "chatgpt": {},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "runtime": {"directory": str(tmp_path)},
    }


def _seed(tmp_path: Path, email: str) -> None:
    session = {"email": email, "success": True, "access_token": "at-" + email}
    path = tmp_path / f"session_{email}.json"
    path.write_text(json.dumps(session), encoding="utf-8")
    assert upsert_account(session, json_path=str(path), runtime_config=_config(tmp_path))


def _bump(tmp_path: Path, email: str, delta: int = 5) -> None:
    database = sqlite3.connect(tmp_path / "accounts.sqlite3")
    try:
        database.execute(
            "UPDATE accounts SET updated_at = updated_at + ? WHERE email = ?",
            (delta, email),
        )
        database.commit()
    finally:
        database.close()


def test_full_read_still_carries_every_row_plus_ids(tmp_path):
    _seed(tmp_path, "a@example.test")
    _seed(tmp_path, "b@example.test")

    result = read_accounts_delta(_config(tmp_path))

    assert result["full"] is True
    assert len(result["accounts"]) == 2
    assert len(result["ids"]) == 2
    assert all(str(item).isdigit() for item in result["ids"])
    assert result["cursor"] > 0


def test_since_returns_only_changed_rows_and_the_full_id_set(tmp_path):
    _seed(tmp_path, "a@example.test")
    _seed(tmp_path, "b@example.test")
    full = read_accounts_delta(_config(tmp_path))

    _bump(tmp_path, "b@example.test")
    delta = read_accounts_delta(_config(tmp_path), since=full["cursor"])

    assert delta["full"] is False
    assert [row["email"] for row in delta["accounts"]] == ["b@example.test"]
    assert set(delta["ids"]) == set(full["ids"])


def test_row_deleted_elsewhere_disappears_from_the_id_set(tmp_path):
    _seed(tmp_path, "a@example.test")
    _seed(tmp_path, "b@example.test")
    full = read_accounts_delta(_config(tmp_path))

    database = sqlite3.connect(tmp_path / "accounts.sqlite3")
    try:
        database.execute("DELETE FROM accounts WHERE email = ?", ("a@example.test",))
        database.commit()
    finally:
        database.close()
    delta = read_accounts_delta(_config(tmp_path), since=full["cursor"])

    assert delta["accounts"] == []
    assert len(delta["ids"]) == 1
    assert set(delta["ids"]).isdisjoint({"a@example.test"})


def test_resident_accounts_op_accepts_a_since_cursor(tmp_path):
    _seed(tmp_path, "a@example.test")
    config = _config(tmp_path)
    full = read_accounts_delta(config)

    with patch.object(desktop_serve, "load_runtime_config", return_value=config):
        payload = desktop_serve._payload_for("accounts", {"since": full["cursor"]})

    assert payload["ok"] is True
    assert payload["full"] is False
    assert payload["accounts"] == []
    assert payload["ids"] == full["ids"]


def test_resident_accounts_op_ignores_a_boolean_cursor(tmp_path):
    _seed(tmp_path, "a@example.test")
    config = _config(tmp_path)

    with patch.object(desktop_serve, "load_runtime_config", return_value=config):
        payload = desktop_serve._payload_for("accounts", {"since": True})

    # `True` is an int in Python; it must not be read as "changed since epoch 1".
    assert payload["full"] is True
    assert len(payload["accounts"]) == 1
