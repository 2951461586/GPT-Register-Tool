"""Ratchet for function-level (delayed) imports inside ``sms_tool/``.

Why this exists
---------------
``sms_tool`` carries a few hundred imports that are executed inside a function
body instead of at module top level. Some are load-bearing -- they break a real
import cycle, or defer an optional dependency so a missing extra does not take
down the CLI at startup. But the pattern is invisible: nothing stops the next
one from being added "just to be safe", and a delayed import is also the one
form of import that hides a cycle from any top-level dependency scan.

This is a **ratchet, not a cleanup task**. Every delayed import is frozen by
``(importer, target)`` and carries a **reason**:

``cycle``
    The edge is one of the documented function-level cycles in
    ``docs/architecture.md`` §Known function-level lazy cycles. Lifting it would
    close a real cycle at import time.
``startup``
    The importer is the CLI/command seam (``cli.py``, ``commands/``,
    ``cli_parsers/``) or the target is a heavy optional stack (``requests``,
    ``curl_cffi``, ``playwright``, ``camoufox``, ``psutil``, ...): paying for it
    at import time would slow every command, including ``--help``.
``provider``
    The target is a provider/browser subsystem (``providers.*``,
    ``registration_drivers.*``, ``sentinel``, the browser/fingerprint pools,
    captcha): imported on the path that needs it, not when the package loads.
``test-seam``
    A path that exists so tests can redirect the store (``store`` ->
    ``sms_tool.storage``). It is 0 today -- the database-path provider replaced
    it -- and the category stays so that reintroducing one is a visible decision.
``deferred``
    Everything else: a sibling module imported late to keep the module-level
    graph acyclic or the import light. A **new** ``deferred`` edge cannot be
    adopted without an entry in ``scripts/delayed_import_reasons.json`` that says
    *why*; the other reasons are self-evident from the rule that derived them.

``delayed_import_reasons.json`` is also the override file: an entry there wins
over the derived reason (each needs a ``why``), which is how a mis-classified
edge is corrected in one place instead of by editing the rules.

    python scripts/delayed_import_ratchet.py                 # check vs baseline
    python scripts/delayed_import_ratchet.py --detail        # per-file + per-reason
    python scripts/delayed_import_ratchet.py --update-baseline

``--update-baseline`` prints every new edge with its reason; it refuses to adopt
a new ``deferred`` edge that has no written reason. Removing imports is always
allowed and should be followed by a baseline update.

Standard library only: it runs as a pytest test and as a git hook.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "sms_tool"
BASELINE = Path(__file__).resolve().parent / "delayed_import_baseline.json"
REASONS_FILE = Path(__file__).resolve().parent / "delayed_import_reasons.json"

_SKIP_DIRS = {"__pycache__"}

#: Documented function-level cycles (``docs/architecture.md`` §Known
#: function-level lazy cycles). Keyed by the *last* dotted segment so a later
#: package move does not silently reclassify the edge.
DOCUMENTED_CYCLES: frozenset[tuple[str, str]] = frozenset(
    {
        ("checkout_contract", "payment_catalog"),
        ("payment_catalog", "checkout_contract"),
        ("payment_catalog", "payment_flow"),
        ("payment_flow", "payment_catalog"),
        ("payment_routing", "paypal_proxy"),
        ("paypal_proxy", "payment_routing"),
        ("sentinel_tokens", "client"),
        ("client", "sentinel_tokens"),
        # The five-module cycle: account_identity -> fingerprint_pool ->
        # paypal_proxy -> payment_routing -> proxy_routing -> account_identity.
        ("proxy_routing", "account_identity"),
        ("fingerprint_pool", "paypal_proxy"),
    }
)

#: Importing these at module scope pays for a browser/HTTP/native stack.
HEAVY_TARGETS: frozenset[str] = frozenset(
    {
        "requests",
        "curl_cffi",
        "playwright",
        "camoufox",
        "nodriver",
        "selenium",
        "browserforge",
        "cloakbrowser",
        "qrcode",
        "psutil",
        "PIL",
        "httpx",
    }
)

#: Exact provider package roots (``from .sentinel import x`` resolves here).
PROVIDER_TARGETS: frozenset[str] = frozenset({"sms_tool.sentinel"})

#: Provider/browser subsystems loaded on the path that needs them.
PROVIDER_PREFIXES: tuple[str, ...] = (
    "sms_tool.providers.",
    "sms_tool.registration_drivers.",
    "sms_tool.sentinel.",
    "sms_tool.browser_pool",
    "sms_tool.browser_fingerprint_pool",
    "sms_tool.fingerprint_pool",
    "sms_tool.captcha_solver",
    "sms_tool.nodriver_captcha",
    "sms_tool.chatgpt_bootstrap",
    "sms_tool.page_truth",
    "sms_tool.humanize",
)

CLI_SEAM_PREFIXES: tuple[str, ...] = ("sms_tool/cli.py", "sms_tool/commands/", "sms_tool/cli_parsers/")

REASONS: tuple[str, ...] = ("cycle", "startup", "provider", "test-seam", "deferred")


@dataclass(frozen=True)
class DelayedEdge:
    """One delayed import statement, resolved to ``(importer, target)``."""

    importer: str
    target: str
    reason: str

    @property
    def key(self) -> str:
        return f"{self.importer} -> {self.target}"


def _delayed_imports(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """Import statements nested inside a function body.

    A class body is *not* delayed: it runs at import time, so it behaves like a
    top-level import for cycle and startup-cost purposes.
    """
    found: list[ast.Import | ast.ImportFrom] = []

    def walk(node: ast.AST, in_function: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            in_function = True
        if isinstance(node, (ast.Import, ast.ImportFrom)) and in_function:
            found.append(node)
        for child in ast.iter_child_nodes(node):
            walk(child, in_function)

    walk(tree, False)
    return found


def _module_of(importer: str) -> str:
    return importer.removesuffix(".py").replace("/", ".")


def resolve_target(importer: str, node: ast.Import | ast.ImportFrom) -> str:
    """Resolve one import statement to a dotted module, relative ones included.

    ``from . import endpoints`` inside ``sms_tool/delayed.py`` becomes
    ``sms_tool.endpoints``; a third-party import keeps its own name.
    """
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else ""
    if not node.level:
        return node.module or ""
    package = _module_of(importer).rsplit(".", 1)[0]
    parts = package.split(".") if package else []
    up = node.level - 1
    base = parts[: len(parts) - up] if up else parts
    prefix = ".".join(base)
    if node.module:
        return f"{prefix}.{node.module}" if prefix else node.module
    name = node.names[0].name if node.names else ""
    return f"{prefix}.{name}" if prefix else name


def _last_segment(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def classify(importer: str, target: str) -> str:
    """Derive the reason for one edge from the rules above."""
    if importer == "sms_tool/store/connection.py" and target == "sms_tool.storage":
        return "test-seam"
    # ``importer`` is a path (``sms_tool/payment_catalog.py``); compare module
    # names, not the path's last dotted segment (which would be ``py``).
    if (_last_segment(_module_of(importer)), _last_segment(target)) in DOCUMENTED_CYCLES:
        return "cycle"
    if importer.startswith(CLI_SEAM_PREFIXES):
        return "startup"
    if target.split(".", 1)[0] in HEAVY_TARGETS:
        return "startup"
    if target in PROVIDER_TARGETS or target.startswith(PROVIDER_PREFIXES):
        return "provider"
    return "deferred"


def load_reasons(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Overrides, keyed by ``"<importer> -> <target>"``; each carries a reason + why.

    ``path`` is resolved at call time, not bound as a default: tests redirect
    ``REASONS_FILE`` and a bound default would silently read the committed file.
    """
    path = path or REASONS_FILE
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    edges = payload.get("edges", {})
    if not isinstance(edges, dict):
        raise ValueError(f"{path.name}: 'edges' must be an object")
    for key, entry in edges.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{path.name}: edge {key!r} must be an object")
        reason = entry.get("reason")
        why = str(entry.get("why") or "").strip()
        if reason not in REASONS:
            raise ValueError(f"{path.name}: edge {key!r} has unknown reason {reason!r}")
        if len(why.split()) < 5:
            raise ValueError(f"{path.name}: edge {key!r} needs a 'why' of at least 5 words")
    return edges


def collect_delayed_edges(root: Path = PACKAGE, reasons: dict[str, dict[str, str]] | None = None) -> list[DelayedEdge]:
    """Every delayed import in the tree, with its classified reason."""
    overrides = load_reasons() if reasons is None else reasons
    edges: dict[tuple[str, str], DelayedEdge] = {}
    for path in sorted(root.rglob("*.py")):
        if _SKIP_DIRS & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"skip {path}: {exc}", file=sys.stderr)
            continue
        importer = path.relative_to(root.parent).as_posix()
        for node in _delayed_imports(tree):
            target = resolve_target(importer, node)
            override = overrides.get(f"{importer} -> {target}")
            reason = override["reason"] if override else classify(importer, target)
            edges.setdefault((importer, target), DelayedEdge(importer, target, reason))
    return [edges[key] for key in sorted(edges)]


def count_delayed_imports(root: Path = PACKAGE) -> tuple[int, dict[str, int]]:
    """Return ``(total_statements, {relative_path: count})`` for one package tree.

    Statement count (not edge count) is kept for the historical ``total`` gate;
    duplicates of the same target inside one file collapse in ``collect_delayed_edges``.
    """
    per_file: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        if _SKIP_DIRS & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"skip {path}: {exc}", file=sys.stderr)
            continue
        count = len(_delayed_imports(tree))
        if count:
            per_file[path.relative_to(root.parent).as_posix()] = count
    return sum(per_file.values()), per_file


def load_baseline(path: Path | None = None) -> dict:
    return json.loads((path or BASELINE).read_text(encoding="utf-8"))


def reason_counts(edges: list[DelayedEdge]) -> dict[str, int]:
    counts = {reason: 0 for reason in REASONS}
    for edge in edges:
        counts[edge.reason] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze delayed imports by (importer, target) with a reason label."
    )
    parser.add_argument("--detail", action="store_true", help="print per-file and per-reason counts")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline; new 'deferred' edges need a written reason",
    )
    parser.add_argument("--root", type=Path, default=PACKAGE)
    args = parser.parse_args(argv)

    total, per_file = count_delayed_imports(args.root)
    try:
        reasons = load_reasons()
    except ValueError as exc:
        print(f"delayed-import reasons invalid: {exc}", file=sys.stderr)
        return 2
    edges = collect_delayed_edges(args.root, reasons)
    by_key = {edge.key: edge for edge in edges}
    counts = reason_counts(edges)

    if args.detail:
        for name, count in sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"{count:4d}  {name}")
        print(f"{total:4d}  TOTAL statements, {len(edges)} distinct edges")
        for reason in REASONS:
            print(f"{counts[reason]:4d}  reason={reason}")

    if args.update_baseline:
        previous = load_baseline() if BASELINE.exists() else {}
        previous_edges = dict(previous.get("edges") or {})
        adopting = not previous_edges
        new_keys = sorted(set(by_key) - set(previous_edges))
        unexplained = [
            key for key in new_keys if by_key[key].reason == "deferred" and key not in reasons
        ]
        if unexplained and not adopting:
            print(
                "delayed-import ratchet: refusing to adopt new 'deferred' edges without a reason.\n"
                "Add each one to scripts/delayed_import_reasons.json (reason + why), or remove it:",
                file=sys.stderr,
            )
            for key in unexplained:
                print(f"  {key}", file=sys.stderr)
            return 1
        payload = {
            "total": total,
            "per_file": per_file,
            "reasons": counts,
            "edges": {edge.key: edge.reason for edge in edges},
        }
        # 🔴 ``newline="\n"`` is mandatory -- see the identical note in
        # ``bare_print_ratchet.py``: the ``newline=None`` default turns ``\n``
        # into CRLF on Windows, silently desyncing the worktree copy from its
        # LF index entry (``git diff`` cannot see it).
        BASELINE.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        delta = total - int(previous.get("total") or 0)
        print(
            f"baseline updated: {previous.get('total', 0)} -> {total} ({delta:+d}) statements, "
            f"{len(edges)} edges, {counts}"
        )
        if adopting:
            print(f"  adopted the pre-reason baseline: every edge is now labeled ({counts})")
        else:
            for key in new_keys:
                print(f"  added: {key} [{by_key[key].reason}]")
        return 0

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        return 2

    baseline = load_baseline()
    allowed = int(baseline.get("total") or 0)
    previous_edges = dict(baseline.get("edges") or {})
    if not previous_edges:
        print(
            "delayed-import ratchet: the baseline has no per-edge reasons yet; "
            "run `python scripts/delayed_import_ratchet.py --update-baseline` once to adopt it.",
            file=sys.stderr,
        )
        return 1

    new_keys = sorted(set(by_key) - set(previous_edges))
    changed = sorted(
        key for key in set(by_key) & set(previous_edges) if by_key[key].reason != previous_edges[key]
    )
    failures: list[str] = []
    if new_keys:
        failures.append("new delayed import(s):")
        failures.extend(f"  {key} [{by_key[key].reason}]" for key in new_keys)
    if changed:
        failures.append("reason changed (record an override in scripts/delayed_import_reasons.json):")
        failures.extend(f"  {key}: {previous_edges[key]} -> {by_key[key].reason}" for key in changed)
    if total > allowed:
        failures.append(f"statement count {total} > baseline {allowed} (+{total - allowed})")
    if failures:
        print("delayed-import ratchet FAILED", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        print(
            "Delete the import, or run --update-baseline after reviewing the new edges.",
            file=sys.stderr,
        )
        return 1

    removed = sorted(set(previous_edges) - set(by_key))
    suffix = f"; {len(removed)} edge(s) removed, trim the baseline" if removed else ""
    print(f"delayed-import ratchet OK: {total} statements, {len(edges)} edges, {counts}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
