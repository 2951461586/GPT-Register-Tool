"""Repair the ``narrow_mapping_guards.py`` output where it annotated a name that
already had an assignment in the same scope.

Why this is needed
------------------
``narrow_mapping_guards.py`` rewrites

    name = expr if isinstance(expr, T) else {}

into

    _raw_name: object = expr
    name: T = _raw_name if isinstance(_raw_name, T) else {}

When the original code had **two** statements -- a plain assignment followed by
an in-place narrowing, e.g.

    health = config.get("account_health") if isinstance(config, Mapping) else {}
    health = health if isinstance(health, Mapping) else {}

-- the rewrite leaves the first assignment in place. ``name: T = ...`` declares
the type for the whole function scope in pyright's model, so the earlier
``name = <wider type>`` now conflicts:

    Type "Any | dict | None" is not assignable to declared type "Mapping"

The repair collapses the pair into a single narrowing and drops the redundant
annotation, which restores the exact runtime behaviour (one read of ``expr``, one
``isinstance`` check, one default) and removes the conflict.

    name = <expr>
    _raw_name: object = name
    name: T = _raw_name if isinstance(_raw_name, T) else {}

becomes

    _raw_name: object = <expr>
    name = _raw_name if isinstance(_raw_name, T) else {}

Dry run by default; ``--apply`` writes byte-exact.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: ``<indent><name> = <expr>`` followed by the two rewritten lines.
#: ``<expr>`` is not compared against the temp's source; the temp must read
#: exactly ``name``, which is what ``narrow_mapping_guards.py`` emits.
_TRIPLE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_]*) = (?P<expr>.+)\n"
    r"[ \t]*_raw_(?P=name): object = (?P=name)\n"
    r"[ \t]*(?P=name): (?P<type>[A-Za-z_][A-Za-z0-9_.\[\], ]*) = "
    r"_raw_(?P=name) if isinstance\(_raw_(?P=name), (?P<type2>[A-Za-z_][A-Za-z0-9_.\[\], ]*)\) "
    r"else (?P<default>\{\}|\[\]|\(\))\n",
    re.MULTILINE,
)


def repair_text(text: str) -> tuple[str, int]:
    """Collapse ``assign + temp + annotated-narrow`` triples; return count."""
    changed = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal changed
        if match.group("type").strip() != match.group("type2").strip():
            return match.group(0)
        changed += 1
        indent = match.group("indent")
        name = match.group("name")
        expr = match.group("expr")
        type_name = match.group("type").strip()
        default = match.group("default")
        return (
            f"{indent}_raw_{name}: object = {expr}\n"
            f"{indent}{name} = _raw_{name} if isinstance(_raw_{name}, {type_name}) else {default}\n"
        )

    return _TRIPLE.sub(_sub, text), changed


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
        original = path.read_text(encoding="utf-8")
        new_text, changed = repair_text(original)
        if changed:
            total += changed
            print(f"{path}: {changed} site(s){'' if args.apply else ' (dry run)'}")
            if args.apply:
                path.write_bytes(new_text.encode("utf-8"))
    print(f"total: {total} site(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
