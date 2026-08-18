import json

from sms_tool.account_lifecycle import AccountDeleteRequest, AccountLifecycle


def test_account_lifecycle_deletes_row_and_archives_session(tmp_path):
    database = tmp_path / "accounts.sqlite3"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session = sessions / "session_delete.json"
    session.write_text(json.dumps({"email": "delete@example.com"}), encoding="utf-8")
    config = {
        "chatgpt": {},
        "storage": {"sqlite_path": str(database)},
        "output": {"directory": str(sessions)},
        "protocol_payments": {"matrix": {"cells": []}},
    }
    from sms_tool import storage
    storage.upsert_account({"email": "delete@example.com", "access_token": "at"}, runtime_config=config)
    result = AccountLifecycle(config).delete(AccountDeleteRequest("delete@example.com"))
    assert result.removed_database_rows == 1
    assert not session.exists()
    assert len(result.archived_sessions) == 1


def test_account_lifecycle_removes_all_supported_mailbox_line_shapes(tmp_path):
    pool = tmp_path / "mailboxes.txt"
    pool.write_text(
        "delete@example.com----password----refresh\n"
        "delete@example.com---token\n"
        "delete@example.com|provider|secret\n"
        "keep@example.com----password\n",
        encoding="utf-8",
    )
    config = {
        "chatgpt": {},
        "email_registration": {"pool_files": [str(pool)]},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "output": {"directory": str(tmp_path / "sessions")},
        "protocol_payments": {"matrix": {"cells": []}},
    }
    result = AccountLifecycle(config).delete(AccountDeleteRequest("delete@example.com"))
    assert result.removed_mailbox_lines == 3
    assert pool.read_text(encoding="utf-8") == "keep@example.com----password\n"


def test_account_lifecycle_delete_many_preserves_order_and_shared_mailbox_file(tmp_path):
    pool = tmp_path / "mailboxes.txt"
    pool.write_text(
        "first@example.com----password\n"
        "second@example.com----password\n"
        "keep@example.com----password\n",
        encoding="utf-8",
    )
    config = {
        "chatgpt": {},
        "email_registration": {"pool_files": [str(pool)]},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "output": {"directory": str(tmp_path / "sessions")},
        "protocol_payments": {"matrix": {"cells": []}},
    }

    results = AccountLifecycle(config).delete_many(
        [AccountDeleteRequest("first@example.com"), AccountDeleteRequest("second@example.com")],
        workers=2,
    )

    assert [result.email for result in results] == ["first@example.com", "second@example.com"]
    assert pool.read_text(encoding="utf-8") == "keep@example.com----password\n"
