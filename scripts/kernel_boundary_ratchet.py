"""Ratchet: ``sms_tool/kernel/`` and ``sms_tool/config/`` dependency boundaries.

Why this exists
---------------
``sms_tool/kernel/`` holds pure foundation primitives and may import only the
stdlib, third-party packages, and sibling kernel modules (module-level or
function-local).

``sms_tool/config/`` is the dependency-inversion hub above ``kernel/`` (~70
importers). Now that the registration-driver vocabulary lives in
``sms_tool/kernel/driver_env.py``, ``sms_tool/config/`` must carry **zero**
module-scope imports outside ``sms_tool.kernel`` and ``sms_tool.config``, and
its function-local imports may reach only the two explicit payment-catalog
validation seams (:data:`ALLOWED_CONFIG_DELAYED_TARGETS`).

Usage:
    python scripts/kernel_boundary_ratchet.py
    python scripts/kernel_boundary_ratchet.py --root <repo>
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL_PACKAGE = "sms_tool.kernel"
CONFIG_PACKAGE = "sms_tool.config"
ALLOWED_CONFIG_DELAYED_TARGETS = frozenset(
    {
        "sms_tool.payment.catalog",
        "sms_tool.payment.flow",
    }
)


def _package_of(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    return ".".join(rel.parts)


def _resolve(node: ast.ImportFrom, source_package: str, is_package_init: bool = False) -> str:
    if not node.level:
        return node.module or ""
    parts = source_package.split(".")
    parent_parts = parts if is_package_init else parts[:-1]
    up = node.level - 1
    base = parent_parts[: len(parent_parts) - up] if up else parent_parts
    module = node.module or ""
    return ".".join([*base, module]) if module else ".".join(base)


def _iter_imports(tree: ast.AST) -> list[tuple[ast.Import | ast.ImportFrom, bool]]:
    found: list[tuple[ast.Import | ast.ImportFrom, bool]] = []

    def walk(node: ast.AST, in_function: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            in_function = True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append((node, in_function))
        for child in ast.iter_child_nodes(node):
            walk(child, in_function)

    walk(tree, False)
    return found


def _targets_of(node: ast.Import | ast.ImportFrom, package: str, is_package_init: bool) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return [_resolve(node, package, is_package_init)]
    return [alias.name for alias in node.names]


def violations(root: Path = ROOT) -> list[str]:
    kernel_dir = root / "sms_tool" / "kernel"
    problems: list[str] = []
    for path in sorted(kernel_dir.glob("*.py")):
        is_init = path.name == "__init__.py"
        package = _package_of(path.parent if is_init else path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, _in_fn in _iter_imports(tree):
            for target in _targets_of(node, package, is_init):
                if not target.startswith("sms_tool"):
                    continue
                if target == KERNEL_PACKAGE or target.startswith(KERNEL_PACKAGE + "."):
                    continue
                problems.append(f"{path.relative_to(root).as_posix()}:{getattr(node, 'lineno', 0)}: imports {target}")
    return problems


def config_violations(root: Path = ROOT) -> list[str]:
    config_dir = root / "sms_tool" / "config"
    if not config_dir.is_dir():
        return []
    problems: list[str] = []
    allowed_prefixes = (KERNEL_PACKAGE, CONFIG_PACKAGE)
    for path in sorted(config_dir.glob("*.py")):
        is_init = path.name == "__init__.py"
        package = _package_of(path.parent if is_init else path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, in_fn in _iter_imports(tree):
            for target in _targets_of(node, package, is_init):
                if not target.startswith("sms_tool"):
                    continue
                if any(target == pfx or target.startswith(pfx + ".") for pfx in allowed_prefixes):
                    continue
                if in_fn and target in ALLOWED_CONFIG_DELAYED_TARGETS:
                    continue
                scope = "delayed import" if in_fn else "module-scope import"
                problems.append(
                    f"{path.relative_to(root).as_posix()}:{getattr(node, 'lineno', 0)}: disallowed {scope} {target}"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args(argv)
    root = Path(args.root)
    kernel_dir = root / "sms_tool" / "kernel"
    if not kernel_dir.is_dir():
        print("kernel-boundary ratchet: sms_tool/kernel/ is missing")
        return 1
    problems = [*violations(root), *config_violations(root)]
    if problems:
        print("kernel-boundary ratchet: boundary violation(s) in kernel/ or config/")
        for problem in problems:
            print("  " + problem)
        return 1
    modules = sorted(p.stem for p in kernel_dir.glob("*.py") if p.stem != "__init__")
    config_dir = root / "sms_tool" / "config"
    config_modules = (
        sorted(p.stem for p in config_dir.glob("*.py") if p.stem != "__init__") if config_dir.is_dir() else []
    )
    print(
        f"kernel-boundary ratchet OK ({len(modules)} kernel module(s): {', '.join(modules)}; "
        f"{len(config_modules)} config module(s) clean)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
