"""Static architecture guardrails for the Python/WPF boundary."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_ROOT = ROOT / "sms_tool"
WPF_ROOT = ROOT / "SmsWorkbench"


def _imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module.split(".")[0])
    return result


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    # CFG migration is tracked separately; this gate prevents new direct use in
    # newly introduced command/provider seams without breaking legacy modules.
    for path in PY_ROOT.glob("commands/*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.name != "config.py" and "from .config import CFG" in text:
            failures.append(f"{path.relative_to(ROOT)}: command seam imports CFG")
    for path in WPF_ROOT.glob("MainWindow*.cs"):
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"#if LEGACY_DELETE_CODE.*?#endif", "", text, flags=re.S)
        if "SqliteNative." in text and path.name != "MainWindow.Tasks.cs":
            warnings.append(f"{path.relative_to(ROOT)}: WPF direct SQLite access (migration debt)")
    if warnings:
        print("Architecture scan warnings:")
        print("\n".join(warnings))
    if failures:
        print("Architecture scan failed:")
        print("\n".join(failures))
        return 1
    print("Architecture scan passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
