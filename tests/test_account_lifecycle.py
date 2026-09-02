import json

from sms_tool.account_lifecycle import AccountDeleteRequest, AccountLifecycle


def test_account_lifecycle_deletes_row_and_archives_session(tmp_path):
    database = tmp_path / "accounts.sqlite3"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # Real session files are named `session_{email with '+' stripped}_{ts}.json`.
    session = sessions / "session_delete@example.com_1786415260.json"
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


def test_account_lifecycle_fuzzy_deletes_at_plus_alias(tmp_path):
    """The WPF shell normalizes ``@+`` to ``+@`` (lossy), so the backend
    must handle the case where the DB stores the original ``@+`` form but
    the delete request carries the normalized ``+@`` form."""
    pool = tmp_path / "mailboxes.txt"
    database = tmp_path / "accounts.sqlite3"
    # The mailbox file stores the ORIGINAL "@+" form.
    pool.write_text(
        "cierrariste7566@+oai01hotmail.com----password\n"
        "keep@example.com----password\n",
        encoding="utf-8",
    )
    config = {
        "chatgpt": {},
        "email_registration": {"pool_files": [str(pool)]},
        "storage": {"sqlite_path": str(database)},
        "output": {"directory": str(tmp_path / "sessions")},
        "protocol_payments": {"matrix": {"cells": []}},
    }
    from sms_tool import storage
    # Database also stores the ORIGINAL "@+" form.
    storage.upsert_account(
        {"email": "cierrariste7566@+oai01hotmail.com", "access_token": "at"},
        runtime_config=config,
    )
    # But the delete request carries the NORMALIZED "+@" form (what WPF sends).
    result = AccountLifecycle(config).delete(
        AccountDeleteRequest("cierrariste7566+oai01@hotmail.com"),
    )
    assert result.removed_database_rows == 1
    assert result.removed_mailbox_lines == 1
    assert pool.read_text(encoding="utf-8") == "keep@example.com----password\n"


def test_account_lifecycle_fuzzy_match_does_not_over_delete(tmp_path):
    """Ensure the fuzzy match doesn't accidentally delete an unrelated account
    that merely shares a local-part prefix."""
    database = tmp_path / "accounts.sqlite3"
    config = {
        "chatgpt": {},
        "storage": {"sqlite_path": str(database)},
        "output": {"directory": str(tmp_path / "sessions")},
        "protocol_payments": {"matrix": {"cells": []}},
    }
    from sms_tool import storage
    storage.upsert_account(
        {"email": "cierrariste7566@+oai01hotmail.com", "access_token": "at"},
        runtime_config=config,
    )
    storage.upsert_account(
        {"email": "cierrariste7566other@gmail.com", "access_token": "at2"},
        runtime_config=config,
    )
    # Delete using the normalized form — should only match the first account.
    result = AccountLifecycle(config).delete(
        AccountDeleteRequest("cierrariste7566+oai01@hotmail.com"),
    )
    assert result.removed_database_rows == 1
    # The unrelated account must survive.
    import sqlite3
    with sqlite3.connect(database) as conn:
        rows = conn.execute("SELECT email FROM accounts").fetchall()
    assert any("cierrariste7566other@gmail.com" in r[0] for r in rows)


def test_account_lifecycle_session_archive_does_not_parse_every_session(tmp_path):
    """Deleting one account must archive only its sessions, not parse all of
    them. This guards the prefix-glob fix: before it, every delete opened all
    ~797 session files just to find the matching few."""

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # 200 unrelated session files, none for the target email.
    for i in range(200):
        (sessions / f"session_unrelated_{i}@example.org_17860000{i:03d}.json").write_text(
            json.dumps({"email": f"unrelated_{i}@example.org"}), encoding="utf-8"
        )
    # The one matching file, using the real `email with '+' stripped` scheme.
    match = sessions / "session_targettag@example.com_1786415260.json"
    match.write_text(json.dumps({"email": "target+tag@example.com"}), encoding="utf-8")

    config = {
        "chatgpt": {},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "output": {"directory": str(sessions)},
        "protocol_payments": {"matrix": {"cells": []}},
    }
    result = AccountLifecycle(config).delete(AccountDeleteRequest("target+tag@example.com"))
    assert result.archived_sessions == (str(sessions / "_deleted" / match.name),)
    # Unrelated files are untouched.
    assert len(list(sessions.glob("session_unrelated_*.json"))) == 200
