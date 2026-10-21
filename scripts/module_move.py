"""Move one top-level module family into a subpackage, gates included.

Why this exists
---------------
The 2026-10-21 remediation moved module families out of the flat ``sms_tool/``
root one cluster at a time. The first attempt (``proxy_*``) did the code move
correctly and still could not go green, because five gates encode the module
names and paths directly:

* ``tests/governance/test_dependency_guard.py`` -- sees the bare
  ``from proxy_edge_probe import ...`` fallback that ``upi_link`` keeps for
  direct-script execution;
* ``scripts/docs_consistency_scan.py`` -- resolves ``proxy/routing.py:85`` style
  anchors, which stop resolving the moment the file moves;
* ``tests/governance/test_import_layer_ratchet.py`` -- pins the exact edge totals;
* ``scripts/delayed_import_ratchet.py`` -- keys documented cycles by the last
  dotted segment, so a rename silently reclassifies an edge;
* ``scripts/format_guard.py`` -- the rewritten imports are not ruff-formatted.

Doing those by hand per cluster is where the attempt was lost. This script does
the whole move in one reviewed step, so a cluster is one command plus a test run.

Usage:
    python scripts/module_move.py --spec scripts/move_specs/proxy.json --check
    python scripts/module_move.py --spec scripts/move_specs/proxy.json --apply

``--check`` validates the spec and prints the planned work without touching the
tree. ``--apply`` performs every step and then runs the gate list, exiting
non-zero if any gate fails (in which case ``git checkout .`` restores the tree).

Spec shape::

    {
      "package": "proxy",
      "layer": 5,
      "modules": {"proxy_bridge": "bridge", ...},
      "root_package_init": false,
      "documented_cycles": [["routing", "account_identity"]],
      "doc_row_old": "| Proxy authority ... | `proxy/entry.py`, ... |",
      "doc_row_new": "| Proxy authority ... | `proxy/` (...) |"
    }
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY_ROOTS = ("sms_tool", "tests", "scripts")
EXTRA_PY = ("start_proxy_pool.py", "verify_proxy.py", "chatgpt_phone_reg.py")
TEXT_ROOTS = (
    "sms_tool",
    "tests",
    "scripts",
    "docs",
    "SmsWorkbench",
    "SmsWorkbench.Contracts",
)
#: Historical snapshots quote old paths on purpose; rewriting them would falsify the record.
TEXT_EXCLUDED = ("docs/audits/",)
TEXT_FILES = (
    "README.md",
    "README_EN.md",
    "docs/architecture.md",
    "docs/directory-map.md",
    "docs/CONTEXT.md",
    "config.example.json",
    "config_schema.json",
    "ipc_schema.json",
)
JSON_BASELINES = (
    "scripts/pyright_baseline.json",
    "scripts/unused_import_baseline.json",
    "scripts/endpoints_literal_baseline.json",
    "scripts/lane_coupling_baseline.json",
    "scripts/bare_print_baseline.json",
)
GATES = (
    "scripts/root_module_ratchet.py",
    "scripts/import_layer_ratchet.py",
    "scripts/delayed_import_ratchet.py",
    "scripts/module_coverage_check.py",
    "scripts/architecture_scan.py",
    "scripts/docs_consistency_scan.py",
    "scripts/dependency_graph.py",
    "scripts/format_guard.py",
)


# --------------------------------------------------------------------------
# spec
# --------------------------------------------------------------------------


class Spec:
    def __init__(self, data: dict):
        self.package: str = data["package"]
        self.layer: int = int(data["layer"])
        self.modules: dict[str, str] = data["modules"]
        self.root_package_init: bool = bool(data.get("root_package_init", False))
        self.documented_cycles: list[list[str]] = data.get("documented_cycles", [])
        self.doc_row_old: str = data.get("doc_row_old", "")
        self.doc_row_new: str = data.get("doc_row_new", "")

    @property
    def moves(self) -> dict[str, str]:
        return {f"sms_tool.{old}": f"sms_tool.{self.package}.{new}" for old, new in self.modules.items()}

    @property
    def file_moves(self) -> dict[str, str]:
        return {f"sms_tool/{old}.py": f"sms_tool/{self.package}/{new}.py" for old, new in self.modules.items()}


def load_spec(path: pathlib.Path) -> Spec:
    return Spec(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# import rewriting
# --------------------------------------------------------------------------


def line_starts(text: str) -> list[int]:
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def module_of(rel: str) -> str:
    rel = rel[:-3] if rel.endswith(".py") else rel
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def resolve_relative(source_module: str, is_package: bool, level: int, base: str | None) -> str:
    parts = source_module.split(".")
    if not is_package:
        parts = parts[:-1]
    keep = len(parts) - (level - 1)
    if keep <= 0:
        return ""
    return ".".join(parts[:keep] + ([base] if base else []))


def relative_from(source_module: str, is_package: bool, target_module: str) -> tuple[int, str]:
    src_pkg = source_module if is_package else source_module.rsplit(".", 1)[0]
    src_parts = src_pkg.split(".")
    tgt_parts = target_module.split(".")
    common = 0
    while common < len(src_parts) and common < len(tgt_parts) and src_parts[common] == tgt_parts[common]:
        common += 1
    return len(src_parts) - common + 1, ".".join(tgt_parts[common:])


def rewrite_relative(source: str, old_module: str, new_module: str, is_package: bool, moves: dict[str, str]) -> str:
    """Rewrite every relative import in one file, from old_module to new_module."""
    tree = ast.parse(source)
    starts = line_starts(source)
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        target = resolve_relative(old_module, is_package, node.level, node.module)
        start = starts[node.lineno - 1] + node.col_offset
        end = starts[(node.end_lineno or node.lineno) - 1] + (node.end_col_offset or node.col_offset)
        if node.module is not None:
            mapped = moves.get(target, target)
            level, module = relative_from(new_module, is_package, mapped)
            prefix = "from " + "." * node.level + node.module
            if prefix == "from " + "." * level + module:
                continue
            text = source[start:end]
            edits.append((start, end, text.replace(prefix, "from " + "." * level + module, 1)))
            continue
        # `from . import a, b as c` -- each alias may name a module of its own,
        # and the *level* has to be recomputed even when nothing in the list
        # moved: the file itself is one directory deeper now. Missing that is
        # how ``from . import endpoints`` (``sms_tool.endpoints`` before the
        # move) started resolving to ``sms_tool.paypal.endpoints``.
        target = resolve_relative(old_module, is_package, node.level, None)
        level, module = relative_from(new_module, is_package, target)
        kept: list[str] = []
        extra: list[str] = []
        for alias in node.names:
            child = f"{target}.{alias.name}"
            binding = alias.asname or alias.name
            if child not in moves:
                kept.append(alias.name + (f" as {alias.asname}" if alias.asname else ""))
                continue
            parent, leaf = moves[child].rsplit(".", 1)
            child_level, parent_rel = relative_from(new_module, is_package, parent)
            extra.append(f"from {'.' * child_level}{parent_rel} import {leaf} as {binding}")
        statements: list[str] = []
        indent = " " * node.col_offset
        if kept:
            statements.append(f"from {'.' * level}{module} import " + ", ".join(kept))
        statements.extend(extra)
        if not statements:
            continue
        replacement = ("\n" + indent).join(statements)
        if replacement != source[start:end]:
            edits.append((start, end, replacement))
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source


def rewrite_root_package_imports(source: str, moves: dict[str, str]) -> str:
    """Rewrite ``from sms_tool import <moved>`` into the subpackage form.

    The dotted pass cannot see this form: the module name appears as a *binding*
    after ``import``, not as part of a dotted path, so ``from sms_tool import
    paypal_protocol`` survived every other pattern and left the module
    unimportable. Measured on the paypal cluster: 15 test modules failed
    collection on exactly this line.
    """
    tree = ast.parse(source)
    starts = line_starts(source)
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or node.module != "sms_tool":
            continue
        kept: list[str] = []
        extra: list[str] = []
        for alias in node.names:
            binding = alias.asname or alias.name
            full = f"sms_tool.{alias.name}"
            if full not in moves:
                kept.append(alias.name + (f" as {alias.asname}" if alias.asname else ""))
                continue
            parent, leaf = moves[full].rsplit(".", 1)
            extra.append(f"from {parent} import {leaf} as {binding}")
        if not extra:
            continue
        start = starts[node.lineno - 1] + node.col_offset
        end = starts[(node.end_lineno or node.lineno) - 1] + (node.end_col_offset or node.col_offset)
        indent = " " * node.col_offset
        statements: list[str] = []
        if kept:
            statements.append("from sms_tool import " + ", ".join(kept))
        statements.extend(extra)
        edits.append((start, end, ("\n" + indent).join(statements)))
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source


def rewrite_python_file(path: pathlib.Path, spec: Spec) -> bool:
    """Rewrite one file; returns True when it changed."""
    rel = path.relative_to(ROOT).as_posix()
    # After the move the file already sits at its new path, so its OLD module
    # name has to come from the reverse map -- resolving siblings against the new
    # name silently turns `from .proxy_entry` into `sms_tool.proxy.proxy_entry`.
    reverse = {new: old for old, new in spec.file_moves.items()}
    new_rel = rel
    old_module = module_of(reverse.get(rel, rel))
    new_module = module_of(rel)
    is_package = path.name == "__init__.py" or new_rel.endswith("__init__.py")
    original = path.read_text(encoding="utf-8")
    updated = rewrite_relative(original, old_module, new_module, is_package, spec.moves)
    updated = rewrite_root_package_imports(updated, spec.moves)
    if updated != original:
        path.write_text(updated, encoding="utf-8", newline="\n")
        return True
    return False


def rewrite_names_and_paths(spec: Spec) -> list[str]:
    """Absolute dotted names, bare fallback imports and file-path strings."""
    dotted = [(re.compile(r"\b" + re.escape(old) + r"\b"), new) for old, new in spec.moves.items()]
    bare = [
        (re.compile(r"^(\s*)from " + re.escape(old) + r" import ", re.M), rf"\1from {new} import ")
        for old, new in spec.moves.items()
    ]
    bare_import = [
        (re.compile(r"^(\s*)import " + re.escape(old) + r"$", re.M), rf"\1import {new}")
        for old, new in spec.moves.items()
    ]
    backslash = chr(92)
    separator = "[/" + backslash + backslash + "]"
    path_pairs = []
    for old, new in spec.modules.items():
        target = f"sms_tool/{spec.package}/{new}.py"
        path_pairs.append((re.compile("sms_tool" + separator + re.escape(old) + r"\.py"), target))
        path_pairs.append((re.compile("(['\"])" + re.escape(old) + r"\.py\1"), rf"\g<1>{spec.package}/{new}.py\g<1>"))
        # Documentation anchors: `proxy/entry.py:68` and `proxy/entry.py`. The
        # lookbehind is load-bearing: without it a *different* file that merely
        # ends in the moved name -- `providers/mailbox_gmail.py`, which did not
        # move -- was rewritten into a path that does not exist. Measured on the
        # 2026-10-21 mailbox cluster; four such rewrites had to be reverted by
        # hand. A bare `old.py` (no directory in front of it) still matches.
        path_pairs.append((re.compile(r"(?<![\w/\\])" + re.escape(old) + r"\.py(?=:|\b)"), f"{spec.package}/{new}.py"))

    changed: list[str] = []
    candidates: list[pathlib.Path] = []
    for base in TEXT_ROOTS:
        candidates.extend(sorted((ROOT / base).rglob("*")))
    candidates.extend(ROOT / rel for rel in TEXT_FILES)
    candidates.extend(ROOT / rel for rel in EXTRA_PY)
    for path in candidates:
        if not path.is_file() or path.suffix not in {".py", ".md", ".json", ".cs", ".xaml", ".ps1"}:
            continue
        if "__pycache__" in path.parts or "bin" in path.parts or "obj" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(rel.startswith(prefix) for prefix in TEXT_EXCLUDED):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        updated = text
        for pattern, new in dotted:
            updated = pattern.sub(new, updated)
        if path.suffix == ".py":
            for pattern, new in bare:
                updated = pattern.sub(new, updated)
            for pattern, new in bare_import:
                updated = pattern.sub(new, updated)
        for pattern, new in path_pairs:
            updated = pattern.sub(new, updated)
        if updated != text:
            path.write_text(updated, encoding="utf-8", newline="\n")
            changed.append(path.relative_to(ROOT).as_posix())
    return changed


def rename_json_keys(spec: Spec) -> list[str]:
    """Baselines keyed by module path: rename the key, keep the number."""
    changed: list[str] = []
    for rel in JSON_BASELINES:
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        updated = text
        for old, new in spec.file_moves.items():
            updated = updated.replace(f'"{old}"', f'"{new}"')
        if updated != text:
            json.loads(updated)  # a rename that breaks JSON must fail here, not later
            path.write_text(updated, encoding="utf-8", newline="\n")
            changed.append(rel)
    return changed


# --------------------------------------------------------------------------
# gate metadata
# --------------------------------------------------------------------------


def sync_layer_manifest(spec: Spec) -> None:
    path = ROOT / "scripts" / "layer_manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["packages"][spec.package] = spec.layer
    for old in spec.modules:
        data["root_modules"].pop(f"{old}.py", None)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def sync_subpackages(spec: Spec) -> None:
    path = ROOT / "scripts" / "import_layer_ratchet.py"
    text = path.read_text(encoding="utf-8")
    if f'"{spec.package}",' in text.split("SUBPACKAGES = (")[1].split(")")[0]:
        return
    text = text.replace("SUBPACKAGES = (\n", f'SUBPACKAGES = (\n    "{spec.package}",\n', 1)
    path.write_text(text, encoding="utf-8", newline="\n")


def sync_documented_cycles(spec: Spec) -> None:
    if not spec.documented_cycles:
        return
    path = ROOT / "scripts" / "delayed_import_ratchet.py"
    text = path.read_text(encoding="utf-8")
    anchor = "        # The five-module cycle:"
    lines = "\n".join(f'        ("{a}", "{b}"),' for a, b in spec.documented_cycles)
    text = text.replace(anchor, f"{lines}\n{anchor}", 1)
    path.write_text(text, encoding="utf-8", newline="\n")


def sync_directory_map(spec: Spec) -> None:
    groups = ROOT / "scripts" / "directory_map_groups.json"
    data = json.loads(groups.read_text(encoding="utf-8"))
    pattern = re.compile("sms_tool/" + re.escape(spec.package) + r"/([a-z_]+)\.py$")
    for name, paths in data["groups"].items():
        touched = [p for p in paths if pattern.match(p)]
        if not touched:
            continue
        kept = [p for p in paths if not pattern.match(p)]
        glob = f"sms_tool/{spec.package}/*.py"
        if glob not in kept:
            kept.append(glob)
        data["groups"][name] = kept
    groups.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    if spec.doc_row_old and spec.doc_row_new:
        doc = ROOT / "docs" / "directory-map.md"
        text = doc.read_text(encoding="utf-8")
        if spec.doc_row_old not in text:
            raise SystemExit("doc_row_old not found in docs/directory-map.md")
        doc.write_text(text.replace(spec.doc_row_old, spec.doc_row_new, 1), encoding="utf-8", newline="\n")


def sync_pinned_totals() -> None:
    """Rewrite the pinned totals in test_import_layer_ratchet from the live tree."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import import_layer_ratchet as ilr  # noqa: PLC0415

    totals = ilr.analyze(ROOT).totals()
    path = ROOT / "tests" / "governance" / "test_import_layer_ratchet.py"
    text = path.read_text(encoding="utf-8")
    for key in ("total_module_edges", "ordered_pairs", "mutual_pairs", "minority_edges", "delayed_cross_dir_edges"):
        text = re.sub(rf'("{key}": )\d+', rf"\g<1>{totals[key]}", text)
    path.write_text(text, encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=str(ROOT), check=check, text=True, capture_output=True)


def validate(spec: Spec) -> None:
    for old in spec.modules:
        source = ROOT / "sms_tool" / f"{old}.py"
        if not source.is_file():
            raise SystemExit(f"missing source module: sms_tool/{old}.py")
    for new in spec.modules.values():
        target = ROOT / "sms_tool" / spec.package / f"{new}.py"
        if target.exists():
            raise SystemExit(f"target already exists: {target.relative_to(ROOT)}")
    if (ROOT / "sms_tool" / spec.package).exists() and not spec.root_package_init:
        if not (ROOT / "sms_tool" / spec.package / "__init__.py").exists():
            raise SystemExit(f"sms_tool/{spec.package}/ exists without __init__.py")


def apply(spec: Spec) -> int:
    package_dir = ROOT / "sms_tool" / spec.package
    package_dir.mkdir(parents=True, exist_ok=True)
    if not (package_dir / "__init__.py").exists():
        (package_dir / "__init__.py").write_text(
            f'"""{spec.package.capitalize()} helpers, split out of the package root on 2026-10-21."""\n\n'
            "from __future__ import annotations\n",
            encoding="utf-8",
            newline="\n",
        )
    for old, new in spec.modules.items():
        run(["git", "mv", f"sms_tool/{old}.py", f"sms_tool/{spec.package}/{new}.py"])

    touched = 0
    for base in PY_ROOTS:
        for path in sorted((ROOT / base).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if rewrite_python_file(path, spec):
                touched += 1
    for rel in EXTRA_PY:
        path = ROOT / rel
        if path.is_file() and rewrite_python_file(path, spec):
            touched += 1
    print(f"relative imports rewritten in {touched} file(s)")

    print(f"names/paths rewritten in {len(rewrite_names_and_paths(spec))} file(s)")
    print(f"baseline keys renamed in {len(rename_json_keys(spec))} file(s)")

    sync_subpackages(spec)
    sync_layer_manifest(spec)
    sync_documented_cycles(spec)
    sync_directory_map(spec)

    run([sys.executable, "-m", "ruff", "format", "sms_tool", "tests", "scripts", *EXTRA_PY])
    run([sys.executable, "scripts/dependency_graph.py", "--write"])
    for script in (
        "scripts/root_module_ratchet.py",
        "scripts/import_layer_ratchet.py",
        "scripts/delayed_import_ratchet.py",
    ):
        attempts = 2
        for attempt in range(attempts):
            result = run([sys.executable, script, "--update-baseline"], check=False)
            if result.returncode == 0:
                break
            time.sleep(0.5)
        print(f"{script} --update-baseline -> {result.returncode}")
        if result.returncode != 0:
            print(result.stdout + result.stderr)
    sync_pinned_totals()

    failures = []
    for script in GATES:
        result = run([sys.executable, script], check=False)
        status = "OK" if result.returncode == 0 else "FAIL"
        print(f"  {status:4s} {script}")
        if result.returncode != 0:
            failures.append((script, (result.stdout + result.stderr).strip()))
    for script, output in failures:
        print(f"\n--- {script} ---\n{output}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move a module family into a subpackage, gates included.")
    parser.add_argument("--spec", type=pathlib.Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="validate the spec and print the plan")
    group.add_argument("--apply", action="store_true", help="perform the move")
    args = parser.parse_args(argv)

    spec = load_spec(args.spec)
    validate(spec)
    print(f"package: sms_tool/{spec.package} (layer {spec.layer})")
    for old, new in spec.modules.items():
        print(f"  sms_tool/{old}.py -> sms_tool/{spec.package}/{new}.py")
    if args.check:
        print(f"{len(spec.modules)} module(s) ready to move; run --apply to perform it")
        return 0
    return apply(spec)


if __name__ == "__main__":
    raise SystemExit(main())
