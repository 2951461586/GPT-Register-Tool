#!/usr/bin/env python3
"""Retire the stored copies of the access-token probe verdict.

``at_probe_status_code`` and the top-level ``token_probe`` are duplicated facts,
and both drifted:

* ``at_probe_status_code`` is a *derived cache*. The row payload the WPF grid
  reads computes it from the probe records
  (``desktop.read._access_token_probe_status_code`` ranks
  ``account_scan.token_probe`` / ``token_probe`` / ``scan.token_probe`` /
  ``quota.last_result`` by timestamp), so nothing reads the stored scalar back.
* the top-level ``token_probe`` is the **same lane's** record as
  ``quota.last_result`` (``store.markers.mark_quota_status`` wrote both) but was
  only refreshed on an HTTP 200, so a 401 could outlive the 200 that healed the
  account — a scan heals a token without going through that function.

Measured 2026-09-04 on the fleet: five rows kept
``token_probe.status_code=401`` + ``at_probe_status_code='401'`` for 160 rows of
probe history while ``account_scan.token_probe`` and ``quota.last_result`` both
said 200. The read path already ranked by timestamp and displayed 200, so this
is a landmine removal, not a display change.

Both keys are dropped from the DB ``raw_json`` **and** the session JSON file
(the seed of every later upsert). The command is dry-run by default, and it
refuses any row whose derived verdict would change — the cleanup may not move a
single badge, only remove the copy that made the two disagree.

Usage:
    python scripts/repair_stale_probe_markers.py              # dry run
    python scripts/repair_stale_probe_markers.py --apply
    python scripts/repair_stale_probe_markers.py --report out.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sms_tool.desktop.read import _access_token_probe_status_code  # noqa: E402

DERIVED_SCALAR = "at_probe_status_code"
DUPLICATE_PROBE = "token_probe"


def strip_stale_probe_fields(data: dict[str, Any]) -> list[str]:
    """Drop the two stored copies from ``data`` in place; return what went.

    ``token_probe`` is only dropped when ``quota.last_result`` is present: that
    is the same lane's record, so the drop is lossless. Without it the probe
    would be the only evidence and the caller's verdict check keeps it.
    """
    dropped: list[str] = []
    if DERIVED_SCALAR in data:
        data.pop(DERIVED_SCALAR)
        dropped.append(DERIVED_SCALAR)
    quota = data.get("quota")
    if isinstance(quota, dict) and isinstance(quota.get("last_result"), dict) and DUPLICATE_PROBE in data:
        data.pop(DUPLICATE_PROBE)
        dropped.append(DUPLICATE_PROBE)
    return dropped


def _strip_payload(raw_text: Any) -> tuple[dict[str, Any], list[str], bool]:
    """Return (payload, dropped_keys, verdict_unchanged) for one stored blob."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        return {}, [], True
    try:
        data = json.loads(raw_text)
    except ValueError:
        return {}, [], True
    if not isinstance(data, dict):
        return {}, [], True
    before = _access_token_probe_status_code(data)
    dropped = strip_stale_probe_fields(data)
    return data, dropped, not dropped or _access_token_probe_status_code(data) == before


def repair(database: Path, *, apply: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "database": str(database),
        "applied": bool(apply),
        "rows_scanned": 0,
        "db_rows_changed": 0,
        "session_files_changed": 0,
        "dropped": {DERIVED_SCALAR: 0, DUPLICATE_PROBE: 0},
        "skipped_verdict_changed": [],
        "skipped_unreadable": [],
    }
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in conn.execute("SELECT id, email, json_path, raw_json FROM accounts ORDER BY id")]
        for row in rows:
            result["rows_scanned"] += 1
            data, dropped, unchanged = _strip_payload(row.get("raw_json"))
            if dropped and not unchanged:
                result["skipped_verdict_changed"].append(row.get("email") or row.get("id"))
                continue
            if dropped:
                for key in dropped:
                    result["dropped"][key] += 1
                result["db_rows_changed"] += 1
                if apply:
                    conn.execute(
                        "UPDATE accounts SET raw_json=? WHERE id=?",
                        (json.dumps(data, ensure_ascii=False, separators=(",", ":")), row["id"]),
                    )
            session_path = Path(str(row.get("json_path") or ""))
            if not session_path.is_file():
                continue
            try:
                file_data = json.loads(session_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                result["skipped_unreadable"].append(str(session_path))
                continue
            if not isinstance(file_data, dict):
                continue
            before = _access_token_probe_status_code(file_data)
            file_dropped = strip_stale_probe_fields(file_data)
            if not file_dropped or _access_token_probe_status_code(file_data) != before:
                continue
            result["session_files_changed"] += 1
            if apply:
                session_path.write_text(json.dumps(file_data, ensure_ascii=False, indent=2), encoding="utf-8")
        if apply:
            conn.commit()
    finally:
        conn.close()
    return result


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=root / "runtime" / "accounts.sqlite3")
    parser.add_argument("--apply", action="store_true", help="write the change; default is dry-run")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    if not args.database.is_file():
        print(f"[!] database not found: {args.database}")
        return 2
    output = json.dumps(repair(args.database, apply=args.apply), ensure_ascii=False, indent=2)
    print(output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
