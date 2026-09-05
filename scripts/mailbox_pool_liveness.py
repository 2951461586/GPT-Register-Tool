#!/usr/bin/env python3
"""Probe every mailbox-pool credential and optionally prune definitive dead entries.

Network failures and 5xx responses are retained. Only provider responses that
prove the credential is invalid (401/403/404/410) are removable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sms_tool.config import CFG
from sms_tool.mailbox import _fetch_mailbox_messages
from sms_tool.mailbox_parsers import parse_mailbox_pool_line


def _fingerprint(raw: str) -> str:
    """Identify a pool credential without persisting the credential itself."""
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def _quarantine_path(value: Path | None) -> Path:
    return value or (ROOT / "runtime" / "mailbox_liveness_quarantine.json")


def _load_quarantine(path: Path) -> dict[str, dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    rows = value.get("entries") if isinstance(value, dict) else None
    return rows if isinstance(rows, dict) else {}


def _status_from_error(error: str) -> int | None:
    match = re.search(r"(?:HTTP|status)[ :]+(401|403|404|410|429|500|502|503|504)\b", error, re.I)
    return int(match.group(1)) if match else None


def _probe(item: tuple[int, str, object]) -> dict[str, object]:
    index, raw, mailbox = item
    email = str(getattr(mailbox, "email", "") or "").strip().lower()
    provider = str(getattr(mailbox, "provider", "") or "").strip().lower()
    try:
        messages = _fetch_mailbox_messages(mailbox, limit=1, proxy=None)
        return {"index": index, "email": email, "provider": provider, "ok": True, "messages": len(messages or [])}
    except Exception as exc:
        error = str(exc)
        status = _status_from_error(error)
        definitive = status in {401, 403, 404, 410}
        return {
            "index": index,
            "email": email,
            "provider": provider,
            "ok": False,
            "definitive_dead": definitive,
            "status_code": status or 0,
            "error": re.sub(r"https?://[^\s]+", "[URL]", error)[:240],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-file", type=Path, default=ROOT / "mailbox_tokens.txt")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--apply", action="store_true", help="remove only definitive dead entries after the scan")
    parser.add_argument("--quarantine", type=Path, default=None, help="quarantine state for second confirmation")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    lines = args.pool_file.read_text(encoding="utf-8-sig").splitlines(keepends=True) if args.pool_file.exists() else []
    items: list[tuple[int, str, object]] = []
    skipped = 0
    for index, raw in enumerate(lines):
        mailbox = parse_mailbox_pool_line(raw, str(args.pool_file), index + 1)
        if mailbox is None:
            if raw.strip() and not raw.lstrip().startswith("#"):
                skipped += 1
            continue
        items.append((index, raw, mailbox))

    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 32))) as executor:
        futures = [executor.submit(_probe, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda row: int(row.get("index") or 0))
    quarantine_path = _quarantine_path(args.quarantine)
    quarantine = _load_quarantine(quarantine_path)
    dead_indexes = {int(row["index"]) for row in results if row.get("definitive_dead")}
    confirmed_indexes: set[int] = set()
    now = int(time.time())
    for row in results:
        index = int(row.get("index") or 0)
        fp = _fingerprint(lines[index]) if 0 <= index < len(lines) else ""
        if row.get("definitive_dead") and fp:
            previous = quarantine.get(fp) if isinstance(quarantine.get(fp), dict) else {}
            confirmations = int(previous.get("confirmations") or 0) + 1
            quarantine[fp] = {
                "email": row.get("email", ""),
                "provider": row.get("provider", ""),
                "status_code": row.get("status_code", 0),
                "confirmations": confirmations,
                "first_seen_at": int(previous.get("first_seen_at") or now),
                "last_seen_at": now,
                "last_error": str(row.get("error") or "")[:240],
            }
            if confirmations >= 2:
                confirmed_indexes.add(index)
        elif fp:
            quarantine.pop(fp, None)
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    quarantine_path.write_text(
        json.dumps({"version": 1, "entries": quarantine}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report: dict[str, object] = {
        "pool_file": str(args.pool_file),
        "total": len(items),
        "skipped": skipped,
        "ok": sum(bool(row.get("ok")) for row in results),
        "failed": sum(not bool(row.get("ok")) for row in results),
        "definitive_dead": len(dead_indexes),
        "confirmed_dead": len(confirmed_indexes),
        "quarantine": str(quarantine_path),
        "dry_run": not args.apply,
        "results": [
            {key: value for key, value in row.items() if key not in {"index"}}
            for row in results
        ],
    }
    if args.apply and confirmed_indexes:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = args.pool_file.with_name(f"{args.pool_file.name}.pre_liveness_{stamp}")
        shutil.copy2(args.pool_file, backup)
        args.pool_file.write_text("".join(line for index, line in enumerate(lines) if index not in confirmed_indexes), encoding="utf-8")
        report["backup"] = str(backup)
        report["removed_lines"] = len(confirmed_indexes)
        report["remaining_lines"] = len(lines) - len(confirmed_indexes)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
