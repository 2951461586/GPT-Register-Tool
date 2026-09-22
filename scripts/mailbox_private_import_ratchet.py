"""Ratchet for cross-module imports of private symbols from facade modules.

Why this exists
---------------
``sms_tool/mailbox.py`` defines ~40 top-level functions and **every one of them
is ``_``-prefixed** -- it has no public surface at all. Yet it is the most
imported module in the package: registration, auth, recovery, mailbox routing,
CLI adapters and ``scripts/`` all reach straight into those private names
(``_poll_email_otp``, ``_load_mailbox_pool``, ``_mailbox_from_config``, ...).

That is a real coupling surface with no guard on it. ``directory-map.md``
describes the top-level ``mailbox_*`` files as "compatibility facades only" and
places the real implementations under ``sms_tool/providers/`` -- and
``tests/test_docs_consistency.py`` does lock the six provider facades
(``mailbox_cfworker`` / ``_gmail`` / ``_graph`` / ``_icloud_url`` / ``_remail`` /
``_smailr``). But ``mailbox.py`` itself is not one of those facades: it is the
implementation *and* the private interface hub, and nothing counted its
consumers.

A 2026-09-17 survey found five more modules in the same position, so
``WATCHED_MODULES`` now covers all six. Each has a private definition count
that is close to its consumed-symbol count -- i.e. they too are private hubs
rather than modules with a thin private tail:

    module            private defs   distinct consumed symbols
    mailbox                     43                          20
    session_refresh             14                          11
    mail_otp                    15                          12
    sentinel_tokens             29                          12
    http_utils                   7                           9
    utils                       11                          16

The baselines are recorded **per module**, not as one grand total, so that
lowering one module's baseline cannot be masked by growth in another.

This is a **ratchet, not a cleanup task**. It records today's set of
``(importer, watched_module, symbol)`` triples and fails if the set grows. It
deliberately does not demand that existing consumers be rewritten -- each one
needs its own decision about whether the public seam should be widened or the
caller changed. Lowering a baseline is a conscious act:

    python scripts/mailbox_private_import_ratchet.py --update-baseline

Run it only after *removing* consumers, never after adding them.
``--update-baseline`` prints the per-module delta so a reviewer can see whether
the number moved for the right reason.

Known blind spots (deliberate)
------------------------------
1. Only ``from <...>.<watched> import _name`` counts. Attribute access through
   an imported module (``import sms_tool.mailbox`` then ``mailbox._foo()``) is
   not detected. A 2026-09-17 scan found that form only inside comments and
   docstrings, so it is not implemented until a real occurrence appears --
   guessing at dynamic-attribute detection would produce a gate that fires on
   prose.
2. ``tests/`` is not scanned (see ``SCAN_ROOTS``), so a private symbol reached
   only from a test is invisible here. That is intentional -- but it means the
   counts are "production consumers", not "all consumers".

The same-name trap
------------------
Symbols are grouped by their **defining module**, resolved from the import
statement itself, not by the file that happens to import them. ``_load_mailbox_pool``
is imported from ``.mailbox`` by ``sms_tool/cli.py`` and would otherwise be
attributed to ``cli``. Conversely ``sms_tool/mailbox_service.py`` imports
``_email_cfg`` *from* ``.mailbox``: the consumer is the service file, the watched
module is ``mailbox``. Grouping on the last dotted segment of the import -- and
nothing else -- is what keeps the two apart.

Standard library only: it runs as a pytest test and as a git hook.

Usage:
    python scripts/mailbox_private_import_ratchet.py                 # check
    python scripts/mailbox_private_import_ratchet.py --detail        # per-file
    python scripts/mailbox_private_import_ratchet.py --list          # every triple
    python scripts/mailbox_private_import_ratchet.py --module utils  # one module
    python scripts/mailbox_private_import_ratchet.py --update-baseline
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "mailbox_private_import_baseline.json"

# Scanned trees, relative to the repository root. ``tests/`` is excluded on
# purpose: a test asserting internal behaviour is not the same coupling as a
# production caller, and counting it would make "lower the baseline" mean
# "edit the tests".
SCAN_ROOTS = ("sms_tool", "scripts")

# Modules whose private surface is watched. Extend the tuple when another
# module is found to be serving as a private interface hub.
WATCHED_MODULES = ("mailbox", "session_refresh", "mail_otp", "sentinel_tokens", "http_utils", "utils")

_SKIP_DIRS = {"__pycache__"}

# Where each watched module may live. A bare ``from .X import`` is unambiguous,
# but ``from .providers.X import`` also ends in a watched name and would be
# counted as the same module -- wrong once ``X`` exists in two packages.
WATCHED_QUALIFIERS: dict[str, tuple[str, ...]] = {
    "mailbox": ("sms_tool",),
    "session_refresh": ("sms_tool",),
    "mail_otp": ("sms_tool", "sms_tool.providers"),
    "sentinel_tokens": ("sms_tool",),
    "http_utils": ("sms_tool",),
    "utils": ("sms_tool", "sms_tool.providers"),
}


# A watched hub's own file is never a consumer of its own surface. Kept as full
# relative paths, not bare stems: a nested ``sms_tool/paypal/utils.py`` is a
# different module and must still be scanned.
SELF_PATHS = frozenset(f"{root}/{name}.py" for root in SCAN_ROOTS for name in WATCHED_MODULES)


def _watched_module_of(node: ast.ImportFrom) -> str | None:
    """Return the watched module a ``from ... import`` statement targets, else ``None``.

    Accepts ``from .mailbox import``, ``from ..mailbox import`` and
    ``from sms_tool.mailbox import`` -- matched on the *last* dotted segment, so
    ``from .providers.mailbox_gmail import`` (a different module) is not caught,
    while the package guard rejects a same-named module outside ``sms_tool``.
    """
    module = node.module or ""
    if not module:
        return None
    last = module.split(".")[-1]
    if last not in WATCHED_MODULES:
        return None
    # A relative import (level > 0) whose last segment matched is one of the
    # package's own modules; an absolute import must name the package.
    if node.level == 0:
        prefix = module[: -len(last) - 1] if module != last else ""
        allowed = WATCHED_QUALIFIERS.get(last, ("sms_tool",))
        if not any(
            prefix == candidate or prefix.startswith(f"{candidate}.")
            for candidate in allowed
        ):
            return None
        return last
    # Relative (level > 0): the package qualifier is implied by the importing
    # file's own location, so no explicit check is possible here and one is not
    # attempted -- the pre-existing lenient behaviour for relative imports is
    # kept. ``SELF_PATHS`` excludes the hub files themselves, so a self-import
    # cannot inflate a count.
    return last


def _private_aliases(node: ast.ImportFrom) -> list[str]:
    """``_``-prefixed, non-dunder names imported from the watched module."""
    found = []
    for alias in node.names:
        name = alias.name
        if name.startswith("_") and not name.endswith("__"):
            found.append(name)
    return found


def _pairs_in_source(source: str, rel: str) -> set[tuple[str, str, str]]:
    """Return the ``(importer, watched_module, symbol)`` triples from one source text.

    Extracted so the predicate can be exercised on synthetic sources in both
    directions -- a gate that can only say "FAIL" is not a gate.  See
    ``tests/test_mailbox_private_import_ratchet.py``.
    """
    found: set[tuple[str, str, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        watched = _watched_module_of(node)
        if watched is None:
            continue
        for name in _private_aliases(node):
            found.add((rel, watched, name))
    return found


def collect(root: Path = ROOT) -> tuple[
    int, dict[str, int], dict[str, int], list[str]
]:
    """Return ``(total, {module: count}, {relative_path: count}, sorted_triples)``."""
    per_module: dict[str, int] = {name: 0 for name in WATCHED_MODULES}
    per_file: dict[str, int] = {}
    triples: set[tuple[str, str, str]] = set()

    for scan_root in SCAN_ROOTS:
        for path in sorted((root / scan_root).rglob("*.py")):
            if _SKIP_DIRS & set(path.parts):
                continue
            rel = path.relative_to(root).as_posix()
            # A watched module is not a consumer of its own surface. Compared on
            # the full relative path (``sms_tool/utils.py``), not the bare stem,
            # so a *different* file that merely shares the name -- e.g. a nested
            # ``sms_tool/paypal/utils.py`` -- is still scanned.
            if rel in SELF_PATHS:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                print(f"skip {path}: {exc}", file=sys.stderr)
                continue
            try:
                triples |= _pairs_in_source(source, rel)
            except SyntaxError as exc:
                print(f"skip {path}: {exc}", file=sys.stderr)
                continue

    for rel, watched, _name in triples:
        per_module[watched] = per_module.get(watched, 0) + 1
        per_file[rel] = per_file.get(rel, 0) + 1
    return (
        len(triples),
        per_module,
        per_file,
        sorted(f"{rel}:{watched}.{name}" for rel, watched, name in triples),
    )


def load_baseline(path: Path | None = None) -> dict:
    """Read the baseline. ``path`` defaults to the module constant *at call time*
    -- a default of ``BASELINE`` would freeze the value at import and make the
    constant un-patchable, which silently disables tests that redirect it."""
    return json.loads((path or BASELINE).read_text(encoding="utf-8"))


def _baseline_for_module(baseline: dict, module: str) -> dict:
    """Baseline slice for one watched module.

    Current baselines are shaped ``{"modules": {name: {...}}}``. The original
    single-module file was ``{"total": n, "per_file": {...}, "pairs": [...]}``
    with no ``modules`` key; that shape is still read so the migration did not
    have to be atomic with the code change.
    """
    modules = baseline.get("modules")
    if isinstance(modules, dict) and module in modules:
        return modules[module]
    if module == "mailbox" and "pairs" in baseline:
        return {
            "total": int(baseline.get("total") or 0),
            "per_file": baseline.get("per_file") or {},
            "pairs": baseline.get("pairs") or [],
        }
    return {"total": 0, "per_file": {}, "pairs": []}


def _triples_for_module(triples: list[str], module: str) -> list[str]:
    """Filter the flat ``importer:<module>.<symbol>`` list down to one module."""
    marker = f":{module}."
    return [item for item in triples if marker in item]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--detail", action="store_true", help="print per-file counts")
    parser.add_argument("--list", action="store_true", help="print every importer:module.symbol triple")
    parser.add_argument(
        "--module",
        action="append",
        choices=sorted(WATCHED_MODULES),
        help="restrict the report to one watched module (repeatable)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline with today's counts (only after removing consumers)",
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    modules = tuple(args.module) if args.module else WATCHED_MODULES
    total, per_module, per_file, triples = collect(args.root)

    if args.list:
        for item in triples:
            if any(f":{name}." in item for name in modules):
                print(item)

    if args.update_baseline:
        previous = load_baseline() if BASELINE.exists() else {}
        payload = {
            "modules": {
                name: {
                    "total": per_module.get(name, 0),
                    "per_file": {
                        rel: sum(
                            1
                            for item in triples
                            if item.startswith(f"{rel}:") and f":{name}." in item
                        )
                        for rel in sorted({item.split(":", 1)[0] for item in _triples_for_module(triples, name)})
                    },
                    "pairs": _triples_for_module(triples, name),
                }
                for name in sorted(WATCHED_MODULES)
            }
        }
        # newline="\n" is load-bearing: Path.write_text on Windows opens in TEXT
        # mode, so the "\\n" terminator below would become CRLF and the file
        # would differ by line ending alone across machines. Same trap that
        # corrupted a restore during this change's own validation.
        with BASELINE.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated: {len(WATCHED_MODULES)} watched modules, total {total}")
        for name in sorted(WATCHED_MODULES):
            before = int(_baseline_for_module(previous, name).get("total") or 0)
            after = per_module.get(name, 0)
            delta = after - before
            flag = " " if delta == 0 else ("+" if delta > 0 else "!")
            print(f"  {flag} {name:<16} {before:3d} -> {after:3d} ({delta:+d})")
            old = set(_baseline_for_module(previous, name).get("pairs") or [])
            for item in sorted(set(_triples_for_module(triples, name)) - old):
                print(f"      added:   {item}")
            for item in sorted(old - set(_triples_for_module(triples, name))):
                print(f"      removed: {item}")
        return 0

    if args.detail:
        for name in sorted(modules):
            print(f"  {name}: {per_module.get(name, 0)}")
        for name, count in sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"{count:4d}  {name}")
        print(f"{total:4d}  TOTAL")

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        return 2

    baseline = load_baseline(BASELINE)
    grown: list[tuple[str, int, int, list[str]]] = []
    for name in sorted(modules):
        slice_ = _baseline_for_module(baseline, name)
        allowed = int(slice_.get("total") or 0)
        current = per_module.get(name, 0)
        if current > allowed:
            new = sorted(set(_triples_for_module(triples, name)) - set(slice_.get("pairs") or []))
            grown.append((name, current, allowed, new))

    if grown:
        for name, current, allowed, new in grown:
            print(
                f"{name} private-import ratchet: {current} > baseline {allowed} "
                f"(+{current - allowed}). Remove the consumer, widen the public seam, "
                f"or justify a baseline bump.",
                file=sys.stderr,
            )
            for item in new:
                print(f"  new consumer: {item}", file=sys.stderr)
        return 1

    summary = ", ".join(f"{name}={per_module.get(name, 0)}" for name in sorted(modules))
    print(f"private-import ratchet OK ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
