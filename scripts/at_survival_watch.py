#!/usr/bin/env python3
"""Record how long freshly registered accounts' access tokens stay alive.

Read-only: every probe is ``probe_account_liveness`` (the ``wham/usage`` GET), no
relogin, no persistence.  For each account it waits until ``t0 + offset`` for
every offset in ``--schedule-minutes`` and appends one JSON line per probe to
``--out``.  Accounts are addressed by id (``accounts.id``); the access token is
re-read from the database at every checkpoint, so a relogin in between is
picked up.

Why it exists: a registration-time probe only proves the token was alive for
~10 seconds.  The 2026-10-11 iCloud accounts all returned HTTP 401 within
minutes, which no existing record could have shown.  This tool is the
measurement every later variable (2FA, warm-up, exit country) is judged by.

    python scripts/at_survival_watch.py --ids 13950,13952 --label ctrl \
        --schedule-minutes 1,5,15,60 --t0 created
    python scripts/at_survival_watch.py --summary runtime/at_survival.jsonl

``--t0`` is ``created`` (the account's ``created_at``), ``now`` or an epoch.
Output lines never contain tokens, cookies or e-mail addresses.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_OUT = PROJECT_ROOT / "runtime" / "at_survival.jsonl"
DEFAULT_SCHEDULE = "1,5,15,60"


def parse_schedule(value: str) -> list[float]:
    offsets = sorted({float(part) for part in str(value).split(",") if part.strip()})
    if not offsets or any(offset < 0 for offset in offsets):
        raise ValueError("schedule must be a comma list of non-negative minutes")
    return offsets


def resolve_t0(spec: str, created_at: float, now: float) -> float:
    spec = str(spec or "created").strip().lower()
    if spec == "created":
        return float(created_at)
    if spec == "now":
        return float(now)
    return float(spec)


def probe_row(label: str, account_id: int, offset_min: float, t0: float, result: dict) -> dict:
    """Shape one output line from a liveness result; drops everything sensitive."""
    code = result.get("status_code")
    error = str(result.get("error") or "")
    return {
        "label": label,
        "account_id": int(account_id),
        "offset_min": offset_min,
        "age_s": int(time.time() - t0),
        "at": int(time.time()),
        "status_code": int(str(code)) if str(code or "").isdigit() else 0,
        "state": str(result.get("status") or ""),
        "alive": str(code) == "200",
        "revoked": "revoked" in error.lower() or "invalidated" in error.lower(),
        "error": error[:80] if str(code) != "200" else "",
    }


def summarize(rows: list[dict]) -> dict:
    """Per label and offset: alive count / probed count (ignores transport unknowns)."""
    table: dict[str, dict[float, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for row in rows:
        code = int(row.get("status_code") or 0)
        if code not in (200, 401, 403):
            continue
        cell = table[str(row.get("label") or "")][float(row.get("offset_min") or 0)]
        cell[1] += 1
        cell[0] += 1 if code == 200 else 0
    return {label: {offset: f"{c[0]}/{c[1]}" for offset, c in sorted(cells.items())} for label, cells in table.items()}


def _load_account(account_id: int) -> dict:
    from sms_tool.store.accounts import get_account_record_by_id

    return get_account_record_by_id(account_id)


def watch(ids: list[int], label: str, offsets: list[float], t0_spec: str, out: Path) -> int:
    from sms_tool.accounts.account_liveness import probe_account_liveness
    from sms_tool.config import initialize_runtime_config

    initialize_runtime_config()
    now = time.time()
    t0s = {}
    for account_id in ids:
        account = _load_account(account_id)
        if not account:
            print(f"[!] account {account_id} not found; skipped")
            continue
        t0s[account_id] = resolve_t0(t0_spec, account.get("created_at") or now, now)
    if not t0s:
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    pending = sorted((t0s[i] + o * 60, i, o) for i in t0s for o in offsets)
    for due, account_id, offset in pending:
        wait = due - time.time()
        if wait > 0:
            time.sleep(wait)
        account = _load_account(account_id)
        try:
            result = probe_account_liveness(account, timeout=30)
        except Exception as exc:  # a transport failure is a data point, not a crash
            result = {"status": "unknown", "error": f"probe_exception:{type(exc).__name__}"}
        row = probe_row(label, account_id, offset, t0s[account_id], result)
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[*] {label} id={account_id} +{offset:g}min -> HTTP {row['status_code']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", help="comma separated accounts.id values")
    parser.add_argument("--label", default="run", help="arm / batch label stored on every line")
    parser.add_argument("--schedule-minutes", default=DEFAULT_SCHEDULE)
    parser.add_argument("--t0", default="created", help="created | now | epoch seconds")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--summary", metavar="JSONL", help="print alive/probed per label+offset and exit")
    args = parser.parse_args(argv)

    if args.summary:
        rows = [
            json.loads(line) for line in Path(args.summary).read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))
        return 0
    if not args.ids:
        parser.error("--ids or --summary is required")
    ids = [int(part) for part in args.ids.split(",") if part.strip()]
    return watch(ids, args.label, parse_schedule(args.schedule_minutes), args.t0, Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
