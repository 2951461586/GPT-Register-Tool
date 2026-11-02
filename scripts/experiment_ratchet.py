"""Ratchet: every ``registration.*`` A/B toggle carries a recorded decision.

Why this exists
---------------
The registration lane has 22 boolean switches. Until now the only record of why
each one exists was the prose table in
``docs/current/registration-ab-runbook.md`` -- which nothing verified. A switch
could therefore be added without a hypothesis, left on after its experiment
ended, and removed without a note. The ledger makes the decision a data file and
this script makes the link between code, config and document checkable.

What it checks
--------------
1. **Shape.** ``kind``/``status``/``verdict`` come from fixed vocabularies; an
   ``ab`` entry needs a runbook ``id`` and a ``feature`` entry must not have one;
   ``open`` needs an expiry, ``concluded`` needs a verdict and -- while the key
   is still read -- a ``keep.reason``; ``retired`` must not be read at all.
2. **Config parity.** Every entry's ``default`` equals the value in
   ``config.example.json``. A default that only exists in code is a switch
   nobody can find.
3. **Code <-> ledger, both directions.** Every key read through
   ``registration_flag`` / ``_registration_flag`` is registered, and every
   registered key is still read. A call whose key cannot be resolved statically
   must be listed in ``dynamic_call_sites`` with a reason -- silence is how a
   dynamic read would escape the ledger.
4. **Expiry.** An ``open`` entry uses its own ``expires`` or the ledger-wide
   ``review_date``; a past date fails. The date is a review prompt, not a
   measurement plan.
5. **Runbook linkage.** Every ``ab`` entry's ``id`` and ``registration.<key>``
   appear in the runbook. That is what stops the ledger from inventing a
   citation, and the runbook from naming a toggle the ledger never recorded.

String-valued switches (``registration.preflight_login_page``) are out of scope:
they are not read through the boolean helpers, so this gate could not prove
anything about them.

Usage:
    python scripts/experiment_ratchet.py            # check
    python scripts/experiment_ratchet.py --detail   # also print the ledger
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LEDGER = Path(__file__).resolve().parent / "experiment_ledger.json"
CONFIG_EXAMPLE = ROOT / "config.example.json"
RUNBOOK = ROOT / "docs" / "current" / "registration-ab-runbook.md"
SOURCE_ROOTS = (ROOT / "sms_tool",)

KINDS = frozenset({"ab", "feature"})
STATUSES = frozenset({"open", "concluded", "retired"})
VERDICTS = frozenset({"adopted", "rejected", "inconclusive"})
FLAG_CALLS = frozenset({"registration_flag", "_registration_flag"})


# --------------------------------------------------------------------------- #
# code scan
# --------------------------------------------------------------------------- #


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, used to resolve flag keys."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                constants[target.id] = node.value.value
    return constants


def _enclosing_function(tree: ast.Module, line: int) -> str:
    best = "<module>"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= line <= end:
                best = node.name
    return best


def code_reads(root: Path = ROOT) -> tuple[dict[str, list[str]], list[tuple[str, str, int]]]:
    """Return ``(key -> ["path:line", ...], [(path, function, line), ...])``.

    The second element is every call whose key is not a static literal; those must
    be declared in ``dynamic_call_sites`` or they silently escape the ledger.
    """
    reads: dict[str, list[str]] = {}
    dynamic: list[tuple[str, str, int]] = []
    for source_root in SOURCE_ROOTS:
        for path in sorted(source_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            constants = _module_constants(tree)
            relative = path.relative_to(root).as_posix()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id not in FLAG_CALLS:
                    continue
                # ``registration_flag(config, key, default)`` vs
                # ``_registration_flag(key, default)``.
                value = node.args[1] if node.func.id == "registration_flag" else node.args[0]
                key = None
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    key = value.value
                elif isinstance(value, ast.Name):
                    key = constants.get(value.id)
                if key is None:
                    dynamic.append((relative, _enclosing_function(tree, node.lineno), node.lineno))
                else:
                    reads.setdefault(key, []).append(f"{relative}:{node.lineno}")
    return reads, dynamic


# --------------------------------------------------------------------------- #
# ledger checks
# --------------------------------------------------------------------------- #


def _expiry(entry: dict[str, Any], ledger: dict[str, Any]) -> str | None:
    value = entry.get("expires") or ledger.get("review_date")
    return value if isinstance(value, str) else None


def _check_shape(key: str, entry: Any, ledger: dict[str, Any], reads: dict[str, list[str]], root: Path) -> list[str]:
    failures: list[str] = []
    if not isinstance(entry, dict):
        return [f"{key}: entry must be an object"]

    kind = entry.get("kind")
    if kind not in KINDS:
        failures.append(f"{key}: kind must be one of {sorted(KINDS)}, found {kind!r}")
    status = entry.get("status")
    if status not in STATUSES:
        failures.append(f"{key}: status must be one of {sorted(STATUSES)}, found {status!r}")

    default = entry.get("default")
    if not isinstance(default, bool):
        failures.append(f"{key}: default must be true or false, found {default!r}")

    if kind == "ab" and not isinstance(entry.get("id"), str):
        failures.append(f"{key}: an ab entry needs its runbook id (P0-1 ... P3-1)")
    if kind == "feature" and "id" in entry:
        failures.append(f"{key}: a feature entry must not claim a runbook id")

    still_read = key in reads
    if status == "retired" and still_read:
        failures.append(f"{key}: retired, but still read at {', '.join(reads[key])}")
    if status in {"open", "concluded"} and not still_read:
        failures.append(f"{key}: no longer read by code -- mark it retired (or delete the entry)")

    if status == "open":
        expiry = _expiry(entry, ledger)
        if expiry is None:
            failures.append(f"{key}: open entries need expires or a ledger review_date")
        else:
            try:
                due = datetime.date.fromisoformat(expiry)
            except ValueError:
                failures.append(f"{key}: expires must be an ISO date, found {expiry!r}")
            else:
                if due < datetime.date.today():
                    failures.append(f"{key}: review date {expiry} has passed -- decide or retire it")
    if status == "concluded":
        if entry.get("verdict") not in VERDICTS:
            failures.append(f"{key}: concluded entries need verdict in {sorted(VERDICTS)}")
        if still_read:
            keep = entry.get("keep")
            if not isinstance(keep, dict) or not str(keep.get("reason") or "").strip():
                failures.append(f"{key}: concluded but still read -- explain why in keep.reason")

    doc = entry.get("doc")
    if not isinstance(doc, str) or not (root / doc).is_file():
        failures.append(f"{key}: doc must name an existing file, found {doc!r}")
    return failures


def _check_config(key: str, default: Any, section: dict[str, Any]) -> list[str]:
    if key not in section:
        return [f"{key}: missing from config.example.json registration section"]
    value = section[key]
    if not isinstance(value, bool):
        return [f"{key}: config.example.json value must be a boolean, found {value!r}"]
    if value is not default:
        return [f"{key}: default is {default!r} in the ledger but {value!r} in config.example.json"]
    return []


def _check_runbook(key: str, entry: dict[str, Any], runbook: str) -> list[str]:
    if entry.get("kind") != "ab":
        return []
    failures: list[str] = []
    if entry["id"] not in runbook:
        failures.append(f"{key}: runbook id {entry['id']!r} does not appear in {RUNBOOK.name}")
    if f"registration.{key}" not in runbook:
        failures.append(f"{key}: {RUNBOOK.name} never names registration.{key}")
    return failures


def check(root: Path = ROOT) -> list[str]:
    """Return every violation; an empty list means the ledger is honest."""
    if not LEDGER.is_file():
        return [f"{LEDGER.name}: missing"]

    try:
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{LEDGER.name}: invalid JSON ({exc})"]

    failures: list[str] = []
    if ledger.get("schema") != "experiment_ledger.v1":
        failures.append(f"{LEDGER.name}: schema must be experiment_ledger.v1")
    toggles = ledger.get("toggles")
    if not isinstance(toggles, dict) or not toggles:
        return failures + [f"{LEDGER.name}: toggles must be a non-empty object"]

    reads, dynamic = code_reads(root)
    declared_dynamic = {
        (item.get("file"), item.get("function"))
        for item in ledger.get("dynamic_call_sites") or []
        if isinstance(item, dict) and str(item.get("reason") or "").strip()
    }
    for path, function, line in dynamic:
        if (path, function) not in declared_dynamic:
            failures.append(
                f"{path}:{line}: flag key in {function} is not static -- declare it in "
                f"dynamic_call_sites with a reason (and remove the entry when it becomes static)"
            )

    try:
        section = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))["registration"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        return failures + [f"config.example.json: cannot read the registration section ({exc})"]

    runbook = RUNBOOK.read_text(encoding="utf-8") if RUNBOOK.is_file() else ""
    if not runbook:
        failures.append(f"{RUNBOOK.name}: missing (the runbook is the ab entries' evidence)")

    ids: dict[str, str] = {}
    for key, entry in toggles.items():
        failures.extend(_check_shape(key, entry, ledger, reads, root))
        if not isinstance(entry, dict):
            continue
        failures.extend(_check_config(key, entry.get("default"), section))
        failures.extend(_check_runbook(key, entry, runbook))
        if isinstance(entry.get("id"), str):
            if entry["id"] in ids:
                failures.append(f"{key}: runbook id {entry['id']} is already claimed by {ids[entry['id']]}")
            ids[entry["id"]] = key

    for key, sites in sorted(reads.items()):
        if key not in toggles:
            failures.append(f"{key}: read at {', '.join(sites)} but not in the ledger -- record the decision first")

    return failures


def describe() -> list[str]:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    reads, _dynamic = code_reads(ROOT)
    lines: list[str] = []
    counts: dict[str, int] = {}
    for key, entry in ledger["toggles"].items():
        status = str(entry.get("status"))
        counts[status] = counts.get(status, 0) + 1
        marker = "ab " if entry.get("kind") == "ab" else "feat"
        sites = len(reads.get(key, []))
        lines.append(f"  {marker} {key:48s} {status:10s} default={str(entry.get('default')):5s} sites={sites}")
    lines.append("  " + ", ".join(f"{name}={value}" for name, value in sorted(counts.items())))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Every registration.* toggle has a recorded decision.")
    parser.add_argument("--detail", action="store_true", help="print the ledger and where each key is read")
    args = parser.parse_args(argv)

    failures = check(ROOT)
    if args.detail:
        for line in describe():
            print(line)
    if failures:
        print(f"experiment ledger: FAILED ({len(failures)} problem(s))")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("experiment ledger: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
