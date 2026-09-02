"""Guards for the SQLite transaction semantics of the account store (round 6).

A round-6 finding claimed ``upsert_account`` performed a rename ``UPDATE`` and
the account ``INSERT`` in **two independent transactions**, so a failure between
them would leave "email renamed but account not written".

That is **wrong**, and this module exists so nobody re-introduces it.
Measured 2026-09-02 against the real ``_connect()``:

- ``sqlite3.connect(path)`` uses the default ``isolation_level = ''``
  (implicit-BEGIN mode): the first DML opens a transaction, ``commit()`` closes it.
- ``store/normalize.py:_resolve_account_email`` runs its rename ``UPDATE`` on the
  **same connection** the caller later uses for the INSERT, and there is no
  ``commit()`` in between.
- Empirically: rename, raise, close without commit -> the rename is **rolled back**.

The guard that matters is therefore not "wrap it in BEGIN IMMEDIATE" but
"do not switch the connection to autocommit". Passing ``isolation_level=None``
to ``sqlite3.connect`` would silently turn the two statements into two
transactions and create exactly the partial-write the finding described.
"""
import inspect
import shutil
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from sms_tool.store import connection
from sms_tool.store.connection import reset_database_init_cache
from sms_tool.store.constants import SCHEMA_VERSION
from sms_tool.store.normalize import _resolve_account_email


class _SqliteCase(unittest.TestCase):
    def make_db(self, seeded=True):
        """Create a throwaway database.

        Cleanup order matters on Windows: SQLite holds the file open, so the
        connection must be closed before the directory is removed. ``addCleanup``
        is LIFO, therefore the rmtree is registered first and runs last.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "probe.db"
        if seeded:
            conn = sqlite3.connect(str(path))
            try:
                conn.execute("CREATE TABLE accounts (email TEXT UNIQUE, plan TEXT)")
                # Stored with different casing than the caller will pass, so
                # _resolve_account_email takes the rename branch (existing != canonical).
                conn.execute("INSERT INTO accounts VALUES ('OldTag@Example.com', 'free')")
                conn.commit()
            finally:
                conn.close()
        return path

    def open_db(self, path, close=True):
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        if close:
            self.addCleanup(conn.close)
        return conn


class ImplicitTransactionTests(_SqliteCase):
    def test_connect_does_not_use_autocommit(self):
        """``isolation_level=None`` would split one logical write into two txns."""
        conn = self.open_db(self.make_db())
        # Mirror _connect(): no isolation_level argument at all.
        self.assertEqual(conn.isolation_level, "")
        self.assertNotIn("isolation_level", inspect.getsource(connection._connect))

    def test_dml_opens_transaction_implicitly(self):
        conn = self.open_db(self.make_db())
        self.assertFalse(conn.in_transaction)
        conn.execute("UPDATE accounts SET plan=?", ("plus",))
        self.assertTrue(conn.in_transaction)


class UpsertAtomicityTests(_SqliteCase):
    """The rename and the insert must live or die together."""

    def test_rename_rolls_back_when_the_following_write_fails(self):
        path = self.make_db()
        conn = self.open_db(path, close=False)
        try:
            # Exactly what upsert_account does first.
            resolved = _resolve_account_email(conn, "oldtag@example.com")
            self.assertEqual(resolved, "oldtag@example.com")  # rename branch taken
            raise RuntimeError("simulated failure before INSERT")
        except RuntimeError:
            pass
        finally:
            conn.close()  # no commit -> everything must roll back

        check = self.open_db(path)
        emails = [row[0] for row in check.execute("SELECT email FROM accounts")]
        self.assertEqual(emails, ["OldTag@Example.com"])

    def test_rename_commits_together_with_the_insert(self):
        path = self.make_db()
        conn = self.open_db(path, close=False)
        try:
            email = _resolve_account_email(conn, "oldtag@example.com")
            conn.execute(
                "INSERT INTO accounts (email, plan) VALUES (?, ?) "
                "ON CONFLICT(email) DO UPDATE SET plan=excluded.plan",
                (email, "plus"),
            )
            conn.commit()
        finally:
            conn.close()

        check = self.open_db(path)
        rows = [dict(r) for r in check.execute("SELECT email, plan FROM accounts")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "oldtag@example.com")
        self.assertEqual(rows[0]["plan"], "plus")


class InitCacheResetTests(_SqliteCase):
    """reset_database_init_cache() is the only escape hatch from the memo.

    Round 6 found it had zero callers anywhere -- production or tests -- meaning
    it had never once been exercised. These are its first tests.
    """

    def test_memo_suppresses_a_second_init_on_the_same_path(self):
        path = self.make_db(seeded=False)
        connection.init_database(path)
        self.assertTrue(path.exists())
        path.unlink()
        # memoized -> must NOT recreate
        connection.init_database(path)
        self.assertFalse(path.exists())

    def test_reset_cache_forces_the_next_init_to_run(self):
        path = self.make_db(seeded=False)
        connection.init_database(path)
        path.unlink()
        reset_database_init_cache()
        connection.init_database(path)
        self.assertTrue(path.exists(), "reset must invalidate the memo")

    def test_force_flag_bypasses_the_memo_without_resetting_it(self):
        path = self.make_db(seeded=False)
        connection.init_database(path)
        path.unlink()
        connection.init_database(path, force=True)
        self.assertTrue(path.exists())

    def test_init_stamps_the_schema_version(self):
        """Round 6: the database used to carry no version marker at all."""
        path = self.make_db(seeded=False)
        connection.init_database(path)
        check = self.open_db(path)
        stamped = check.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(stamped, SCHEMA_VERSION)
        self.assertGreaterEqual(stamped, 1)

    def test_failed_init_is_retried_and_then_stamped(self):
        """The memo only records successes, so a failed init runs again."""
        path = self.make_db(seeded=False)
        real_connect = sqlite3.connect
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("simulated init failure")
            return real_connect(*args, **kwargs)

        with unittest.mock.patch.object(sqlite3, "connect", flaky):
            with self.assertRaises(sqlite3.OperationalError):
                connection.init_database(path)
        connection.init_database(path)
        check = self.open_db(path)
        self.assertEqual(
            check.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
        )

    def test_memo_is_keyed_by_path_so_a_new_path_initializes_afresh(self):
        first = self.make_db(seeded=False)
        second = self.make_db(seeded=False)
        connection.init_database(first)
        connection.init_database(second)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())


if __name__ == "__main__":
    unittest.main()
