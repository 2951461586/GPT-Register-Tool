#!/usr/bin/env python
"""Retention planner for the git-ignored ``runtime/`` tree (P2-16 / P0-1).

Why this exists
---------------
``runtime/`` has no retention at all. As of 2026-09-09 it held 9,618 files /
99.5 MiB, of which 81% is two never-pruned directories:

* ``payment_batch_locks/gates/`` — 3,949 **empty** ``slot-0.lock`` files plus
  their batch directories. A batch gate is created per payment batch and is
  never released, so the count only ever grows.
* ``payment_batches/`` — 3,879 per-batch result/event files.

The disk cost is small; the real cost is that the directory becomes
un-listable and any future ``rglob`` over it slows to a crawl.

Why a rule list and not "delete anything older than N days"
-----------------------------------------------------------
``runtime/accounts.sqlite3`` is 67 MiB and **is the account database**. Other
files (``payment_link_runs.jsonl``, ``paypal_proxy_state.json``,
``registration_proxy_health.json``) are live state written on every run. A
pure age sweep would destroy production data, so retention is
**rule-driven with an explicit never-delete set**.

Why relocate instead of delete
------------------------------
On this machine the real-time scanner (Huorong) throttles deletes to single
digits per second, and concurrency makes it fail outright. A **same-volume
``os.rename``** is a metadata operation: moving a 20 GiB / 110k-file tree off
the project measured 0.041 s. So ``--apply`` relocates candidates to
``runtime/_retention/<timestamp>/`` and stops there. Nothing is destroyed
unless you explicitly pass ``--purge``, which then deletes the relocated
tree single-threaded (again the only mode that survives the AV scanner).

Usage
-----
::

    python scripts/runtime_retention.py                 # dry run, prints plan
    python scripts/runtime_retention.py --apply         # relocate to _retention/
    python scripts/runtime_retention.py --min-age 60    # override every rule
    python scripts/runtime_retention.py --purge         # delete relocated trees

The script is deliberately **not** wired into any registration or payment hot
path. Nothing imports it; it is an operator tool.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = PROJECT_ROOT / "runtime"
RELOCATE_DIRNAME = "_retention"

# --------------------------------------------------------------------------
# Never delete. These are live state, not accumulated output.
# --------------------------------------------------------------------------
# Note on redundancy: ``accounts.sqlite3`` is listed here *and* covered by the
# ``.sqlite3`` suffix rule in :func:`is_protected`. That is deliberate
# defence-in-depth, not an oversight -- mutation testing confirmed the list
# entry is behaviourally redundant today (removing it leaves every test green),
# but it documents intent and survives a future narrowing of the suffix rule.
NEVER_DELETE: frozenset[str] = frozenset(
    {
        "accounts.sqlite3",
        "accounts.sqlite3-journal",
        "accounts.sqlite3-wal",
        "payment_link_runs.jsonl",  # payment reconciliation trail
        "registration_progress.jsonl",  # live run progress
        "paypal_proxy_state.json",  # proxy health (wired by P1-2)
        "registration_proxy_health.json",  # proxy health (wired by P1-2)
        "account_relogin_guard.json",
        "mailbox_pool_repair.json",
        "phone_proxy_probe_cache.json",
        "sensitive_policy.json",
    }
)

# Live databases can spawn sidecar files; see :func:`is_protected` for the
# (deliberately narrow) ``.lock`` handling.
NEVER_DELETE_SUFFIXES: tuple[str, ...] = (".sqlite3", ".db")


@dataclass(frozen=True)
class Rule:
    """One retention rule.

    ``relpath_glob`` is matched against the path **relative to the runtime
    root**, using :meth:`pathlib.PurePath.match` semantics on the first
    component when ``match_dir`` is set, otherwise on the file name.
    """

    name: str
    min_age_days: int
    note: str
    match_dir: str | None = None  # e.g. "payment_batch_locks"
    match_name: str | None = None  # e.g. "*.log"
    recursive: bool = True

    def matches(self, relpath: str) -> bool:
        parts = Path(relpath).parts
        if self.match_dir is not None:
            if not parts or parts[0] != self.match_dir:
                return False
            if self.match_name is None:
                return True
            return Path(relpath).name and Path(parts[-1]).match(self.match_name)
        if self.match_name is None:
            return False
        if self.recursive:
            return Path(parts[-1]).match(self.match_name)
        return len(parts) == 1 and Path(parts[-1]).match(self.match_name)


# --------------------------------------------------------------------------
# Rules, most aggressive first. A file is retired by the first rule that
# matches it *and* whose min_age_days is satisfied.
# --------------------------------------------------------------------------
RULES: tuple[Rule, ...] = (
    Rule(
        name="empty batch gate locks",
        min_age_days=7,
        note=(
            "payment_batch_locks/gates/<batch>/slot-0.lock -- 3,949 empty locks "
            "on 2026-09-09, never released after a batch finishes."
        ),
        match_dir="payment_batch_locks",
    ),
    Rule(
        name="empty payment-operation gate locks",
        min_age_days=7,
        note=(
            "payment_operations/gates/payment-operation-<key_hash>/slot-0.lock -- "
            "806 empty locks on 2026-09-09, none older than 5.9 days, so today "
            "this rule retires nothing. It exists because the namespace is "
            "unbounded (a fresh idempotency key == a fresh directory) and "
            "P2-19 only stops the growth from now on; the existing 806 need the "
            "age floor before they can go. Same root cause as the batch gates: "
            "CrossProcessSemaphore.release() never removed the slot file. The "
            "<hash>.json record that sits next to gates/ is idempotency state "
            "and is deliberately NOT matched."
        ),
        match_dir="payment_operations",
        match_name="*.lock",
    ),
    Rule(
        name="browser profiles",
        min_age_days=3,
        note=(
            "P0-1: external_sessions/profiles.py writes user_data_dir "
            "unconditionally and nothing ever GCs it (20 GiB deleted 09-07, "
            "37.2 GiB again 10 days later). A running profile's mtime is "
            "fresh, so the age floor naturally spares it."
        ),
        match_dir="browser_profiles",
    ),
    Rule(
        name="per-batch payment artifacts",
        min_age_days=30,
        note="payment_batches/*.json|*.jsonl -- 3,879 files on 2026-09-09.",
        match_dir="payment_batches",
    ),
    Rule(
        name="one-off scan snapshots",
        min_age_days=14,
        note=(
            "promotion_scan.json / liveness_scan.json / "
            "mailbox_pool_liveness_*.json -- point-in-time snapshots."
        ),
        match_name="*scan*.json",
    ),
    Rule(
        name="operator logs and scratch files",
        min_age_days=30,
        note="app_*.log, _*.txt, _purge_*.log -- desktop logs and scratch output.",
        match_name="*.log",
        recursive=False,
    ),
    Rule(
        name="mailbox imports",
        min_age_days=30,
        note="mailbox_imports/ -- 71 files, oldest 34.8 days on 2026-09-09.",
        match_dir="mailbox_imports",
    ),
)


@dataclass(frozen=True)
class PlanItem:
    relpath: str
    size_bytes: int
    age_days: float
    rule: str

    def as_row(self) -> str:
        return (
            f"{self.age_days:>7.1f}d  {self.size_bytes:>12,}  "
            f"{self.relpath}  [{self.rule}]"
        )


def is_protected(relpath: str) -> bool:
    """Hard veto. Checked before any rule, and cannot be overridden by CLI."""
    name = Path(relpath).name
    if name in NEVER_DELETE:
        return True
    if name.endswith((".sqlite3", ".db")):
        return True
    # ``.lock`` protection is for **top-level sidecar locks** such as
    # ``paypal_proxy_state.json.lock``. It deliberately does NOT cover
    # ``payment_batch_locks/gates/<batch>/slot-0.lock`` -- those 3,949 empty
    # locks are the single biggest thing this tool exists to retire, and an
    # early revision of this function protected them by accident (the dry run
    # reported 33 candidates instead of ~4,000).
    if name.endswith(".lock"):
        return len(Path(relpath).parts) == 1
    return False


def plan_retention(
    root: Path,
    now: float | None = None,
    min_age_days: int | None = None,
    rules: tuple[Rule, ...] = RULES,
) -> list[PlanItem]:
    """Pure planner: walk ``root`` and return what *would* be retired.

    Never touches the filesystem. ``min_age_days`` overrides every rule's
    floor (useful for "what if I waited 60 days?").
    """
    if now is None:
        now = time.time()
    if not root.is_dir():
        return []

    items: list[PlanItem] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip the relocation area itself, and anything already retired.
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir.parts and rel_dir.parts[0] in {RELOCATE_DIRNAME, "_trash-20260909"}:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in {RELOCATE_DIRNAME}]

        for name in filenames:
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue
            relpath = str(rel_dir / name) if str(rel_dir) != "." else name
            if is_protected(relpath):
                continue
            age_days = (now - st.st_mtime) / 86400.0
            for rule in rules:
                if not rule.matches(relpath):
                    continue
                floor = rule.min_age_days if min_age_days is None else min_age_days
                if age_days >= floor:
                    items.append(
                        PlanItem(
                            relpath=relpath,
                            size_bytes=st.st_size,
                            age_days=age_days,
                            rule=rule.name,
                        )
                    )
                break  # first matching rule wins
    items.sort(key=lambda i: -i.age_days)
    return items


def relocate(root: Path, items: list[PlanItem], stamp: str) -> Path:
    """Move planned files under ``root/_retention/<stamp>/`` preserving layout.

    Same-volume rename: metadata only, so even a 20 GiB tree is instant.
    Empty parent directories are pruned bottom-up when they become empty.
    """
    target_root = root / RELOCATE_DIRNAME / stamp
    moved = 0
    for item in items:
        src = root / item.relpath
        if not src.exists():
            continue
        dst = target_root / item.relpath
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dst)
            moved += 1
        except OSError as exc:  # pragma: no cover - operator feedback
            print(f"  ! skip {item.relpath}: {exc}", file=sys.stderr)

    # Prune directories that are now empty (deepest first).
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        rel = Path(dirpath).relative_to(root)
        if not rel.parts or rel.parts[0] in {RELOCATE_DIRNAME, "_trash-20260909"}:
            continue
        try:
            next(os.scandir(dirpath), None)
        except OSError:
            continue
        if not any(os.scandir(dirpath)):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    return target_root


def purge_relocated(root: Path) -> int:
    """Delete every ``_retention/<stamp>`` tree, single-threaded.

    Sequential on purpose: concurrency saturates the AV scan queue and every
    delete fails. See the memory notes on large-directory deletion.
    """
    base = root / RELOCATE_DIRNAME
    if not base.is_dir():
        return 0
    removed = 0
    for child in sorted(base.iterdir()):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            if not child.exists():
                removed += 1
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--min-age",
        type=int,
        default=None,
        help="override every rule's age floor (days)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="relocate planned files to runtime/_retention/<stamp>/",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="delete relocated trees (single-threaded). Requires a prior --apply.",
    )
    args = parser.parse_args(argv)

    root: Path = args.root
    if not root.is_dir():
        print(f"runtime root not found: {root}", file=sys.stderr)
        return 2

    if args.purge:
        n = purge_relocated(root)
        print(f"purged {n} relocated tree(s) from {root / RELOCATE_DIRNAME}")
        return 0

    items = plan_retention(root, min_age_days=args.min_age)
    total = sum(i.size_bytes for i in items)
    print(f"runtime root : {root}")
    print(f"candidates   : {len(items)} file(s), {total:,} bytes")
    if not items:
        print("nothing to retire.")
        return 0

    by_rule: dict[str, int] = {}
    for i in items:
        by_rule[i.rule] = by_rule.get(i.rule, 0) + 1
    print("\nby rule:")
    for rule, count in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>6}  {rule}")

    print("\noldest 40:")
    for item in items[:40]:
        print("  " + item.as_row())

    if not args.apply:
        print("\nDRY RUN -- nothing was moved. Re-run with --apply to relocate.")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = relocate(root, items, stamp)
    print(f"\nrelocated {len(items)} file(s) -> {target}")
    print("Nothing was deleted. To destroy them: --purge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
