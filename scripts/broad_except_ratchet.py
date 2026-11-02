"""Ratchet: broad ``except`` handlers must not grow.

Why this exists
---------------
Measured on 2026-10-28: ``sms_tool`` + ``services`` carried 522 ``except
Exception:`` handlers, 151 of which swallowed the error with a bare ``pass``.
Neither number is wrong by itself -- a cleanup path that must not fail is a real
thing -- but nothing distinguished "cleanup, best effort" from "logic error,
silently ignored", and the count only ever grew. This gate freezes the current
per-file numbers so the next broad handler is a deliberate act rather than a
default.

The reviewable convention for the handlers that have to stay broad is
[Rule 26](../docs/rules/rule-26-broad-except-marker.md): narrow to
``kernel/transport.TRANSPORT_ERRORS`` or a concrete exception tuple when the
handler only guards a transport call, and otherwise mark it
``# broad-except: compat`` with a reason where one is not obvious.

**The marker is documentation; this script never reads it.** ``grep -r
broad-except scripts/`` matches only a ``print`` format string. Permission lives
in ``broad_except_baseline.json`` (``policy`` ceilings, ``policy.lanes``,
``whitelist``, ``policy.budget_history``) and nowhere else, so a comment can
never buy an allowance and deleting one can never lower a count.

What counts
-----------
* **broad**: ``except Exception:`` / ``except BaseException:``, or a tuple that
  contains one of them. ``except (OSError, ValueError):`` is not broad: it names
  what it expects.
* **silent**: a broad handler whose body is exactly ``pass`` or ``...``.

What does not count
-------------------
A bare ``except:`` is reported by ruff/pyright separately and is already rare;
this gate is about the handler people write when they mean "anything".

Whitelist and budget (2026-10-28)
---------------------------------
Freezing every count in one dump says "this is how it is", not "this is
accepted". The baseline therefore has three parts:

* ``whitelist`` - the files where a broad handler is the **design**, each with a
  ``reason``. An entry without a reason fails, and so does an entry whose file no
  longer has any broad handler (that is stale permission, not permission).
* ``policy`` - the global budget (``max_broad`` / ``max_silent``). It exists so a
  file cannot quietly absorb another file's growth: per-file allowances alone let
  a deletion in one module pay for an addition in another.
* ``files`` - the frozen per-file counts for everything not whitelisted. These
  are debt, not endorsement: they may only shrink.

So the honest description today is "962 broad handlers, of which the whitelisted
files are accepted and the rest are frozen debt with a hard ceiling".

Usage
-----
``python scripts/broad_except_ratchet.py``          # check (exit 1 on growth)
``python scripts/broad_except_ratchet.py --write``  # re-freeze the baseline

Output is ASCII-only: CI runs on a Windows runner whose stdout is cp1252.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(__file__).resolve().parent / "broad_except_baseline.json"

#: The trees the gate owns. Tests are excluded on purpose: a test double that
#: swallows an exception is a different (and usually louder) problem.
TREES = ("sms_tool", "services")

BROAD_NAMES = frozenset({"Exception", "BaseException"})


def _is_broad(handler: ast.ExceptHandler) -> bool:
    node = handler.type
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id in BROAD_NAMES
    if isinstance(node, ast.Tuple):
        return any(isinstance(item, ast.Name) and item.id in BROAD_NAMES for item in node.elts)
    return False


def _is_silent(handler: ast.ExceptHandler) -> bool:
    if len(handler.body) != 1:
        return False
    statement = handler.body[0]
    if isinstance(statement, ast.Pass):
        return True
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is Ellipsis
    )


def count_file(path: Path) -> tuple[int, int]:
    """(broad, silent) for one module; unparseable files are skipped, not failed."""
    try:
        module = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return (0, 0)

    broad = silent = 0
    for node in ast.walk(module):
        if isinstance(node, ast.ExceptHandler) and _is_broad(node):
            broad += 1
            if _is_silent(node):
                silent += 1
    return (broad, silent)


def scan(root: Path = ROOT, trees: Iterable[str] = TREES) -> dict[str, dict[str, int]]:
    """Per-file counts, relative to ``root``, for every file that has a broad handler."""
    counts: dict[str, dict[str, int]] = {}
    for tree in trees:
        directory = root / tree
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            broad, silent = count_file(path)
            if broad == 0:
                continue
            counts[path.relative_to(root).as_posix()] = {"broad": broad, "silent": silent}
    return counts


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {"files": {}, "totals": {"broad": 0, "silent": 0}}
    return json.loads(path.read_text(encoding="utf-8"))


def _totals(counts: Mapping[str, Mapping[str, int]]) -> dict[str, int]:
    return {
        "broad": sum(int(entry["broad"]) for entry in counts.values()),
        "silent": sum(int(entry["silent"]) for entry in counts.values()),
    }


def build_baseline(
    counts: Mapping[str, Mapping[str, int]],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-freeze ``files``/``policy`` while keeping the reviewed ``whitelist`` and lanes."""
    whitelist = (previous or {}).get("whitelist", {})
    previous_policy = (previous or {}).get("policy", {})
    lanes = previous_policy.get("lanes", {}) if isinstance(previous_policy, Mapping) else {}
    history = previous_policy.get("budget_history", []) if isinstance(previous_policy, Mapping) else []
    totals = _totals(counts)
    policy: dict[str, Any] = {"max_broad": totals["broad"], "max_silent": totals["silent"]}
    if lanes:
        # A lane budget is a reviewed decision, not a measurement: re-freezing the
        # debt table must not be able to drop it, or the gate silently becomes
        # decorative on the next --write (measured 2026-11-01: it did).
        policy["lanes"] = lanes
    if history:
        # Same rule: the budget history is the reviewed record of *why* a cap moved.
        # Re-freezing measures the tree; it must not erase the audit trail that
        # justifies the number it just wrote.
        policy["budget_history"] = history
    return {
        "_comment": (
            "Broad-except gate state (see scripts/broad_except_ratchet.py). 'files' is frozen "
            "debt with a hard ceiling in 'policy'; 'whitelist' is the reviewed set where a "
            "broad handler is the design. A file absent from both has an allowance of 0."
        ),
        "policy": policy,
        "whitelist": whitelist,
        "files": {name: dict(entry) for name, entry in sorted(counts.items())},
        "totals": totals,
    }


def violations(
    counts: Mapping[str, Mapping[str, int]],
    baseline: Mapping[str, Any],
) -> list[str]:
    """Every file that is above its allowance, plus the message that explains it."""
    allowed = baseline.get("files", {}) if isinstance(baseline, Mapping) else {}
    problems: list[str] = []
    for name in sorted(counts):
        entry = counts[name]
        limit = allowed.get(name, {"broad": 0, "silent": 0})
        for kind in ("broad", "silent"):
            current = int(entry.get(kind, 0))
            cap = int(limit.get(kind, 0)) if isinstance(limit, Mapping) else 0
            if current > cap:
                problems.append(
                    "%s: %s handlers %d > allowed %d (narrow the except, or re-freeze deliberately "
                    "with --write)" % (name, kind, current, cap)
                )
    return problems


def check(counts: Mapping[str, Mapping[str, int]], baseline: Mapping[str, Any]) -> list[str]:
    """Every reason the tree may not ship as-is, in one list.

    Four independent tests, deliberately not folded into one:

    * per file: the count may not exceed the frozen allowance;
    * globally: the totals may not exceed ``policy``, so no module can pay for
      another module's growth;
    * per lane: ``policy.lanes`` scopes a budget to a set of directories. The
      protocol registration lane is the one where a swallowed exception is
      indistinguishable from a protocol failure (it lands in the funnel as
      ``unknown`` while the batch keeps spending mailboxes), so it carries
      ``silent = 0``;
    * whitelist integrity: every reviewed entry needs a reason, has to describe a
      file that still has a broad handler at all, and its declared ``broad`` /
      ``silent`` counts must equal what the tree measures -- the numbers are the
      evidence a reviewer saw, so a drift means the acceptance is stale even when
      the prose reason still reads true;
    * ``policy.budget_history``: the reviewed record of every deliberate cap move.
      It is preserved across ``--write`` for the same reason ``lanes`` is a budget
      and not a measurement.
    """
    problems = violations(counts, baseline)

    policy = baseline.get("policy", {}) if isinstance(baseline, Mapping) else {}
    totals = _totals(counts)
    for kind, key in (("broad", "max_broad"), ("silent", "max_silent")):
        cap = int(policy.get(key, 0)) if isinstance(policy, Mapping) else 0
        if totals[kind] > cap:
            problems.append(
                "total %s handlers %d > budget %d (narrow one, or raise the budget "
                "deliberately with --write)" % (kind, totals[kind], cap)
            )

    whitelist = baseline.get("whitelist", {}) if isinstance(baseline, Mapping) else {}
    lanes = policy.get("lanes", {}) if isinstance(policy, Mapping) else {}
    for lane_name in sorted(lanes):
        lane = lanes[lane_name] if isinstance(lanes[lane_name], Mapping) else {}
        prefixes = tuple(str(item) for item in (lane.get("paths") or ()))
        scoped = {
            name: value
            for name, value in counts.items()
            if prefixes and name.startswith(prefixes) and name not in whitelist
        }
        for kind, key in (("broad", "max_broad"), ("silent", "max_silent")):
            used = sum(int(value.get(kind, 0)) for value in scoped.values())
            cap = int(lane.get(key, 0))
            if used > cap:
                problems.append(
                    "lane %s: %s handlers %d > budget %d (%s)"
                    % (lane_name, kind, used, cap, lane.get("reason", "no reason recorded"))
                )

    for name in sorted(whitelist):
        entry = whitelist[name]
        reason = str(entry.get("reason", "")).strip() if isinstance(entry, Mapping) else ""
        if not reason:
            problems.append("%s: whitelisted without a reason (the list is a review record, not a mute button)" % name)
        if name not in counts:
            problems.append("%s: whitelisted but no longer has a broad handler; drop the entry (--write)" % name)
            continue
        # The numbers in a whitelist entry are the review evidence: they say what a
        # reviewer saw when they accepted the handlers. A drift between them and the
        # tree means the file changed after review, so the acceptance is stale even
        # when the reason still reads true (measured 2026-11-02: two entries were
        # carrying counts from a different revision). --write does not refresh them
        # on purpose -- re-measure and re-review, do not re-freeze.
        measured = (int(counts[name].get("broad", 0)), int(counts[name].get("silent", 0)))
        declared = (
            int(entry.get("broad", -1)) if isinstance(entry, Mapping) else -1,
            int(entry.get("silent", -1)) if isinstance(entry, Mapping) else -1,
        )
        if declared != measured:
            problems.append(
                "%s: whitelist counts are stale -- declared broad %d/silent %d, measured broad %d/silent %d "
                "(re-measure the entry, do not re-freeze it)"
                % (name, declared[0], declared[1], measured[0], measured[1])
            )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Broad-except ratchet")
    parser.add_argument("--write", action="store_true", help="re-freeze the baseline")
    parser.add_argument("--report", action="store_true", help="print every counted file")
    arguments = parser.parse_args(argv)

    counts = scan()
    previous = load_baseline()

    if arguments.write:
        BASELINE_PATH.write_text(
            json.dumps(build_baseline(counts, previous), indent=2, sort_keys=True) + chr(10),
            encoding="utf-8",
            newline=chr(10),
        )
        print("broad-except baseline written: %s" % BASELINE_PATH)
        return 0

    if arguments.report:
        for name in sorted(counts):
            entry = counts[name]
            print("%-72s broad=%-4d silent=%d" % (name, entry["broad"], entry["silent"]))

    problems = check(counts, previous)
    totals = _totals(counts)
    budget = previous.get("policy", {}) if isinstance(previous, Mapping) else {}
    whitelisted = previous.get("whitelist", {}) or {}
    print(
        "broad-except: %d handlers (%d silent) across %d files; whitelisted %d; budget %s/%s"
        % (
            totals["broad"],
            totals["silent"],
            len(counts),
            len(whitelisted),
            budget.get("max_broad", "?"),
            budget.get("max_silent", "?"),
        )
    )
    if problems:
        print("")
        for problem in problems:
            print("  " + problem)
        print("")
        print("FAIL: broad-except gate tripped.")
        return 1

    print("OK: no file exceeds its allowance, the budget holds, and the whitelist is honest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
