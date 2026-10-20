"""Pins the SQLite journal-mode decision recorded in ``store/connection.py``.

2026-10-20 re-tested the 2026-09-01 deferral on a 795-row / 38.5 MB synthetic
database (audit-2026-09-01-round4 §11). With the production pattern -- one
connection per operation -- ``WAL`` is 1.3-1.5x SLOWER than ``delete`` and the
``delete`` mode saw zero ``database is locked`` under 2 writers + 4 readers.
WAL only wins once connections are reused (delete 105-117 vs WAL 500-563 tx/s),
so connection reuse is the lever, not the journal mode, and production volume
(< 1 tx/s averaged over a batch) is far below that crossover.

Why a test instead of only a comment: enabling WAL is not a one-line pragma.
It leaves ``-wal``/``-shm`` sidecars, and ``scripts/cleanup_invalid_accounts.py``
copies the database with ``shutil.copy2`` -- any content still sitting in the
WAL is silently absent from that backup. This module is the tripwire: turning
WAL on fails here, and the failure message says what else has to change with it.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sms_tool.store import connection


class JournalModeDecisionTests(unittest.TestCase):
    def make_db(self) -> Path:
        """A throwaway initialized database.

        Cleanup order matters on Windows: SQLite holds the file open, so every
        connection must be closed before the directory is removed. ``addCleanup``
        is LIFO, therefore the rmtree is registered first and runs last.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "journal.db"
        connection.init_database(path)
        return path

    def test_connections_open_the_database_in_delete_mode(self):
        path = self.make_db()
        conn = connection._connect(path=str(path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(
            mode,
            "delete",
            "WAL was re-measured on 2026-10-20 and lost on the production "
            "connection pattern (audit-2026-09-01-round4 §11). If you are "
            "enabling it deliberately, also make cleanup_invalid_accounts.py "
            "checkpoint (or use sqlite3 backup) before copying the file.",
        )

    def test_a_committed_write_leaves_no_wal_sidecar(self):
        """The backup path copies one file; a -wal sidecar would escape it."""
        path = self.make_db()
        conn = connection._connect(path=str(path))
        try:
            conn.execute(
                "INSERT INTO accounts (email, success, created_at, updated_at) VALUES ('journal@example.test', 1, 1, 1)"
            )
            conn.commit()
        finally:
            conn.close()
        sidecars = [suffix for suffix in ("-wal", "-shm") if Path(str(path) + suffix).exists()]
        self.assertEqual(sidecars, [], "journal_mode=delete must not leave WAL sidecars")

    def test_the_written_row_is_visible_to_a_new_connection(self):
        """Sanity anchor: delete mode commits are readable without a checkpoint."""
        path = self.make_db()
        conn = connection._connect(path=str(path))
        try:
            conn.execute(
                "INSERT INTO accounts (email, success, created_at, updated_at) VALUES ('visible@example.test', 1, 1, 1)"
            )
            conn.commit()
        finally:
            conn.close()
        check = sqlite3.connect(str(path))
        try:
            found = check.execute("SELECT COUNT(*) FROM accounts WHERE email='visible@example.test'").fetchone()[0]
        finally:
            check.close()
        self.assertEqual(found, 1)


if __name__ == "__main__":
    unittest.main()
