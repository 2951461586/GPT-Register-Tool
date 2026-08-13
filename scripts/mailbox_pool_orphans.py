#!/usr/bin/env python3
"""Report and optionally prune orphan mailbox-pool entries.

An *orphan* is a mailbox-pool line whose email has no row in the accounts
database. Orphans are split by whether a matching ``sessions/session_*.json``
still exists, because a pool line with neither a database row nor a session
file is unrecoverable dead weight, while one that still has a session may be
resumable and is kept.

The command is read-only by default: it prints a reconciliation report. Pass
``--apply`` to remove only the no-session orphans, after backing up the pool
file. Paths are repo-relative and can be overridden for other layouts.

Replaces the ad-hoc root-level ``_check_status.py`` / ``_cleanup_orphans.py``
scratch scripts, which hardcoded absolute paths and rewrote the pool file with
no dry-run or backup.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _pool_email(line: str) -> str:
    """Extract the lowercased email from one mailbox-pool line.

    The committed format is ``email----secret...``; ``|`` and whitespace are
    tolerated as alternate separators so older pool files still parse.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    head = stripped.split("----", 1)[0].split("|", 1)[0]
    return head.split()[0].strip().lower() if head.split() else ""


def _db_emails(database: Path, domain: str) -> set[str]:
    if not database.exists():
        return set()
    conn = sqlite3.connect(database)
    try:
        if domain:
            rows = conn.execute(
                "SELECT LOWER(email) FROM accounts WHERE email LIKE ?",
                (f"%@{domain.lower()}",),
            )
        else:
            rows = conn.execute("SELECT LOWER(email) FROM accounts")
        return {str(row[0]).strip().lower() for row in rows if row[0]}
    finally:
        conn.close()


def _session_stems(sessions_dir: Path) -> list[str]:
    if not sessions_dir.is_dir():
        return []
    return [path.name for path in sessions_dir.glob("*.json")]


def _has_session(email: str, session_files: list[str]) -> bool:
    # Session filenames encode a normalized local-part; match on that stem the
    # same way the historical script did (drop the domain and a ``+tag``).
    base = email.replace("+oai01", "oai01").split("@", 1)[0]
    return any(base and base in name for name in session_files)


def reconcile(
    database: Path,
    sessions_dir: Path,
    pool_file: Path,
    *,
    domain: str,
) -> dict[str, object]:
    lines = pool_file.read_text(encoding="utf-8-sig").splitlines(keepends=True) if pool_file.exists() else []
    pool_emails = {email for line in lines if (email := _pool_email(line)) and (not domain or email.endswith("@" + domain.lower()))}
    db = _db_emails(database, domain)
    session_files = _session_stems(sessions_dir)

    orphans = sorted(pool_emails - db)
    with_session = [email for email in orphans if _has_session(email, session_files)]
    no_session = sorted(set(orphans) - set(with_session))
    return {
        "pool_file": str(pool_file),
        "domain_filter": domain or "(all)",
        "pool_total": len(pool_emails),
        "in_database": len(pool_emails & db),
        "orphans": len(orphans),
        "orphans_with_session": sorted(with_session),
        "orphans_no_session": no_session,
    }


def prune(pool_file: Path, remove_emails: set[str]) -> dict[str, object]:
    if not remove_emails or not pool_file.exists():
        return {"removed": 0, "remaining": None, "backup": None}
    lines = pool_file.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = pool_file.with_name(f"{pool_file.name}.pre_orphan_prune_{stamp}")
    shutil.copy2(pool_file, backup)
    kept = [line for line in lines if _pool_email(line) not in remove_emails]
    pool_file.write_text("".join(kept), encoding="utf-8")
    return {"removed": len(lines) - len(kept), "remaining": len(kept), "backup": str(backup)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "runtime" / "accounts.sqlite3")
    parser.add_argument("--sessions-dir", type=Path, default=PROJECT_ROOT / "sessions")
    parser.add_argument("--pool-file", type=Path, default=PROJECT_ROOT / "mailbox_tokens.txt")
    parser.add_argument("--domain", default="", help="restrict to one email domain, e.g. icloud.com")
    parser.add_argument("--apply", action="store_true", help="remove no-session orphans; default is a dry-run report")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    result = reconcile(args.database, args.sessions_dir, args.pool_file, domain=args.domain)
    result["dry_run"] = not args.apply
    if args.apply:
        result["prune"] = prune(args.pool_file, set(result["orphans_no_session"]))

    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
