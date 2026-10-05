"""Ratchet: per-provider duplication against ``common/`` must not grow.

Why this exists
---------------
``services/protocol-payment/`` holds seven vendored payment adapters (blik,
ideal, twint, kakao, momo, pix, direct_card) that were each forked from the same
upstream script, plus a shared ``common/`` package the extractors were meant to
converge on. The migration to ``common/`` is **not** complete and, worse, it was
invisible: adding a fresh private copy of a helper that ``common/`` already owns
looked exactly like adding a new helper.

``docs/architecture.md`` Rule 19 is explicit that convergence is not free:

    *"A same-named function in two modules is not a duplicate until its AST
    proves it. ... Merging any of these changes production behaviour, so
    convergence is forbidden without fresh evidence and an explicit decision."*

So this ratchet does **not** demand convergence. It only makes the divergence
count visible and freezes it, exactly like ``extractor_parity_report.py`` freezes
the extractor pairing counts.

What it checks
--------------
For every ``.py`` under ``services/protocol-payment/<provider>/`` it collects the
top-level functions and matches them **by name** against every top-level function
defined under ``services/protocol-payment/common/``. Each match lands in exactly
one of three disjoint buckets:

``delegating``
    The body (docstring stripped) is a single ``return <call>(...)``. This is the
    migrated state -- the local name is a thin shim over ``common/``. Cost zero.
``identical``
     The body is AST-identical to the ``common/`` definition. Pure duplication
    with no behavioural question to settle, so it is the cheapest convergence
    candidate -- but still a convergence, so it is frozen rather than forced.
``divergent``
    Same name, independent body. Converging one of these *changes production
    behaviour* and needs evidence, so it is the number that must not grow.

The baseline is per file, not one grand total: with a single number, migrating
one extractor would mask a new fork added to another.

The classifier is pinned by ``FIXTURES`` on every invocation -- a synthetic
``common/`` + provider tree whose expected ``(identical, delegating, divergent)``
triple is asserted for each file, in both directions (a shim must not count as
divergent; an identical copy must not count as delegating). A classifier that
puts everything in one bucket fails before it judges the real tree.

Standard library only.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "provider_decoupling_baseline.json"

PAYMENTS = Path("services") / "protocol-payment"
COMMON_NAME = "common"
EXCLUDED_DIRS = {"__pycache__", "_vendor", "logs"}

DELEGATING = "delegating"
IDENTICAL = "identical"
DIVERGENT = "divergent"

#: The buckets that must never grow. ``delegating`` is the migration *state*: if
#: a shim is inlined back it stops delegating, so it reappears in one of these
#: two and is caught. Freezing ``delegating`` itself would forbid progress.
FROZEN = (IDENTICAL, DIVERGENT)


def _definitions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Top-level function definitions, by name."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return {}
    return {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _body_without_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _normalised_ast(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Behavioural fingerprint: call signature + body, positions stripped.

    The signature is **included**, because a body that matches verbatim under a
    different signature is not a safe convergence candidate. ``blik``'s
    ``build_email(first, last)`` has the same body shape as ``common``'s
    ``build_email(profile, first, last)`` but callers pass different arguments;
    reporting that as ``identical`` would invite exactly the silent behaviour
    change Rule 19 forbids. Annotations are dropped -- type hints do not change
    behaviour -- but argument names, count, ordering, defaults, ``*args`` and
    ``**kwargs`` all stay.
    """
    args = node.args
    stripped_args = ast.arguments(
        posonlyargs=[ast.arg(arg=a.arg) for a in args.posonlyargs],
        args=[ast.arg(arg=a.arg) for a in args.args],
        vararg=ast.arg(arg=args.vararg.arg) if args.vararg else None,
        kwonlyargs=[ast.arg(arg=a.arg) for a in args.kwonlyargs],
        kw_defaults=list(args.kw_defaults),
        kwarg=ast.arg(arg=args.kwarg.arg) if args.kwarg else None,
        defaults=list(args.defaults),
    )
    fingerprint = ast.FunctionDef(
        name="fingerprint",
        args=stripped_args,
        body=_body_without_docstring(node),
        decorator_list=[],
        returns=None,
        type_comment=None,
    )
    module = ast.Module(body=[fingerprint], type_ignores=[])
    return ast.dump(ast.fix_missing_locations(module), annotate_fields=True, include_attributes=False)


def _is_delegating(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A body that does nothing but hand off to another callable."""
    body = _body_without_docstring(node)
    if len(body) != 1:
        return False
    statement = body[0]
    if isinstance(statement, ast.Pass):
        return True
    return isinstance(statement, ast.Return) and isinstance(statement.value, ast.Call)


@dataclass
class Pair:
    """One same-named definition in a provider and in ``common/``."""

    provider_file: str
    name: str
    kind: str
    common_file: str


@dataclass
class Analysis:
    pairs: list[Pair] = field(default_factory=list)
    provider_files: list[str] = field(default_factory=list)

    def counts_for(self, provider_file: str) -> dict[str, int]:
        buckets = Counter(p.kind for p in self.pairs if p.provider_file == provider_file)
        return {kind: buckets[kind] for kind in (DELEGATING, IDENTICAL, DIVERGENT)}

    def summary(self) -> dict[str, int]:
        """Totals per kind, plus the count of files carrying a frozen bucket."""
        buckets = Counter(p.kind for p in self.pairs)
        frozen_files = {p.provider_file for p in self.pairs if p.kind in FROZEN}
        return {
            "same_named_total": len(self.pairs),
            DELEGATING: buckets[DELEGATING],
            IDENTICAL: buckets[IDENTICAL],
            DIVERGENT: buckets[DIVERGENT],
            "files_with_frozen_pairs": len(frozen_files),
        }


def _common_definitions(payments_root: Path) -> dict[str, tuple[str, str]]:
    """name -> (common-relative file, normalised body AST)."""
    common_root = payments_root / COMMON_NAME
    out: dict[str, tuple[str, str]] = {}
    if not common_root.is_dir():
        return out
    for path in sorted(common_root.rglob("*.py")):
        if EXCLUDED_DIRS & set(path.parts):
            continue
        for name, node in _definitions(path).items():
            rel = path.relative_to(payments_root).as_posix()
            out.setdefault(name, (rel, _normalised_ast(node)))
    return out


def analyze(root: Path = ROOT) -> Analysis:
    payments_root = root / PAYMENTS
    common = _common_definitions(payments_root)
    analysis = Analysis()

    if not payments_root.is_dir():
        return analysis

    for path in sorted(payments_root.rglob("*.py")):
        if EXCLUDED_DIRS & set(path.parts):
            continue
        if COMMON_NAME in path.relative_to(payments_root).parts:
            continue
        rel = path.relative_to(payments_root).as_posix()
        definitions = _definitions(path)
        if not definitions:
            continue
        analysis.provider_files.append(rel)
        for name, node in definitions.items():
            entry = common.get(name)
            if entry is None:
                continue
            common_file, common_ast = entry
            if _is_delegating(node):
                kind = DELEGATING
            elif _normalised_ast(node) == common_ast:
                kind = IDENTICAL
            else:
                kind = DIVERGENT
            analysis.pairs.append(Pair(rel, name, kind, common_file))
    return analysis


# --------------------------------------------------------------------------
# Fixtures: a throwaway payments tree with a pinned bucket assignment.
# --------------------------------------------------------------------------

FIXTURE_TREE: dict[str, str] = {
    "services/protocol-payment/common/__init__.py": "",
    "services/protocol-payment/common/core.py": (
        "def shared_add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def shared_scale(a, k):\n"
        "    return a * k\n"
        "\n"
        "def only_common():\n"
        "    return 1\n"
    ),
    "services/protocol-payment/alpha/__init__.py": "",
    # Fully migrated: both names are thin shims.
    "services/protocol-payment/alpha/delegating.py": (
        "def shared_add(a, b):\n"
        "    return common_shared_add(a, b)\n"
        "\n"
        "def shared_scale(a, k):\n"
        '    """docstring must not stop the shim being recognised."""\n'
        "    return common_shared_scale(a, k)\n"
    ),
    # Verbatim copy: identical, no behavioural question.
    "services/protocol-payment/alpha/copied.py": ("def shared_add(a, b):\n    return a + b\n"),
    # Independent logic: the frozen bucket.
    "services/protocol-payment/alpha/forked.py": (
        "def shared_add(a, b):\n"
        "    return a + b + 1\n"
        "\n"
        "def shared_scale(a, k):\n"
        "    return common_shared_scale(a, k) * 2\n"
    ),
    # A different signature with an identical-looking body is NOT a delegation
    # and NOT identical: converging it would change every caller's argument
    # binding, which is the mistake the signature-bearing fingerprint prevents.
    "services/protocol-payment/alpha/resignatured.py": ("def shared_add(a, b, c=0):\n    return a + b\n"),
    # A single non-call return must not be mistaken for a shim.
    "services/protocol-payment/alpha/lying.py": ("def shared_add(a, b):\n    return 5\n"),
    # Names that do not exist in common/ contribute nothing.
    "services/protocol-payment/alpha/unrelated.py": ("def private_helper():\n    return 1\n"),
    "services/protocol-payment/alpha/logs/ignored.py": ("def shared_add(a, b):\n    return a + b\n"),
}

#: file -> (identical, delegating, divergent)
FIXTURE_EXPECTED: tuple[tuple[str, int, int, int], ...] = (
    ("alpha/delegating.py", 0, 2, 0),
    ("alpha/copied.py", 1, 0, 0),
    ("alpha/forked.py", 0, 0, 2),
    ("alpha/resignatured.py", 0, 0, 1),
    ("alpha/lying.py", 0, 0, 1),
    ("alpha/unrelated.py", 0, 0, 0),
)


def _build_fixture_tree(base: Path) -> Path:
    for rel, text in FIXTURE_TREE.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return base


def classifier_is_trustworthy() -> str | None:
    """Return a failure description if the classifier fails its own fixtures."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        base = _build_fixture_tree(Path(tmp) / "repo")
        analysis = analyze(base)

        for provider_file, identical, delegating, divergent in FIXTURE_EXPECTED:
            counts = analysis.counts_for(provider_file)
            expected = {IDENTICAL: identical, DELEGATING: delegating, DIVERGENT: divergent}
            if counts != expected:
                problems.append(f"{provider_file}: expected {expected}, got {counts}")

        if any(p.provider_file.startswith("alpha/logs/") for p in analysis.pairs):
            problems.append("logs/ was not excluded")

        if not analysis.pairs:
            problems.append("classifier found no same-named definitions at all")

        # Guard against a classifier that dumps everything into one bucket and
        # would therefore pass a total-only check.
        kinds = {p.kind for p in analysis.pairs}
        if len(kinds) < 3:
            problems.append(f"classifier produced only {sorted(kinds)}; all three buckets must appear")
    return "; ".join(problems) if problems else None


def load_baseline(path: Path | None = None) -> dict[str, object]:
    if path is None:
        path = BASELINE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _baseline_files(baseline: dict[str, object]) -> dict[str, dict[str, int]]:
    files = baseline.get("files")
    if not isinstance(files, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for key, value in files.items():
        if not isinstance(value, dict):
            continue
        buckets: dict[str, int] = {}
        for kind in FROZEN:
            try:
                buckets[kind] = int(value.get(kind, 0))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                buckets[kind] = 0
        out[str(key)] = buckets
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ratchet on per-provider duplication against services/protocol-payment/common/."
    )
    parser.add_argument("--detail", action="store_true", help="print the per-file bucket table")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline (only after CONVERGING a provider, i.e. lowering it)",
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    broken = classifier_is_trustworthy()
    if broken is not None:
        print(f"SELF-TEST FAILED: {broken}", file=sys.stderr)
        print("provider-decoupling-ratchet: refusing to report with a broken classifier.", file=sys.stderr)
        return 1

    analysis = analyze(args.root)
    summary = analysis.summary()

    if args.detail:
        print(f"{'provider file':48s} {'deleg':>6s} {'ident':>6s} {'diverge':>8s}")
        print("-" * 74)
        for provider_file in sorted(set(analysis.provider_files)):
            counts = analysis.counts_for(provider_file)
            print(f"{provider_file:48s} {counts[DELEGATING]:6d} {counts[IDENTICAL]:6d} {counts[DIVERGENT]:8d}")
        print("\n=== frozen pairs (identical / divergent), with the common owner ===")
        for pair in sorted((p for p in analysis.pairs if p.kind in FROZEN), key=lambda p: (p.provider_file, p.name)):
            print(f"  {pair.kind:10s} {pair.provider_file}:{pair.name}  == {pair.common_file}")
        print("\n=== summary ===")
        for key, value in summary.items():
            print(f"  {key:26s} {value}")

    if args.update_baseline:
        files = {
            provider_file: analysis.counts_for(provider_file) for provider_file in sorted(set(analysis.provider_files))
        }
        payload = {"files": files, **summary}
        with BASELINE.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(
            f"baseline updated: {BASELINE.name} "
            f"({summary[DELEGATING]} delegating, {summary[IDENTICAL]} identical, "
            f"{summary[DIVERGENT]} divergent)"
        )
        return 0

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        print("run: python scripts/provider_decoupling_ratchet.py --update-baseline", file=sys.stderr)
        return 2

    baseline = load_baseline(BASELINE)
    allowed = _baseline_files(baseline)

    grown: list[tuple[str, str, int, int]] = []
    for provider_file in sorted(set(analysis.provider_files) | set(allowed)):
        counts = analysis.counts_for(provider_file)
        for kind in FROZEN:
            current = counts[kind]
            limit = allowed.get(provider_file, {}).get(kind, 0)
            if current > limit:
                grown.append((provider_file, kind, current, limit))

    if grown:
        for provider_file, kind, current, limit in grown:
            print(
                f"{provider_file}: {kind} {current} > baseline {limit} (+{current - limit}). "
                f"Import the shared helper from common/ (or add a thin shim over it), or -- if "
                f"the divergence is deliberate -- record the decision in docs/architecture.md "
                f"and bump the baseline in the same commit.",
                file=sys.stderr,
            )
        return 1

    print(
        f"provider-decoupling ratchet OK ({summary['same_named_total']} same-named def(s): "
        f"{summary[DELEGATING]} delegating, {summary[IDENTICAL]} identical, "
        f"{summary[DIVERGENT]} divergent)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
