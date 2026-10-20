"""Resident desktop-read RSS probe (run as a child process by the perf budget test).

Builds a 795-row database shaped like production, runs one full ``read_accounts``
(which parses and sanitizes every session row) and then ten ``read_accounts_delta``
calls -- the resident desktop channel's steady state -- and prints RSS in MB.

Self-contained on purpose: the budget must measure the real import graph, not a
test harness that already imported half of it.

    python tests/fixtures/resident_rss_probe.py <db-path>
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sms_tool import desktop_read  # noqa: E402
from sms_tool.store import connection  # noqa: E402

#: ~3.5 KB of session JSON per row; production sessions measure 5-20 KB, so this
#: is the conservative end of the per-row payload.
SESSION_BYTES = 3000


def build(db_path: Path) -> None:
    connection.set_database_path_provider(lambda *args, **kwargs: db_path)
    connection.init_database()
    raw = json.dumps(
        {
            "access_token": "at_" + "x" * SESSION_BYTES,
            "user": {"email": "resident@example.test"},
            "auth_session": {"oauth_refresh_token": "rt_" + "y" * 200},
            "quota": {"plan": "plus"},
        },
        separators=(",", ":"),
    )
    now = int(time.time())
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO accounts"
            " (email, success, status, session_token, access_token, paypal_ok, created_at, updated_at, raw_json)"
            " VALUES (?, 1, 'active', '', ?, 0, ?, ?, ?)",
            [(f"acct{i}@example.test", "at_" + "x" * 100, now - i, now - i, raw) for i in range(795)],
        )
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    db_path = Path(args[0])
    build(db_path)

    full = desktop_read.read_accounts()
    cursor = max(int(row.get("updated_at") or 0) for row in full)
    for _ in range(10):
        desktop_read.read_accounts_delta(since=cursor - 1)

    import psutil

    print(psutil.Process().memory_info().rss // 1048576)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
