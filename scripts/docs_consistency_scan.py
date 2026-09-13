"""Check live documentation pointers and current module layout.

Two tiers, because the docs mix two very different kinds of pointer:

* **Weak (all docs).** A `` `path.py:NNN` `` pointer must name a file that exists
  and a line that is in range. This catches the catastrophic case -- a module
  that was split or deleted, e.g. ``playwright.py:1518`` after the file became a
  27-line re-export shell. It cannot tell that ``base.py:9`` stopped being
  ``RegistrationDriver``, because the file merely grew.
* **Strong (symbol tables only).** A table row shaped
  ``| `Symbol` | `path.py:NNN` | ... |`` is resolved with ``ast`` and the symbol
  must actually be defined on that line. Symbol names and table layout survive
  refactors, so this catches the silent off-by-N drift the weak tier misses.

Anything the strong tier cannot resolve (env-var names, prose-anchored pointers
into a function body) is skipped rather than failed -- the gate must never fire
on a pointer it does not understand.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "sms_tool"

DOCS = (
    "README.md",
    "README_EN.md",
    "docs/README.md",
    "docs/CONTEXT.md",
    "docs/architecture.md",
    "docs/directory-map.md",
    "docs/registration-and-proxy-architecture.md",
    "docs/paypal-zero-due-link.md",
    "docs/protocol-payment-enhancement.md",
    # Current-state and ADR documents are canonical too; excluding them lets
    # stale symbol/line references survive CI even though maintainers read
    # these files as active contracts. Historical audits intentionally remain
    # outside the line-number gate because they preserve old snapshots.
    "docs/current/README.md",
    "docs/current/account-health.md",
    "docs/current/configuration.md",
    "docs/current/registration-architecture.md",
    "docs/current/registration-recovery.md",
    "docs/current/telemetry-and-runtime.md",
    "docs/adr/README.md",
    "docs/adr/0001-provider-layout.md",
    "docs/adr/0002-storage-events.md",
    "docs/adr/0003-driver-registry.md",
    "docs/adr/0004-browser-flow-split.md",
    "docs/adr/0005-cooperative-cancellation.md",
    "docs/adr/0006-browser-pool-and-pulse.md",
    "docs/adr/0007-email-verification-stuck.md",
    "docs/adr/0008-registration-result-contract.md",
    "docs/adr/0009-registration-hardening.md",
)

# ```mod.py:12``` or the shorthand ```:12``` (repeats the previous file on the line).
# ``.json`` is tracked too -- the architecture doc chains bare ```:227`` refs off a
# ```config.json:216`` lead -- but only ``.py`` pointers are validated, because
# config.json is gitignored and user-edited, so its line numbers are not ours to police.
REF = re.compile(r"`((?:[A-Za-z0-9_./]+/)?[A-Za-z0-9_]+\.(?:py|json)):(\d+)`|`:(\d+)`")
SYMBOL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
# `| col1 | col2 | ...` -- only the first two cells matter for symbol pointers.
TABLE_ROW = re.compile(r"^\|(.+?)\|(.+?)\|")


def _resolve_source(name: str) -> Path | None:
    for candidate in (ROOT / name, SRC / name):
        if candidate.is_file():
            return candidate
    return None


def _iter_ref_matches(text: str):
    """Yield ``(match, path|None, line_number, extension)`` resolving ``:NN`` shorthands.

    The match itself is handed to callers that need to rewrite the number in
    place (see ``scripts/refresh_doc_symbol_lines.py``); everything else should
    use :func:`_iter_refs` and never re-derive the shorthand rule.
    """
    last_file: str | None = None
    for match in REF.finditer(text):
        full, full_num, bare_num = match.group(1), match.group(2), match.group(3)
        if full is not None:
            last_file = full
            yield match, _resolve_source(full), int(full_num), full.rsplit(".", 1)[-1]
        elif last_file:
            yield match, _resolve_source(last_file), int(bare_num), last_file.rsplit(".", 1)[-1]
        else:
            yield match, None, int(bare_num), ""


def _iter_refs(text: str):
    """Yield ``(ref_text, path|None, line_number, extension)`` resolving ``:NN`` shorthands.

    A bare ```:NN`` repeats the file named by the last fully-qualified pointer on
    the same line, which is how the architecture doc writes ``/ `:539` ``.
    """
    for match, source, num, ext in _iter_ref_matches(text):
        yield match.group(0), source, num, ext


def _symbol_lines(path: Path) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, int] = {}

    def record(name: str, lineno: int) -> None:
        found.setdefault(name, lineno)

    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            record(node.name, node.lineno)
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        record(f"{node.name}.{sub.name}", sub.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    record(target.id, node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            record(node.target.id, node.lineno)
    return found


def check_line_refs(failures: list[str]) -> None:
    for rel in DOCS:
        path = ROOT / rel
        if not path.is_file():
            continue
        for lineno, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for ref_text, source, num, ext in _iter_refs(text):
                if ext != "py":
                    continue
                if source is None:
                    failures.append(f"{rel}:{lineno} unresolvable module in {ref_text}")
                    continue
                total = len(source.read_text(encoding="utf-8").splitlines())
                if num < 1 or num > total:
                    failures.append(
                        f"{rel}:{lineno} {ref_text} past EOF "
                        f"({source.relative_to(ROOT)} has {total} lines)"
                    )


def check_symbol_tables(failures: list[str]) -> None:
    cache: dict[Path, dict[str, int]] = {}
    for rel in DOCS:
        path = ROOT / rel
        if not path.is_file():
            continue
        for lineno, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            row = TABLE_ROW.match(text)
            if not row:
                continue
            symbols = SYMBOL.findall(row.group(1))
            refs = list(_iter_refs(row.group(2)))
            if not symbols or len(symbols) != len(refs):
                continue
            for symbol, (ref_text, source, num, ext) in zip(symbols, refs):
                if source is None or ext != "py":
                    continue
                if source not in cache:
                    cache[source] = _symbol_lines(source)
                actual = cache[source].get(symbol)
                if actual is None:
                    continue  # not a module-level definition -- gate stays silent
                if actual != num:
                    failures.append(
                        f"{rel}:{lineno} {symbol} documented at {ref_text} "
                        f"but defined at {source.relative_to(ROOT)}:{actual}"
                    )


def main() -> int:
    def release_key(path: Path) -> tuple[int, ...]:
        match = re.search(r"release-v(\d+(?:\.\d+)+)\.md$", path.name)
        return tuple(int(part) for part in match.group(1).split(".")) if match else ()

    # Release notes live in docs/releases/ (archived 2026-09-06).
    releases = sorted((ROOT / "docs" / "releases").glob("release-v*.md"), key=release_key)
    if not releases:
        print("No release notes found")
        return 1
    latest = releases[-1].name
    failures: list[str] = []
    for path in (ROOT / "README.md", ROOT / "README_EN.md", ROOT / "docs" / "README.md"):
        text = path.read_text(encoding="utf-8")
        if latest not in text:
            failures.append(f"{path.relative_to(ROOT)} does not point to {latest}")
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    if "Half-migrated" in architecture or "半迁移" in architecture:
        failures.append("architecture.md still describes providers as half-migrated")
    for path in (ROOT / "docs" / "directory-map.md", ROOT / "docs" / "architecture.md"):
        for ref in re.findall(r"`(sms_tool/providers/[^`]+\.py)`", path.read_text(encoding="utf-8")):
            if not (ROOT / ref).is_file():
                failures.append(f"missing documented path: {ref}")

    check_line_refs(failures)
    check_symbol_tables(failures)

    if failures:
        print("Documentation consistency check failed")
        print("\n".join(failures))
        print("hint: python scripts/refresh_doc_symbol_lines.py --apply")
        return 1
    print(f"Documentation consistency check passed ({latest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
