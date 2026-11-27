"""CI and local integrity guard for ``rust/*-rs/`` PyO3 acceleration crates.

Why this exists
---------------
``native/sentinel_vm/`` has always been compiled and tested in CI, while the
``rust-anything``-managed PyO3 crates under ``rust/*-rs/`` had only doc-count
checks in pytest and no compilation or manifest check in CI. A broken
``src/lib.rs``, a stale ``unit`` path after a Python refactor, an accidentally
introduced ``unsafe`` block, or a corrupted ``cases.jsonl`` could land on
``main`` unnoticed.

What this script checks
-----------------------
1. Every ``rust/*-rs/`` directory carries ``rust-anything.json``, ``Cargo.toml``,
   ``Cargo.lock``, ``src/lib.rs``, and non-empty valid ``cases.jsonl`` (strict
   ``\\n`` splitting so Unicode line separators inside JSON strings stay intact).
2. Each manifest's ``unit`` resolves to an existing Python file in the repo and
   every declared exported function exists in that file's AST.
3. Zero ``unsafe`` keywords appear in ``src/*.rs`` outside comments.
4. If ``recorded-from.json`` exists, it parses as JSON and names the same unit.
5. With ``--cargo-check``: runs ``cargo check --manifest-path <crate>/Cargo.toml``
   across every crate using a shared ``CARGO_TARGET_DIR`` so shared dependencies
   (``pyo3``, ``serde``, ``regex``, ``sha2``) compile once instead of 47 times.
   ``cargo check`` type-checks and borrow-checks every crate + PyO3 binding
   without requiring link-time ``python312.lib``.

Usage
-----
    python scripts/rust_crates_guard.py               # static checks only (<1s)
    python scripts/rust_crates_guard.py --cargo-check # static + cargo check all crates

Output is ASCII-only (CI runs on Windows cp1252).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUST_DIR = ROOT / "rust"

_UNSAFE_RE = re.compile(r"\bunsafe\b")


def _strip_rust_comments(source: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", no_block)


def _python_defined_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def discover_crates(rust_dir: Path = RUST_DIR) -> list[Path]:
    if not rust_dir.is_dir():
        return []
    return [path for path in sorted(rust_dir.glob("*-rs")) if path.is_dir()]


def validate_crates(rust_dir: Path = RUST_DIR, repo_root: Path = ROOT) -> tuple[list[Path], list[str]]:
    crates = discover_crates(rust_dir)
    problems: list[str] = []
    if not crates:
        return [], [f"no rust/*-rs crates found under {rust_dir}"]

    ast_cache: dict[Path, set[str]] = {}
    for crate in crates:
        name = crate.name
        for required in ("rust-anything.json", "Cargo.toml", "Cargo.lock", "src/lib.rs", "cases.jsonl"):
            if not (crate / required).is_file():
                problems.append(f"{name}: missing required file {required}")

        manifest_path = crate / "rust-anything.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                problems.append(f"{name}: invalid rust-anything.json ({exc})")
                manifest = None
            if isinstance(manifest, dict):
                unit = str(manifest.get("unit") or "").strip()
                unit_path = repo_root / unit if unit else None
                if not unit or unit_path is None or not unit_path.is_file():
                    problems.append(f"{name}: manifest unit does not exist: {unit!r}")
                else:
                    if unit_path not in ast_cache:
                        try:
                            ast_cache[unit_path] = _python_defined_functions(unit_path)
                        except SyntaxError as exc:
                            problems.append(f"{name}: unit {unit} has syntax error ({exc})")
                            ast_cache[unit_path] = set()
                    defined = ast_cache[unit_path]
                    raw_exports = manifest.get("exports") or manifest.get("functions") or []
                    if not raw_exports:
                        problems.append(f"{name}: manifest declares no exported functions")
                    for item in raw_exports:
                        fn_name = item["name"] if isinstance(item, dict) else str(item)
                        if fn_name and fn_name not in defined:
                            problems.append(f"{name}: exported function {fn_name!r} not found in {unit}")

                recorded_path = crate / "recorded-from.json"
                if recorded_path.is_file():
                    try:
                        recorded = json.loads(recorded_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError) as exc:
                        problems.append(f"{name}: invalid recorded-from.json ({exc})")
                    else:
                        rec_unit = str((recorded or {}).get("unit") or "").strip()
                        if rec_unit and unit and rec_unit != unit:
                            problems.append(
                                f"{name}: recorded-from.json unit {rec_unit!r} disagrees with manifest {unit!r}"
                            )

        cases_path = crate / "cases.jsonl"
        if cases_path.is_file():
            lines = [line for line in cases_path.read_text(encoding="utf-8").split("\n") if line.strip()]
            if not lines:
                problems.append(f"{name}: cases.jsonl is empty")
            else:
                for idx, line in enumerate(lines, start=1):
                    try:
                        json.loads(line)
                    except ValueError as exc:
                        problems.append(f"{name}: cases.jsonl line {idx} invalid JSON ({exc})")
                        break

        src_dir = crate / "src"
        if src_dir.is_dir():
            for rs_file in sorted(src_dir.rglob("*.rs")):
                cleaned = _strip_rust_comments(rs_file.read_text(encoding="utf-8"))
                if _UNSAFE_RE.search(cleaned):
                    rel = rs_file.relative_to(crate).as_posix()
                    problems.append(f"{name}: forbidden unsafe keyword in {rel}")

    return crates, problems


def run_cargo_checks(crates: list[Path], *, target_dir: Path | None = None) -> list[str]:
    env = os.environ.copy()
    shared_target = target_dir or Path(env.get("CARGO_TARGET_DIR") or (Path(tempfile.gettempdir()) / "gpt_rs_check"))
    shared_target.mkdir(parents=True, exist_ok=True)
    env["CARGO_TARGET_DIR"] = str(shared_target)

    failures: list[str] = []
    for crate in crates:
        manifest = crate / "Cargo.toml"
        proc = subprocess.run(
            ["cargo", "check", "--manifest-path", str(manifest), "--quiet"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = " | ".join(detail[-4:]) if detail else f"exit {proc.returncode}"
            failures.append(f"{crate.name}: cargo check failed: {tail}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify rust/*-rs PyO3 crates")
    parser.add_argument("--cargo-check", action="store_true", help="Run cargo check across all rust/*-rs crates")
    parser.add_argument("--target-dir", type=Path, default=None, help="Shared CARGO_TARGET_DIR for --cargo-check")
    args = parser.parse_args(argv)

    started = time.monotonic()
    crates, problems = validate_crates()
    if problems:
        for item in problems:
            print(f"ERROR: {item}", file=sys.stderr)
        print(f"rust-crates-guard FAILED: {len(problems)} static issue(s) across {len(crates)} crate(s)")
        return 1

    if args.cargo_check:
        cargo_failures = run_cargo_checks(crates, target_dir=args.target_dir)
        if cargo_failures:
            for item in cargo_failures:
                print(f"ERROR: {item}", file=sys.stderr)
            print(f"rust-crates-guard FAILED: {len(cargo_failures)} cargo check failure(s)")
            return 1

    elapsed = time.monotonic() - started
    mode = "static + cargo check" if args.cargo_check else "static"
    print(f"rust-crates-guard OK ({len(crates)} crate(s), {mode}, {elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
