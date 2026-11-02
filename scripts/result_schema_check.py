"""Cross-language result-payload contract: Python produces it, C# reads it.

Why this exists
---------------
``tests/workbench/test_backend_cli_contract.py`` pins the *arguments* the desktop
sends to the backend. Nothing pinned the *answer*. ``BackendResultInterpreter``
reads payload keys by name and defaults every missing one to ``0``/``""``/``null``,
so a rename on the Python side degrades silently: the WPF label reads "完成 0/0",
the batch report carries a key nobody consumes, and both look like a normal run.

``result_schema.json`` is the declaration; this checker is the gate. Three rules,
and each one covers a real failure the other two cannot see:

1. **produced == declared.** The keys the declared builder returns must equal the
   declared key set. Catches an add/remove/rename on the Python side, in both
   directions, without needing anyone to remember this file exists.
2. **declared reads are real.** Every key a C# reader declares must still be read
   inside the named function (or modelled by the named type). Catches "delete the
   claim to make the check pass".
3. **declared reads are backend keys.** Every such key must be in the declared
   key set. Catches a rename that updated Python and the manifest but not C#.

A reader is declared in one of two shapes, because one of them is strictly
stronger and only applies where it is true:

* ``{"file", "function", "reads"}`` -- the function may serve several payload
  families (``BackendResultInterpreter.BatchSummaryLabel`` also reads the
  liveness/promotion keys), so only the keys it claims are pinned.
* ``{"file", "type", "reads"}`` -- the typed record belongs to exactly one
  family, so **every** ``[JsonPropertyName]`` on it must be declared, and the
  declared set is itself compared against what Python returns. That closes the
  hole the function form cannot close: C# starting to model a key nobody sends
  fails here (measured: adding an undeclared property reports ``file:line``).

A type shared by two families would need its own family declaration rather than a
relaxation of that rule; none exists today.

Stdlib only: this runs as a pre-commit gate, before dependencies are installed.

Usage:
    python scripts/result_schema_check.py            # check
    python scripts/result_schema_check.py --detail   # also print the parsed surface
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "result_schema.json"

#: Value kinds the manifest may declare. ``nullable`` is a separate flag so a
#: reader can tell "always an object" from "an object or null".
KNOWN_TYPES = frozenset({"int", "number", "bool", "string", "object", "list"})

#: ``[JsonPropertyName("x")]`` -- how a typed record names the wire key.
_PROPERTY_PATTERN = re.compile(r'JsonPropertyName\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"')

#: C# shapes that read a payload key from a parsed element. ``Text``/``Number``
#: are the interpreter's own helpers.
_READ_PATTERNS = (
    re.compile(r'(?:TryGetProperty|GetProperty)\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"'),
    re.compile(
        r'(?:Text|Number|String|Integer|Boolean)\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*"([A-Za-z_][A-Za-z0-9_]*)"'
    ),
)


# --------------------------------------------------------------------------- #
# Python side: the keys a builder actually returns
# --------------------------------------------------------------------------- #


def _functions(source: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(source)
    return {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _own_returns(node: ast.FunctionDef | ast.AsyncFunctionDef):
    """Return statements belonging to ``node`` itself, not to a nested def.

    ``ast.walk`` would also descend into a helper defined inside the builder, and
    a dict returned by that helper is not part of the payload. Merging the two
    would report keys nobody sends and, worse, could hide a rename.
    """
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Return):
            yield child
            continue
        stack.extend(ast.iter_child_nodes(child))


def builder_keys(source: str, builder: str, *, _depth: int = 0) -> set[str]:
    """String keys of every dict literal ``builder`` returns.

    A ``return other_builder(...)`` hop inside the same module is followed, so a
    thin wrapper still reports the real payload instead of an empty set (an empty
    set would make rule 1 fail open on a wrapper and look like a rename).
    """
    functions = _functions(source)
    node = functions.get(builder)
    if node is None:
        raise LookupError(f"builder {builder!r} not found")

    keys: set[str] = set()
    found_dict = False
    for returned in _own_returns(node):
        value = returned.value
        if isinstance(value, ast.Dict):
            found_dict = True
            for key in value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in functions
            and _depth < 3
        ):
            found_dict = True
            keys |= builder_keys(source, value.func.id, _depth=_depth + 1)

    if not found_dict:
        raise LookupError(f"builder {builder!r} returns no dict literal (checked return statements)")
    return keys


# --------------------------------------------------------------------------- #
# C# side: the keys one named function reads
# --------------------------------------------------------------------------- #


def _skip_string(source: str, index: int) -> int:
    """Return the index just past the string literal starting at ``index``.

    Handles ``"..."`` with escapes, ``@"..."`` with doubled quotes and
    ``\"\"\"...\"\"\"`` raw literals. Brace counting must not see the braces inside
    ``Loc.F("完成 {0}/{1}")`` -- that string is the reason this exists.
    """
    if source.startswith('"""', index):
        end = source.find('"""', index + 3)
        return len(source) if end < 0 else end + 3
    if source.startswith('@"', index):
        cursor = index + 2
        while cursor < len(source):
            if source[cursor] == '"':
                if cursor + 1 < len(source) and source[cursor + 1] == '"':
                    cursor += 2
                    continue
                return cursor + 1
            cursor += 1
        return cursor
    cursor = index + 1
    while cursor < len(source):
        char = source[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == '"':
            return cursor + 1
        cursor += 1
    return cursor


def _brace_body(source: str, index: int) -> tuple[str, int] | None:
    """Brace-match the block that starts at or after ``index``.

    Returns ``(body, first_line_number)``. Strings and comments are skipped, so a
    format string (``Loc.F("完成 {0}/{1}", ...)``) or a nested block cannot end the
    extraction early.
    """
    body_start = -1
    depth = 0
    while index < len(source):
        char = source[index]
        if char == '"' or source.startswith('@"', index) or source.startswith('"""', index):
            index = _skip_string(source, index)
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline
            continue
        if source.startswith("/*", index):
            closing = source.find("*/", index)
            index = len(source) if closing < 0 else closing + 2
            continue
        if char == "{":
            if body_start < 0:
                body_start = index
                depth = 1
            else:
                depth += 1
        elif char == "}" and body_start >= 0:
            depth -= 1
            if depth == 0:
                return source[body_start : index + 1], source.count("\n", 0, body_start) + 1
        index += 1
    return None


def csharp_function_body(source: str, function: str) -> tuple[str, int] | None:
    """``(body, first_line_number)`` for ``function``, or ``None``.

    Only a **declaration** is accepted: the search is anchored on an access
    modifier, so a call site earlier in the file (``return BatchSummaryLabel(x);``)
    cannot be mistaken for the body and brace-matched from the wrong place.
    """
    signature = re.search(
        rf"(?:public|private|internal|protected)\s+[^\n]{{0,200}}?\b{re.escape(function)}\s*\(",
        source,
    )
    if signature is None:
        return None
    return _brace_body(source, signature.end())


def csharp_type_body(source: str, type_name: str) -> tuple[str, int] | None:
    """``(body, first_line_number)`` for a ``class`` / ``record`` / ``struct``."""
    declaration = re.search(rf"\b(?:class|record|struct)\s+{re.escape(type_name)}\b", source)
    if declaration is None:
        return None
    return _brace_body(source, declaration.end())


def csharp_json_properties(body: str, first_line: int) -> dict[str, int]:
    """Map ``wire key -> line number`` of every ``[JsonPropertyName]`` in a type."""
    properties: dict[str, int] = {}
    for match in _PROPERTY_PATTERN.finditer(body):
        properties.setdefault(match.group(1), first_line + body.count("\n", 0, match.start()))
    return properties


def csharp_reads(body: str, first_line: int) -> dict[str, int]:
    """Map ``key -> line number`` of every payload key the body reads."""
    reads: dict[str, int] = {}
    for pattern in _READ_PATTERNS:
        for match in pattern.finditer(body):
            line = first_line + body.count("\n", 0, match.start())
            reads.setdefault(match.group(1), line)
    return reads


# --------------------------------------------------------------------------- #
# The check itself
# --------------------------------------------------------------------------- #


def _resolve_within(root: Path, relative: str) -> Path | None:
    """Resolve a manifest-declared path, refusing anything outside ``root``.

    The manifest is input: a typo (``../SmsWorkbench/...``) or a copied row from
    another layout must not make the gate read an unrelated file and then report
    a contract failure against the wrong text.
    """
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _module_path(root: Path, module: str) -> Path | None:
    return _resolve_within(root, module.replace(".", "/") + ".py")


def _validate_family_shape(family_id: str, family: object, failures: list[str]) -> dict | None:
    if not isinstance(family, dict):
        failures.append(f"{family_id}: family must be an object")
        return None

    python = family.get("python")
    if not isinstance(python, dict) or not python.get("module") or not python.get("builder"):
        failures.append(f"{family_id}: python.module and python.builder are required strings")
        return None

    keys = family.get("keys")
    if not isinstance(keys, dict) or not keys:
        failures.append(f"{family_id}: keys must be a non-empty object")
        return None

    for key, spec in keys.items():
        if not isinstance(spec, dict):
            failures.append(f"{family_id}.keys.{key}: must be an object")
            continue
        if spec.get("type") not in KNOWN_TYPES:
            failures.append(f"{family_id}.keys.{key}: unknown type {spec.get('type')!r} (known: {sorted(KNOWN_TYPES)})")
        for flag in ("required", "nullable"):
            if flag in spec and not isinstance(spec[flag], bool):
                failures.append(f"{family_id}.keys.{key}: {flag} must be true or false")

    readers = family.get("cs_readers")
    if not isinstance(readers, list) or not readers:
        failures.append(f"{family_id}: cs_readers must be a non-empty list")
        return None
    for reader in readers:
        if not isinstance(reader, dict) or not reader.get("file"):
            failures.append(f"{family_id}: each cs_readers entry needs a file")
            return None
        # Exactly one owner shape: a function reader pins the claims it makes
        # (the function may serve several families), a type reader owns its family.
        if bool(reader.get("function")) == bool(reader.get("type")):
            failures.append(f"{family_id}: cs_readers[{reader['file']}] needs exactly one of function or type")
            return None
        if not isinstance(reader.get("reads"), list):
            failures.append(f"{family_id}: cs_readers[{reader['file']}] needs a reads list")
            return None

    return family


def check(root: Path = ROOT) -> list[str]:
    """Return every contract violation; an empty list means the tree is honest."""
    failures: list[str] = []
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"{MANIFEST_NAME}: missing"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{MANIFEST_NAME}: invalid JSON ({exc})"]

    if manifest.get("version") != 1:
        failures.append(f"{MANIFEST_NAME}: version must be 1, found {manifest.get('version')!r}")

    families = manifest.get("families")
    if not isinstance(families, dict) or not families:
        return failures + [f"{MANIFEST_NAME}: families must be a non-empty object"]

    for family_id, raw_family in families.items():
        family = _validate_family_shape(family_id, raw_family, failures)
        if family is None:
            continue

        python = family["python"]
        declared = set(family["keys"])

        source_path = _module_path(root, python["module"])
        if source_path is None:
            failures.append(f"{family_id}: python.module {python['module']!r} resolves outside the repository")
            continue
        if not source_path.is_file():
            failures.append(f"{family_id}: python module file not found ({source_path.relative_to(root)})")
            continue
        try:
            produced = builder_keys(source_path.read_text(encoding="utf-8-sig"), python["builder"])
        except (LookupError, SyntaxError) as exc:
            failures.append(f"{family_id}: {exc}")
            continue

        missing = sorted(produced - declared)
        stale = sorted(declared - produced)
        if missing:
            failures.append(
                f"{family_id}: produced but not declared: {missing} "
                f"(add them to {MANIFEST_NAME}; C# cannot read what nobody declares)"
            )
        if stale:
            failures.append(
                f"{family_id}: declared but not produced: {stale} "
                f"(the builder no longer returns these -- renaming one breaks C# readers silently)"
            )

        for reader in family["cs_readers"]:
            reader_path = _resolve_within(root, reader["file"])
            if reader_path is None:
                failures.append(f"{family_id}: reader path {reader['file']!r} resolves outside the repository")
                continue
            if not reader_path.is_file():
                failures.append(f"{family_id}: reader file not found ({reader['file']})")
                continue
            text = reader_path.read_text(encoding="utf-8-sig")
            if "type" in reader:
                failures.extend(_check_type_reader(family_id, declared, reader, text))
            else:
                failures.extend(_check_function_reader(family_id, declared, reader, text))

    return failures


def _check_type_reader(family_id: str, declared: set[str], reader: dict, text: str) -> list[str]:
    """A typed record owns its family: every property and every claim is pinned.

    Unlike a function reader (which may serve several payload families and
    therefore only pins the keys it claims), the type belongs to one family, so a
    new ``[JsonPropertyName]`` **is** a contract change: it must be declared here,
    and the declaration is compared against what the Python builder returns. That
    is the rule a function scan cannot express.
    """
    failures: list[str] = []
    extracted = csharp_type_body(text, reader["type"])
    if extracted is None:
        return [f"{family_id}: type {reader['type']} not found in {reader['file']} (renamed or moved?)"]

    body, first_line = extracted
    properties = csharp_json_properties(body, first_line)

    for key in reader["reads"]:
        if key not in declared:
            failures.append(f"{family_id}: {reader['file']} declares reading {key!r}, which is not a declared key")
        elif key not in properties:
            failures.append(
                f"{family_id}: {reader['type']} declares reading {key!r} but has no "
                f'[JsonPropertyName("{key}")] property'
            )

    for key, line in sorted(properties.items(), key=lambda item: item[1]):
        if key not in declared:
            failures.append(
                f"{family_id}: {reader['file']}:{line} models {key!r} on {reader['type']}, "
                f"which {MANIFEST_NAME} does not declare for this family"
            )
    return failures


def _check_function_reader(family_id: str, declared: set[str], reader: dict, text: str) -> list[str]:
    failures: list[str] = []
    extracted = csharp_function_body(text, reader["function"])
    if extracted is None:
        return [
            f"{family_id}: function {reader['function']} not found in {reader['file']} "
            f"(moved or renamed? the declared reads below can no longer be verified)"
        ]

    body, first_line = extracted
    actual = csharp_reads(body, first_line)

    # A function may serve several payload families (BatchSummaryLabel also reads
    # the liveness/promotion keys), so only the declared claims are checked.
    for key in reader["reads"]:
        if key not in declared:
            failures.append(f"{family_id}: {reader['file']} declares reading {key!r}, which is not a declared key")
        elif key not in actual:
            failures.append(
                f"{family_id}: {reader['file']} no longer reads {key!r} in "
                f"{reader['function']} -- drop the claim or restore the read"
            )
    return failures


def describe(root: Path = ROOT) -> list[str]:
    """Human-readable surface, for ``--detail`` (never used by the gate)."""
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    lines: list[str] = []
    for family_id, family in manifest["families"].items():
        keys = family["keys"]
        required = sorted(key for key, spec in keys.items() if spec.get("required"))
        lines.append(f"{family_id}: {len(keys)} keys ({len(required)} required), {len(family['cs_readers'])} reader(s)")
        lines.append(f"  python: {family['python']['module']}.{family['python']['builder']}")
        for reader in family["cs_readers"]:
            target = reader.get("type") or reader.get("function")
            lines.append(f"  reads:  {reader['file']}::{target} -> {', '.join(reader['reads'])}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    unknown = [item for item in args if item != "--detail"]
    if unknown:
        print(f"unknown argument(s): {' '.join(unknown)}", file=sys.stderr)
        return 2

    # ``check(ROOT)``, not ``check()``: the default argument binds the module's
    # ROOT at definition time, so a patched ROOT (tests) would be ignored.
    failures = check(ROOT)
    if failures:
        # Report the failures first: ``--detail`` re-reads the manifest without
        # shape validation, so a malformed one would raise before this prints.
        print(f"result schema: FAILED ({len(failures)} problem(s))")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    if "--detail" in args:
        for line in describe(ROOT):
            print(line)
    print("result schema: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
