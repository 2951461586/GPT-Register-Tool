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
    storage_accounts = PY_ROOT / "store" / "accounts.py"
    try:
        storage_text = storage_accounts.read_text(encoding="utf-8")
        if re.search(r"(?:from|import)\s+[^\n]*mailbox_(?:remail|gmail|smailr|cfworker|graph|chongzhi|icloud)", storage_text):
            failures.append("sms_tool/store/accounts.py imports a concrete mailbox provider")
    except OSError:
        failures.append("cannot read sms_tool/store/accounts.py")
    # Provider implementations have one physical home. Top-level mailbox_* and
    # outlook_imap modules are compatibility facades and must not grow logic.
    for name in ("cfworker", "gmail", "graph", "icloud_url", "remail", "smailr"):
        facade = ROOT / "sms_tool" / f"mailbox_{name}.py"
        implementation = ROOT / "sms_tool" / "providers" / f"mailbox_{name}.py"
        if not implementation.is_file():
            failures.append(f"missing provider implementation: {implementation.relative_to(ROOT)}")
        try:
            tree = ast.parse(facade.read_text(encoding="utf-8"))
            if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for node in tree.body):
                failures.append(f"{facade.relative_to(ROOT)} is a compatibility facade but defines implementation symbols")
        except (OSError, SyntaxError):
            failures.append(f"cannot parse provider facade: {facade.relative_to(ROOT)}")
    outlook_facade = ROOT / "sms_tool" / "outlook_imap.py"
    if not (ROOT / "sms_tool" / "providers" / "outlook_imap.py").is_file():
        failures.append("missing provider implementation: sms_tool/providers/outlook_imap.py")
    else:
        try:
            tree = ast.parse(outlook_facade.read_text(encoding="utf-8"))
            if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for node in tree.body):
                failures.append("sms_tool/outlook_imap.py is a compatibility facade but defines implementation symbols")
        except (OSError, SyntaxError):
            failures.append("cannot parse provider facade: sms_tool/outlook_imap.py")
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
