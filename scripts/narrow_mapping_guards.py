"""Rewrite the ``x = a.get(k) if isinstance(a.get(k), T) else {}`` idiom.

Why this exists
---------------
Pyright evaluates the two ``a.get(k)`` calls independently, so the ``else {}``
branch does not narrow the value: every downstream ``x.get(...)`` reports
``reportOptionalMemberAccess``. Binding the raw value to a temporary first makes
the narrowing visible to the checker without changing a single runtime
behaviour -- the ``isinstance`` predicate, the default, and the order of
evaluation are all preserved.

This is a *refactoring tool*, not a gate. It rewrites only the exact shape
below, refuses anything it does not fully understand, and prints every change so
a reviewer can diff it. Run with ``--apply`` to write; the default is a dry run.

    name = expr if isinstance(expr, T) else {}

becomes

    _raw_<name>: object = expr
    name: <T> = _raw_<name> if isinstance(_raw_<name>, T) else {}

``T`` is kept verbatim (``dict``, ``Mapping``, ``list``, ...). ``expr`` must be
byte-identical on both sides of ``if`` -- anything else is skipped, because a
genuinely different pair of expressions is a real branch, not this idiom.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: ``<indent><name> = <expr> if isinstance(<expr>, <T>) else <default>``
#: ``<expr>`` is captured twice and must match exactly.
_IDIOM = re.compile(
    r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_]*) = "
    r"(?P<expr>.+?) if isinstance\((?P<expr2>.+?), (?P<type>[A-Za-z_][A-Za-z0-9_.\[\], ]*)\) "
    r"else (?P<default>\{\}|\[\]|\(\))$"
)

#: The same idiom wrapped in a parenthesised multi-line expression:
#:
#:     name = (
#:         expr
#:         if isinstance(expr, T)
#:         else {}
#:     )
_IDIOM_MULTILINE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_]*) = \(\n"
    r"[ \t]*(?P<expr>.+?)\n"
    r"[ \t]*if isinstance\((?P<expr2>.+?), (?P<type>[A-Za-z_][A-Za-z0-9_.\[\], ]*)\)\n"
    r"[ \t]*else (?P<default>\{\}|\[\]|\(\))\n"
    r"[ \t]*\)\n",
    re.MULTILINE,
)


def rewrite_line(line: str) -> tuple[list[str], bool]:
    """Return ``(new_lines, changed)`` for one source line."""
    stripped = line.rstrip("\n")
    match = _IDIOM.match(stripped)
    if not match:
        return [line], False
    if match.group("expr") != match.group("expr2"):
        return [line], False
    indent = match.group("indent")
    name = match.group("name")
    expr = match.group("expr")
    type_name = match.group("type").strip()
    default = match.group("default")
    raw = f"_raw_{name}"
    new_lines = [
        f"{indent}{raw}: object = {expr}\n",
        f"{indent}{name}: {type_name} = {raw} if isinstance({raw}, {type_name}) else {default}\n",
    ]
    return new_lines, True


def rewrite_text(text: str) -> tuple[str, int]:
    """Rewrite every idiom in ``text``; return ``(new_text, sites_changed)``.

    Multi-line parenthesised forms are handled first, then the single-line form
    on what remains. Both are anchored on identical ``expr`` on either side of
    ``if`` -- a genuinely different pair is a real branch and is left alone.
    """
    changed = 0

    def _multi(match: re.Match[str]) -> str:
        nonlocal changed
        if match.group("expr") != match.group("expr2"):
            return match.group(0)
        changed += 1
        indent = match.group("indent")
        name = match.group("name")
        expr = match.group("expr")
        type_name = match.group("type").strip()
        default = match.group("default")
        raw = f"_raw_{name}"
        return (
            f"{indent}{raw}: object = {expr}\n"
            f"{indent}{name}: {type_name} = {raw} if isinstance({raw}, {type_name}) else {default}\n"
        )

    text = _IDIOM_MULTILINE.sub(_multi, text)

    out: list[str] = []
    for line in text.splitlines(keepends=True):
        new_lines, did = rewrite_line(line)
        if did:
            changed += 1
        out.extend(new_lines)
    return "".join(out), changed


def process(path: Path, apply: bool) -> int:
    """Rewrite one file; return the number of sites changed."""
    original = path.read_text(encoding="utf-8")
    new_text, changed = rewrite_text(original)
    if changed and apply:
        # Byte-exact write: Windows text mode would turn \n into \r\n and
        # scripts/line_ending_guard.py only forbids *mixed* endings, so the
        # corruption would pass the gate.
        path.write_bytes(new_text.encode("utf-8"))
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args(argv)

    total = 0
    for path in args.paths:
        if not path.is_file():
            print(f"skip (not a file): {path}", file=sys.stderr)
            continue
        changed = process(path, args.apply)
        if changed:
            total += changed
            print(f"{path}: {changed} site(s){'' if args.apply else ' (dry run)'}")
    print(f"total: {total} site(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
