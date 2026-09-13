"""Rewrite ``file.py:NNN`` doc pointers from the symbol each pointer names.

``scripts/docs_consistency_scan.py`` is only half of the contract: it can *detect*
that ``| `Widget` | `widget.py:1` |`` drifted, but it cannot fix it, and its strong
tier only looks at **table rows**. The prose pointers that dominate
``docs/registration-and-proxy-architecture.md``
(`` `retarget_region()`（`proxy_entry.py:405`） ``) are policed by the weak tier
alone, which accepts any in-range line number. So every refactor that moves a
module-level definition leaves two kinds of rot behind:

* a red CI gate the next time somebody touches the file (tables), and
* silently wrong numbers that no test can see (prose).

This generator is the other half. It re-reads the symbol a pointer names, asks
``ast`` where that symbol actually lives today, and rewrites the number::

    python scripts/refresh_doc_symbol_lines.py --check   # report drift, exit 1
    python scripts/refresh_doc_symbol_lines.py --apply   # rewrite in place

A pointer is rewritten only when **all** of these hold:

1. **The symbol really is a module-level definition in that exact file.**
   Anything the resolver cannot map is left untouched, so a mis-paired name can
   never fabricate a number -- it can only fail to produce an edit.
2. **The pairing is unambiguous.** In a table row the symbol comes from cell 1
   and the pointer from cell 2, matched by position (the same rule the gate
   uses). In prose the symbol must be the *nearest* backticked name before the
   pointer, with no other name and no other pointer in between.
3. **The target is a ``.py`` file.** ``config.json`` is gitignored and
   user-edited; its line numbers are not ours to police.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import docs_consistency_scan as scan  # noqa: E402

# `` `Widget` `` or the prose call form `` `Widget()` ``.
NAME = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)(?:\(\))?`")

Cache = dict[Path, dict[str, int]]


def _resolve(source: Path | None, ext: str, symbol: str, cache: Cache) -> int | None:
    """Line the symbol is defined on, or ``None`` when this pointer is not ours."""
    if source is None or ext != "py":
        return None
    if source not in cache:
        try:
            cache[source] = scan._symbol_lines(source)
        except (OSError, SyntaxError):
            cache[source] = {}  # unreadable module: leave its pointers alone
    return cache[source].get(symbol)


def _number_span(match: re.Match) -> tuple[int, int]:
    """Character span of the line number inside a pointer match."""
    group = 2 if match.group(2) is not None else 3
    return match.start(group), match.end(group)


def _table_edits(text: str, cache: Cache) -> list[tuple[int, int, str, str]]:
    """Edits for ``| `Symbol` | `file.py:NNN` | ... |`` -- the gate's strong tier."""
    row = scan.TABLE_ROW.match(text)
    if not row:
        return []
    symbols = scan.SYMBOL.findall(row.group(1))
    refs = list(scan._iter_ref_matches(row.group(2)))
    if not symbols or len(symbols) != len(refs):
        return []
    edits = []
    base = row.start(2)
    for symbol, (match, source, num, ext) in zip(symbols, refs):
        actual = _resolve(source, ext, symbol, cache)
        if actual is None or actual == num:
            continue
        start, end = _number_span(match)
        edits.append((base + start, base + end, str(actual), symbol))
    return edits


def _prose_edits(text: str, cache: Cache) -> list[tuple[int, int, str, str]]:
    """Edits for pointers embedded in a sentence, e.g. `` `Widget()`（`w.py:1`） ``."""
    events: list[tuple[int, str, object]] = [
        (m.start(), "name", m.group(1)) for m in NAME.finditer(text)
    ]
    events += [
        (m.start(), "ref", (m, s, n, e)) for m, s, n, e in scan._iter_ref_matches(text)
    ]
    events.sort(key=lambda item: item[0])

    edits = []
    for index, (_, kind, payload) in enumerate(events):
        if kind != "ref" or index == 0 or events[index - 1][1] != "name":
            continue  # no name immediately in front -> pointer is not attributable
        match, source, num, ext = payload  # type: ignore[misc]
        symbol = events[index - 1][2]
        actual = _resolve(source, ext, symbol, cache)
        if actual is None or actual == num:
            continue
        start, end = _number_span(match)
        edits.append((start, end, str(actual), str(symbol)))
    return edits


def _rewrite(text: str, cache: Cache) -> tuple[str, list[tuple[str, str, str]]]:
    """Apply every resolvable edit right-to-left; return the new text and a changelog."""
    lines = text.splitlines(keepends=True)
    changes: list[tuple[str, str, str]] = []
    for lineno, line in enumerate(lines, 1):
        body = line.rstrip("\r\n")
        edits = _table_edits(body, cache) if scan.TABLE_ROW.match(body) else _prose_edits(body, cache)
        if not edits:
            continue
        newline = line[len(body):]
        for start, end, actual, symbol in sorted(edits, reverse=True):
            changes.append((f"{lineno}", f"{symbol}:{body[start:end]}", actual))
            body = body[:start] + actual + body[end:]
        lines[lineno - 1] = body + newline
    return "".join(lines), changes


def _scan(apply: bool) -> int:
    cache: Cache = {}
    drift = 0
    for rel in scan.DOCS:
        path = scan.ROOT / rel
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        updated, changes = _rewrite(original, cache)
        if not changes:
            continue
        drift += len(changes)
        print(f"{rel}: {len(changes)} pointer(s)")
        for lineno, before, after in changes:
            print(f"  line {lineno}: {before} -> {after}")
        if apply:
            path.write_text(updated, encoding="utf-8")
    if drift and not apply:
        print(f"\n{drift} pointer(s) drifted -- rerun with --apply")
    elif not drift:
        print("Doc symbol pointers are current")
    return 1 if drift and not apply else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rewrite the docs in place (default: report and exit 1 on drift)",
    )
    return _scan(parser.parse_args().apply)


if __name__ == "__main__":
    sys.exit(main())
