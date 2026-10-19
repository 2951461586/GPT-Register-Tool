"""Regenerate the ratchet numbers quoted in the architecture docs.

Why this exists
---------------
``docs/architecture.md`` quoted "287 module-level edges over 31 directory pairs"
while ``scripts/import_layer_ratchet.py`` measured 267 / 28. Nothing recomputed
the quote, so it drifted the moment the code moved -- and a doc that claims to be
the architecture authority cannot carry numbers nobody checks.

The repo already solved this *shape* for ``file.py:NNN`` pointers with
``scripts/refresh_doc_symbol_lines.py`` (bare run reports drift and exits 1,
``--apply`` rewrites). This is the same contract for *measurements*::

    python scripts/refresh_doc_ratchet_numbers.py           # report drift, exit 1
    python scripts/refresh_doc_ratchet_numbers.py --apply   # rewrite in place

Numbers are taken by **importing each ratchet's own analysis functions**, never by
parsing its stdout: a wording change in a print statement must not be able to
silently stop the generator. A block whose numbers cannot be recomputed is an
error, not a guess.

Blocks carry their id in the marker pair so a hand edit inside one is caught and a
missing or duplicated marker is a hard failure::

    <!-- ratchet-numbers:begin architecture -->
    ...
    <!-- ratchet-numbers:end architecture -->

Scope note: only measurements with a committed script behind them belong in a
block. The old sentence "the exact graph has six cycles over 16 of 278 modules"
is a pointer now, because there is no exact import-graph tool in ``scripts/`` --
inventing that tool was deliberately out of scope for this change.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent

#: Where the documents live. Separate from ``ROOT`` (the analysis tree) so a test
#: can regenerate a scratch copy of a doc while the ratchets still measure the
#: real repository.
DOCS_ROOT = ROOT

#: doc (root-relative) -> block id. One block per document today.
BLOCKS: dict[str, str] = {
    "docs/architecture.md": "architecture",
    "docs/current/protocol-payment-enhancement.md": "payment",
}


def _tool(name: str):
    """Import a sibling ratchet script by path (they are not a package)."""
    spec = importlib.util.spec_from_file_location(f"_ratchet_{name}", SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def render_architecture() -> list[str]:
    """Import layer, delayed imports and directory-map coverage."""
    import_layer = _tool("import_layer_ratchet")
    totals = import_layer.analyze(ROOT).totals()

    delayed = _tool("delayed_import_ratchet")
    delayed_total, _per_file = delayed.count_delayed_imports(ROOT / "sms_tool")
    delayed_allowed = int(delayed.load_baseline().get("total") or 0)

    coverage = _tool("module_coverage_check")
    tracked = {path for path in coverage.tracked_paths("sms_tool") if path.endswith(".py")}
    unclaimed, claims, rows_seen = coverage.check(tracked=tracked)
    named, _duplicates = coverage.coverage_counts(tracked, claims)

    return [
        "| Measure | Value |",
        "| --- | --- |",
        f"| Module-level cross-directory edges | {totals['total_module_edges']} |",
        f"| Directory pairs | {totals['ordered_pairs']} |",
        f"| Mutual pairs / minority-direction edges | {totals['mutual_pairs']} / {totals['minority_edges']} |",
        f"| Delayed cross-directory edges | {totals['delayed_cross_dir_edges']} |",
        f"| Delayed imports in the package | {delayed_total} (baseline {delayed_allowed}) |",
        f"| Claimed tracked `sms_tool` modules | {len(named)} of {len(tracked)} "
        f"({len(unclaimed)} unclaimed, {rows_seen} directory-map rows) |",
    ]


def render_payment() -> list[str]:
    """Extractor parity per pair plus the provider-decoupling buckets."""
    parity = _tool("extractor_parity_report")
    counts, _reports = parity.collect(ROOT)

    decoupling = _tool("provider_decoupling_ratchet")
    summary = decoupling.analyze(ROOT).summary()

    lines = ["| Extractor pair | AST-identical functions |", "| --- | ---: |"]
    for key in sorted(counts):
        label = f"`{key}`" + (" (negative control)" if key == "momo:kakao" else "")
        lines.append(f"| {label} | {counts[key]} |")
    lines.append("")
    lines.append(
        f"Same-named definitions against `common/`: **{summary['delegating']} delegating**, "
        f"**{summary['identical']} verbatim copies**, **{summary['divergent']} deliberate forks**."
    )
    return lines


RENDERERS = {
    "architecture": render_architecture,
    "payment": render_payment,
}


def begin_marker(block_id: str) -> str:
    return f"<!-- ratchet-numbers:begin {block_id} -->"


def end_marker(block_id: str) -> str:
    return f"<!-- ratchet-numbers:end {block_id} -->"


def render_block(block_id: str) -> str:
    return "\n".join([begin_marker(block_id), *RENDERERS[block_id](), end_marker(block_id)])


def extract_block(text: str, block_id: str) -> str | None:
    """The current block, or ``None`` when its markers are missing/duplicated."""
    begin, end = begin_marker(block_id), end_marker(block_id)
    if text.count(begin) != 1 or text.count(end) != 1:
        return None
    start = text.index(begin)
    stop = text.index(end) + len(end)
    return text[start:stop] if stop > start else None


def drift_message(doc: str, block_id: str, current: str, expected: str) -> str:
    current_lines = [line for line in current.splitlines() if line not in expected.splitlines()]
    expected_lines = [line for line in expected.splitlines() if line not in current.splitlines()]
    detail = []
    for line in current_lines:
        detail.append(f"  - {line}")
    for line in expected_lines:
        detail.append(f"  + {line}")
    return (
        f"{doc}: the '{block_id}' ratchet block is stale "
        f"({len(current_lines)} line(s) to remove, {len(expected_lines)} to add)\n"
        + "\n".join(detail)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the ratchet numbers quoted in the architecture docs."
    )
    parser.add_argument("--apply", action="store_true", help="rewrite the blocks in place")
    args = parser.parse_args(argv)

    failures: list[str] = []
    changed = 0
    for doc, block_id in BLOCKS.items():
        path = DOCS_ROOT / doc
        if not path.is_file():
            failures.append(f"{doc}: missing document")
            continue
        text = path.read_bytes().decode("utf-8")
        current = extract_block(text, block_id)
        if current is None:
            failures.append(
                f"{doc}: expected exactly one marker pair "
                f"'{begin_marker(block_id)}' / '{end_marker(block_id)}'"
            )
            continue
        expected = render_block(block_id)
        if current == expected:
            continue
        if args.apply:
            path.write_bytes(text.replace(current, expected).encode("utf-8"))
            changed += 1
            continue
        failures.append(drift_message(doc, block_id, current, expected))

    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(
            "\nRun `python scripts/refresh_doc_ratchet_numbers.py --apply` to regenerate "
            "the block(s); never edit the numbers by hand.",
            file=sys.stderr,
        )
        return 1
    if args.apply:
        print(f"ratchet-number blocks updated: {changed}")
    else:
        print(f"ratchet-number blocks OK ({len(BLOCKS)} block(s) up to date)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
