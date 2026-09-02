# -*- coding: utf-8 -*-
"""Detect configuration keys that nothing in the code ever reads.

Why this exists
---------------
Round 6 found 51 keys sitting in the live config shards that no code path
reads. They are not "set but defaulting" -- the leaf names never appear as a
string literal anywhere in ``sms_tool`` or ``services``. A one-off audit list
would go stale the moment somebody wires one up, so the check is a module:

* ``unread_config_keys()`` recomputes the set from source, so the answer is
  always "as of right now" rather than "as of the last audit".
* ``--doctor`` reports them, so a config value that silently does nothing is
  visible instead of assumed working.
* ``tests/test_config_usage.py`` pins the known list, so wiring a key up (or
  adding a new dead one) fails loudly instead of drifting.

How a key is judged
-------------------
A key counts as read when its **leaf name appears as a string literal** in the
sources. That deliberately ignores identifiers: ``run_single_link_mode()``
contains ``link_mode`` as part of a function name, and an early draft of this
check counted that as a hit for the ``paypal.link_mode`` / ``upi.link_mode``
config keys. Both are genuinely dead.

A key is also treated as read when its full dotted path appears literally,
which covers table-driven lookups such as ``get("paypal.link_mode")``.
"""
from __future__ import annotations

import ast
import io
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Config shards that make up the runtime configuration.
SHARD_NAMES = ("config.json", "proxy.json", "runtime.json", "payment.json")

#: Directories holding production source. ``dist/`` and ``runtime/`` contain
#: source *copies* and must stay excluded or every count comes out wrong.
SOURCE_DIRS = ("sms_tool", "services")
SKIP_DIRS = frozenset({"__pycache__", ".venv", "dist", "runtime", ".git", "logs", "sessions"})


class UnreadKey(NamedTuple):
    path: str
    shards: tuple[str, ...]
    in_example: bool

    @property
    def leaf(self) -> str:
        return self.path.rsplit(".", 1)[-1]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with io.open(str(path), "r", encoding="utf-8-sig", errors="replace") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def iter_leaf_paths(obj: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Yield ``(dotted.path, value)`` for every scalar leaf of a nested mapping.

    Keys that themselves contain a dot are skipped: those are mappings keyed by
    *data* rather than by config names, e.g.
    ``email_registration.smailr.domain_ids."smailr.com"``. Their values are
    looked up dynamically, so a literal check would flag every one of them and
    say nothing actionable.
    """
    if not isinstance(obj, dict):
        return
    for key, value in obj.items():
        if "." in str(key):
            continue
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            yield from iter_leaf_paths(value, path)
        else:
            yield path, value


def shard_leaf_paths(root: Path | str | None = None) -> dict[str, list[str]]:
    """Map every config leaf path to the shards that define it."""
    root = Path(root) if root else PROJECT_ROOT
    found: dict[str, list[str]] = {}
    for name in SHARD_NAMES:
        path = root / name
        if not path.is_file():
            continue
        for leaf, _value in iter_leaf_paths(_load_json(path)):
            found.setdefault(leaf, []).append(name)
    return found


def example_leaf_paths(root: Path | str | None = None) -> set[str]:
    root = Path(root) if root else PROJECT_ROOT
    return {leaf for leaf, _ in iter_leaf_paths(_load_json(root / "config.example.json"))}


def _iter_source_files(root: Path) -> Iterable[Path]:
    for name in SOURCE_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(str(base)):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                if filename.endswith(".py"):
                    yield Path(dirpath) / filename


@lru_cache(maxsize=1)
def source_string_literals(root: str | None = None) -> frozenset[str]:
    """Every string literal that appears in production source.

    Cached: ``--doctor`` calls this once, and re-parsing the whole tree on every
    lookup would be pointless work. The cache is per-process, which is the
    lifetime that matters for a CLI invocation.
    """
    root_path = Path(root) if root else PROJECT_ROOT
    literals: set[str] = set()
    for path in _iter_source_files(root_path):
        try:
            with io.open(str(path), "r", encoding="utf-8-sig", errors="replace") as handle:
                source = handle.read()
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
    return frozenset(literals)


def unread_config_keys(root: Path | str | None = None) -> list[UnreadKey]:
    """Config keys that are defined but never read as a literal in source.

    Covers two populations, because both are misleading in different ways:

    * keys in the live shards -- they look like active settings and quietly do
      nothing;
    * keys documented only in ``config.example.json`` -- nobody's runtime is
      affected, but anyone copying the template inherits dead settings.

    Both are returned with ``shards`` telling them apart (empty for the
    example-only ones).
    """
    root = Path(root) if root else PROJECT_ROOT
    literals = source_string_literals(str(root))
    example = example_leaf_paths(root)
    shard_paths = shard_leaf_paths(root)

    def is_read(path: str) -> bool:
        return path.rsplit(".", 1)[-1] in literals or path in literals

    unread: list[UnreadKey] = [
        UnreadKey(path, tuple(shards), path in example)
        for path, shards in sorted(shard_paths.items())
        if not is_read(path)
    ]
    documented_only = [
        UnreadKey(path, (), True)
        for path in sorted(example - set(shard_paths))
        if not is_read(path)
    ]
    return unread + documented_only


def format_unread_report(keys: list[UnreadKey]) -> str:
    """Human-readable block for ``--doctor``. ASCII only: the CI runner's
    stdout is cp1252 and a non-Latin-1 print aborts the step."""
    if not keys:
        return "  (none - every configured key is read somewhere)"
    live = [k for k in keys if k.shards]
    documented = [k for k in keys if not k.shards]
    lines = []
    if live:
        lines.append(f"  {len(live)} live-shard key(s) set but never read:")
        for key in live:
            marker = "  <- also in config.example.json" if key.in_example else ""
            lines.append(f"    - {key.path}  [{', '.join(key.shards)}]{marker}")
    if documented:
        lines.append(f"  {len(documented)} key(s) documented in config.example.json only:")
        for key in documented:
            lines.append(f"    - {key.path}")
    lines.append("  These values have no effect. Remove them or wire them up.")
    return "\n".join(lines)
