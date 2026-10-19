"""The database-path seam: one provider, no reverse import into the shell.

Why this exists
---------------
``store/connection.py`` used to resolve the SQLite file through
``sms_tool.storage.database_path`` -- a reverse import into the back-compat
shell, kept alive solely so 28 test sites could ``patch.object(storage,
"database_path", ...)`` and have internal callers observe it. A test convenience
had become an edge in the production import graph, and it was the only part of
that graph which existed *because of* the tests.

The seam is now explicit (``set_database_path_provider`` /
``database_path_override``). These tests pin the behaviour and the removal:
the schema memo must key on the provider's path, nested overrides must unwind,
and no module under ``store/`` may import the ``sms_tool.storage`` shell again.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

from sms_tool.store import connection

STORE_DIR = Path(__file__).resolve().parents[2] / "sms_tool" / "store"


def test_without_a_provider_the_configured_resolver_is_used():
    """The suite installs a session-wide sandbox provider, so clear it explicitly."""
    previous = connection.database_path_provider()
    connection.set_database_path_provider(None)
    try:
        assert connection.database_path().name == "accounts.sqlite3"
    finally:
        connection.set_database_path_provider(previous)


def test_override_redirects_every_call_and_restores_on_exit(tmp_path):
    ambient = connection.database_path_provider()
    target = tmp_path / "override.sqlite3"
    with connection.database_path_override(target):
        assert connection.database_path() == target
        conn = connection._connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    assert connection.database_path_provider() is ambient
    assert connection.database_path() != target


def test_nested_overrides_unwind_in_order(tmp_path):
    ambient = connection.database_path_provider()
    outer = tmp_path / "outer.sqlite3"
    inner = tmp_path / "inner.sqlite3"
    with connection.database_path_override(outer):
        assert connection.database_path() == outer
        with connection.database_path_override(inner):
            assert connection.database_path() == inner
        assert connection.database_path() == outer
    assert connection.database_path_provider() is ambient


def test_the_schema_memo_keys_on_the_provider_path(tmp_path):
    target = tmp_path / "memo.sqlite3"
    with connection.database_path_override(target):
        key = connection._init_cache_key()
        assert key == os.path.normcase(str(target.resolve()))
        connection.init_database()
        assert target.is_file()


def test_init_database_creates_the_schema_at_the_override_path(tmp_path):
    target = tmp_path / "schema.sqlite3"
    with connection.database_path_override(target):
        connection.init_database()
        conn = connection._connect()
        try:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    assert {"accounts", "registration_checkpoints", "registration_audit"} <= tables


def test_store_does_not_import_the_storage_shell():
    """The reverse dependency must not come back as a patch convenience."""
    offenders: list[str] = []
    for path in sorted(STORE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "sms_tool.storage" or (node.level >= 2 and module == "storage"):
                    offenders.append(f"{path.name}:{node.lineno} -> {module}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.name}:{node.lineno} -> {alias.name}"
                    for alias in node.names
                    if alias.name == "sms_tool.storage"
                )
    assert offenders == [], "store/ reached back into the sms_tool.storage shell: " + ", ".join(offenders)
