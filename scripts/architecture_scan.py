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
    if not (ROOT / "sms_tool" / "providers" / "outlook_imap_client.py").is_file():
        failures.append("missing provider implementation: sms_tool/providers/outlook_imap_client.py")
    else:
        try:
            tree = ast.parse(outlook_facade.read_text(encoding="utf-8"))
            if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for node in tree.body):
                failures.append("sms_tool/outlook_imap.py is a compatibility facade but defines implementation symbols")
        except (OSError, SyntaxError):
            failures.append("cannot parse provider facade: sms_tool/outlook_imap.py")
    # Account workflows have one physical home: sms_tool/accounts/.
    # No forwarding shell is kept at sms_tool/account_*.py on purpose -- a
    # shell would silently absorb the 50+ `patch("sms_tool.accounts.*")`
    # targets without production code ever reading it.
    stray_accounts = sorted(p.name for p in PY_ROOT.glob("account_*.py"))
    if stray_accounts:
        failures.append(
            "account workflow modules must live in sms_tool/accounts/, "
            f"found at package root: {', '.join(stray_accounts)}"
        )
    if not (PY_ROOT / "accounts" / "__init__.py").is_file():
        failures.append("missing sms_tool/accounts/__init__.py")
    # providers/ naming contract: low-level `_client` modules must never
    # import the registration-facing `mailbox_*` flows that compose them.
    for client in (PY_ROOT / "providers").glob("*_client.py"):
        try:
            tree = ast.parse(client.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            failures.append(f"cannot parse provider client: {client.relative_to(ROOT)}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                # Only the *providers-local* mailbox_* flows are upper layer.
                # sms_tool/mailbox_poll.py is shared polling infrastructure that
                # a client is allowed to reuse.
                # NOTE: ast.ImportFrom stores the module name WITHOUT the leading
                # dots -- the relative depth lives in ``node.level``.  Matching
                # on ".mailbox_" would silently never fire.
                # ``node.level == 1`` from inside providers/ resolves to
                # ``sms_tool.providers.<module>`` -- that IS the upper layer.
                # ``node.level == 2`` resolves to ``sms_tool.<module>``, which is
                # shared infrastructure (mailbox_poll) that a client may reuse.
                resolved_is_provider_flow = (
                    node.level == 1 and node.module.startswith("mailbox_")
                ) or node.module.startswith("sms_tool.providers.mailbox_")
                if resolved_is_provider_flow:
                    failures.append(
                        f"{client.relative_to(ROOT)} is a low-level client but imports "
                        f"the upper layer {node.module!r} (layering inversion)"
                    )
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
