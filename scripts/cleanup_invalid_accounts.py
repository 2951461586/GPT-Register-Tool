#!/usr/bin/env python3
"""Archive terminal account rows and remove matching mailbox-pool entries.

The command is dry-run by default. Use ``--apply`` only after reviewing the
reported count. Unknown/network liveness results are deliberately retained.
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

from sms_tool.account_cleanup import select_removable_accounts


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_rows(database: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM accounts ORDER BY id")]
    finally:
        conn.close()


def _session_email(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(data.get("email") or "").strip().lower() if isinstance(data, dict) else ""


def _remove_pool_lines(path: Path, emails: set[str]) -> int:
    if not path.exists():
        return 0
    original = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    kept: list[str] = []
    removed = 0
    for line in original:
        email = line.strip().split("-", 1)[0].split("|", 1)[0].strip().lower()
        if email in emails:
            removed += 1
        else:
            kept.append(line)
    if removed:
        path.write_text("".join(kept), encoding="utf-8")
    return removed


def cleanup(database: Path, sessions_dir: Path, pool_files: list[Path], *, apply: bool) -> dict[str, object]:
    rows = _load_rows(database)
    selected = select_removable_accounts(rows)
    emails = {str(row.get("email") or "").strip().lower() for row in selected}
    result: dict[str, object] = {
        "database": str(database),
        "dry_run": not apply,
        "selected_count": len(selected),
        "reasons": {str(row.get("email")): str(row.get("cleanup_reason")) for row in selected},
        "removed_session_files": [],
        "removed_pool_lines": {},
    }
    if not apply or not emails:
        return result

    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = database.with_name(f"{database.name}.pre_cleanup_{stamp}")
    shutil.copy2(database, backup)
    archive = sessions_dir / f"_removed_account_cleanup_{stamp}"
    archive.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    try:
        conn.executemany("DELETE FROM accounts WHERE lower(email)=?", [(email,) for email in emails])
        conn.commit()
    finally:
        conn.close()
    moved: list[str] = []
    for path in sessions_dir.glob("session_*.json"):
        if _session_email(path) in emails:
            shutil.move(str(path), str(archive / path.name))
            moved.append(path.name)
    result["database_backup"] = str(backup)
    result["archive_directory"] = str(archive)
    result["removed_session_files"] = moved
    result["removed_pool_lines"] = {
        str(path): _remove_pool_lines(path, emails) for path in pool_files
    }
    return result


def main() -> int:
    root = _repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=root / "runtime" / "accounts.sqlite3")
    parser.add_argument("--sessions-dir", type=Path, default=root / "sessions")
    parser.add_argument("--pool-file", action="append", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="apply removal; default is dry-run")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    pools = args.pool_file or [root / name for name in ("hotmail.txt", "hotmail.expired.txt", "hotmail.refreshed.txt", "mailbox_tokens.txt")]
    result = cleanup(args.database, args.sessions_dir, pools, apply=args.apply)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
