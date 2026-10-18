"""Ratchet: ``sms_tool/kernel/`` may import only the stdlib and its siblings.

Why this exists
---------------
The kernel split moved the seven leaf modules (``paths``, ``sanitizer``,
``timeouts``, ``env_loader``, ``driver_env``, ``failure_registry``,
``error_classification``) one package level down so a boundary can be enforced
by directory rather than by convention. The boundary is only real if something
checks it: a single ``from ..config import CFG`` inside the kernel would make
the kernel depend on the application again, and nothing else in the tree would
notice -- the import would resolve, the tests would pass, and the directory
would keep claiming a rule it no longer honours.

What is allowed
---------------
* stdlib and third-party imports;
* ``from .<sibling> import ...`` inside ``sms_tool/kernel/``;
* ``from sms_tool.kernel.<sibling> import ...``.

Anything that reaches an application module -- module-level or function-local,
since a delayed import is still a dependency -- is a violation. ``config`` is
deliberately **not** in the kernel yet: its ``validate_config`` needs the
registration-driver vocabulary, so it stays an application module until that
data is extracted.

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


def _package_of(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    return ".".join(rel.parts)


def _resolve(node: ast.ImportFrom, source_package: str) -> str:
    if not node.level:
        return node.module or ""
    parts = source_package.split(".")
    # ``from .x`` (level 1) stays in the source package; ``from ..x`` climbs.
    base = parts[: len(parts) - node.level + 1] if node.level > 1 else parts
    module = node.module or ""
    return ".".join([*base, module]) if module else ".".join(base)


def violations(root: Path = ROOT) -> list[str]:
    kernel_dir = root / "sms_tool" / "kernel"
    problems: list[str] = []
    for path in sorted(kernel_dir.glob("*.py")):
        package = _package_of(path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                targets.append(_resolve(node, package))
            elif isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            for target in targets:
                if not target.startswith("sms_tool"):
                    continue
                if target == KERNEL_PACKAGE or target.startswith(KERNEL_PACKAGE + "."):
                    continue
                problems.append(f"{path.relative_to(root).as_posix()}:{getattr(node, 'lineno', 0)}: imports {target}")
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
    problems = violations(root)
    if problems:
        print("kernel-boundary ratchet: application imports inside the kernel")
        for problem in problems:
            print("  " + problem)
        return 1
    modules = sorted(p.stem for p in kernel_dir.glob("*.py") if p.stem != "__init__")
    print(f"kernel-boundary ratchet OK ({len(modules)} module(s): {', '.join(modules)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
