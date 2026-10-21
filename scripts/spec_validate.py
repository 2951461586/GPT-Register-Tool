"""Validate the ``architecture-design`` spec graph.

Why this exists
---------------
``docs/architecture.md`` is the architecture authority, ``docs/rules/rule-NN-*``
holds the enforceable boundary rules and ``docs/adr/NNNN-*`` holds the accepted
decisions. Nothing tied them together in a queryable form: a reader asking
"which boundary does rule-13 belong to, and which decision shaped it?" had to
reconstruct the answer from three prose documents, and a stale reference (a
renumbered rule, a reverted ADR) was invisible because nothing resolved them.

``docs/specs/`` closes that with one file per boundary. The frontmatter is the
machine-readable half; the body stays prose. This script is the gate: it
resolves every reference and fails on a dangling one.

Frontmatter shape
-----------------
A YAML *subset*, parsed with the standard library on purpose -- every other gate
in this repo is stdlib-only so it can run before dependencies are installed::

    ---
    id: spec-01-transport-policy-boundary
    type: architecture-design
    status: accepted
    owner: sms_tool/http_client.py
    implements:
      - rule-01
    depends-on:
      - adr-0009
    ---

``id`` must equal ``spec-`` plus the filename stem. ``owner`` is a repo-relative
path that must exist, so a spec cannot outlive the module it describes without
someone noticing.

What is checked
---------------
1. every file under ``docs/specs/`` carries a frontmatter block;
2. ``id`` is unique and matches the filename;
3. ``type`` and ``status`` are from the allowed sets;
4. ``owner`` resolves to an existing path;
5. every ``implements`` entry resolves to ``docs/rules/rule-NN-*.md``;
6. every ``depends-on`` entry resolves to ``docs/adr/NNNN-*.md`` or to another
   spec in this directory;
7. the ``depends-on`` graph over specs has no cycle.

Usage:
    python scripts/spec_validate.py            # check (exit 1 on any problem)
    python scripts/spec_validate.py --list     # print the resolved graph
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "docs" / "specs"
RULES = ROOT / "docs" / "rules"
ADRS = ROOT / "docs" / "adr"

TYPES = frozenset({"architecture-design"})
STATUSES = frozenset({"accepted", "proposed", "superseded"})
REQUIRED = ("id", "type", "status", "owner")


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Return ``(data, error)`` for the leading ``---`` block.

    Only two shapes are supported, and both are unambiguous: ``key: value`` and
    ``key:`` followed by ``  - item`` lines. Anything else is an error rather
    than a silent skip -- a frontmatter the gate cannot read is a spec the gate
    does not protect.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "missing frontmatter block (file must start with '---')"
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return {}, "unterminated frontmatter block (no closing '---')"

    data: dict[str, object] = {}
    current: str | None = None
    for number, raw in enumerate(lines[1:end], start=2):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("  - ") or raw.startswith("- "):
            if current is None:
                return {}, f"line {number}: list item outside a key"
            item = raw.split("- ", 1)[1].strip()
            existing = data.get(current)
            if not isinstance(existing, list):
                return {}, f"line {number}: list item for non-list key {current!r}"
            existing.append(item)
            continue
        if ":" not in raw:
            return {}, f"line {number}: expected 'key: value'"
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            return {}, f"line {number}: empty key"
        if key in data:
            return {}, f"line {number}: duplicate key {key!r}"
        if value:
            data[key] = value
            current = None
        else:
            data[key] = []
            current = key
    return data, ""


def _refs(data: dict[str, object], key: str) -> list[str]:
    value = data.get(key, [])
    return [str(item) for item in value] if isinstance(value, list) else []


def _resolve_rule(ref: str) -> Path | None:
    if not ref.startswith("rule-"):
        return None
    return next(iter(sorted(RULES.glob(f"{ref}-*.md"))), None)


def _resolve_adr(ref: str) -> Path | None:
    if not ref.startswith("adr-"):
        return None
    return next(iter(sorted(ADRS.glob(f"{ref[4:]}-*.md"))), None)


def collect() -> tuple[dict[str, dict[str, object]], list[str]]:
    """Load every spec; return ``(by_id, problems)``."""
    by_id: dict[str, dict[str, object]] = {}
    problems: list[str] = []
    for path in sorted(SPECS.glob("*.md")):
        rel = path.relative_to(ROOT).as_posix()
        data, error = parse_frontmatter(path.read_text(encoding="utf-8"))
        if error:
            problems.append(f"{rel}: {error}")
            continue
        missing = [key for key in REQUIRED if key not in data]
        if missing:
            problems.append(f"{rel}: missing required key(s): {', '.join(missing)}")
            continue
        spec_id = str(data["id"])
        expected = f"spec-{path.stem}"
        if spec_id != expected:
            problems.append(f"{rel}: id {spec_id!r} does not match the filename (expected {expected!r})")
        if spec_id in by_id:
            problems.append(f"{rel}: duplicate id {spec_id!r}")
            continue
        by_id[spec_id] = data
    return by_id, problems


def validate() -> list[str]:
    """Every way the spec graph is broken."""
    if not SPECS.is_dir():
        return [f"{SPECS.relative_to(ROOT)}/ does not exist"]
    by_id, problems = collect()
    if not by_id and not problems:
        return ["no spec files found under docs/specs/"]

    known_specs = set(by_id)
    for spec_id, data in by_id.items():
        rel = f"docs/specs/{spec_id.removeprefix('spec-')}.md"
        if str(data["type"]) not in TYPES:
            problems.append(f"{rel}: type {data['type']!r} is not one of {sorted(TYPES)}")
        if str(data["status"]) not in STATUSES:
            problems.append(f"{rel}: status {data['status']!r} is not one of {sorted(STATUSES)}")
        owner = str(data["owner"])
        if not (ROOT / owner).exists():
            problems.append(f"{rel}: owner {owner!r} does not exist")
        for ref in _refs(data, "implements"):
            if _resolve_rule(ref) is None:
                problems.append(f"{rel}: implements {ref!r} has no docs/rules/{ref}-*.md")
        for ref in _refs(data, "depends-on"):
            if ref.startswith("spec-"):
                if ref not in known_specs:
                    problems.append(f"{rel}: depends-on {ref!r} names no spec in docs/specs/")
            elif _resolve_adr(ref) is None:
                problems.append(
                    f"{rel}: depends-on {ref!r} has no docs/adr/{ref[4:] if ref.startswith('adr-') else ref}-*.md"
                )

    problems.extend(_cycles(by_id))
    return problems


def _cycles(by_id: dict[str, dict[str, object]]) -> list[str]:
    """Depth-first cycle detection over the spec ``depends-on`` edges."""
    problems: list[str] = []
    state: dict[str, int] = {}  # 0 = open, 1 = done
    stack: list[str] = []

    def walk(node: str) -> None:
        if state.get(node) == 1:
            return
        if state.get(node) == 0:
            cycle = stack[stack.index(node) :] + [node]
            problems.append("depends-on cycle: " + " -> ".join(cycle))
            return
        state[node] = 0
        stack.append(node)
        for ref in _refs(by_id.get(node, {}), "depends-on"):
            if ref in by_id:
                walk(ref)
        stack.pop()
        state[node] = 1

    for spec_id in sorted(by_id):
        walk(spec_id)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--list", action="store_true", help="print the resolved graph and exit")
    args = parser.parse_args(argv)

    by_id, load_problems = collect()
    if args.list:
        for spec_id, data in sorted(by_id.items()):
            rules = ", ".join(_refs(data, "implements")) or "-"
            deps = ", ".join(_refs(data, "depends-on")) or "-"
            print(f"{spec_id:44s} implements[{rules}] depends-on[{deps}]")
        return 1 if load_problems else 0

    problems = validate()
    if problems:
        print("spec validation FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"spec validation OK ({len(by_id)} spec(s), no dangling reference, no cycle)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
