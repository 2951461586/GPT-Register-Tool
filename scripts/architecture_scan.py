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


#: Application modules a provider adapter must reach through
#: ``providers/ports.py`` instead of importing. Rule 3 forbade the registration and
#: payment workflow; the 2026-10-21 audit found two more: pool persistence and the
#: stdout/IPC channel. Both are things the routing owner knows, not the adapter.
#:
#: Entries are package-relative **paths**, not bare module names. The bare-name
#: form silently emptied this half of the gate on 2026-11-19: ``desktop_ipc.py``
#: and ``mailbox_pool_writer.py`` moved to ``desktop/ipc.py`` and
#: ``mailbox/pool_writer.py``, the absolute branch kept comparing
#: ``sms_tool.desktop`` against ``"desktop_ipc"``, and the check went green while
#: covering neither module. ``tests/governance/test_provider_ports.py`` pins every
#: entry to a module that still exists, so the next move fails a test instead of
#: emptying the gate.
PACKAGE_ROOT = "sms_tool"

PROVIDER_FORBIDDEN_APP_MODULES = ("desktop.ipc", "mailbox.pool_writer")

#: The one provider module allowed to name those modules: it is the seam that
#: receives them, and it imports them lazily so the module-level graph stays clean.
PROVIDER_PORTS_MODULE = "ports.py"

PROVIDER_PACKAGE = "providers"


def _package_relative_import(node: ast.ImportFrom) -> str | None:
    """The imported module as a path relative to the ``sms_tool`` package root.

    ``None`` means the import resolves outside the package (or above it), which is
    never one of the forbidden application modules.
    """
    module = node.module or ""
    if node.level == 0:
        if module == PACKAGE_ROOT:
            return ""
        prefix = PACKAGE_ROOT + "."
        return module[len(prefix) :] if module.startswith(prefix) else module
    # ``node.level`` counts from the *importing module*, so a file in
    # ``providers/`` is level 1 for ``providers`` and level 2 for the package root.
    if node.level == 1:
        return f"{PROVIDER_PACKAGE}.{module}" if module else PROVIDER_PACKAGE
    if node.level == 2:
        return module
    return None


def _covers(forbidden: str, candidate: str) -> bool:
    return candidate == forbidden or candidate.startswith(forbidden + ".")


def provider_port_violations(py_root: Path) -> list[str]:
    """Provider modules importing the application concerns they must receive."""
    failures: list[str] = []
    providers = py_root / "providers"
    if not providers.is_dir():
        return failures
    for path in sorted(providers.glob("*.py")):
        if path.name == PROVIDER_PORTS_MODULE:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            failures.append(f"cannot parse provider module: providers/{path.name}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            candidate = _package_relative_import(node)
            if candidate is None:
                continue
            if not any(_covers(entry, candidate) for entry in PROVIDER_FORBIDDEN_APP_MODULES):
                continue
            failures.append(
                f"providers/{path.name}:{node.lineno} imports the application module "
                f"{PACKAGE_ROOT}.{candidate}; receive it through providers/ports.py (Rule 3)"
            )
    return failures


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
        if re.search(
            r"(?:from|import)\s+[^\n]*mailbox_(?:remail|gmail|smailr|cfworker|graph|chongzhi|icloud)", storage_text
        ):
            failures.append("sms_tool/store/accounts.py imports a concrete mailbox provider")
    except OSError:
        failures.append("cannot read sms_tool/store/accounts.py")
    # Provider implementations have one physical home under sms_tool/providers/.
    # The legacy compatibility facades (sms_tool/mailbox/{cfworker..smailr}.py and
    # sms_tool/outlook_imap.py) were retired once zero production importers remained.
    for name in ("cfworker", "gmail", "graph", "icloud_url", "remail", "smailr"):
        implementation = ROOT / "sms_tool" / "providers" / f"mailbox_{name}.py"
        if not implementation.is_file():
            failures.append(f"missing provider implementation: {implementation.relative_to(ROOT)}")
        for stale in (ROOT / "sms_tool" / "mailbox" / f"{name}.py", ROOT / "sms_tool" / f"mailbox_{name}.py"):
            if stale.is_file():
                failures.append(f"retired provider facade still present: {stale.relative_to(ROOT)}")
    if not (ROOT / "sms_tool" / "providers" / "outlook_imap_client.py").is_file():
        failures.append("missing provider implementation: sms_tool/providers/outlook_imap_client.py")
    if (ROOT / "sms_tool" / "outlook_imap.py").is_file():
        failures.append("retired provider facade still present: sms_tool/outlook_imap.py")
    # Account workflows have one physical home: sms_tool/accounts/.
    # No forwarding shell is kept at sms_tool/account_*.py on purpose -- a
    # shell would silently absorb the 50+ `patch("sms_tool.accounts.*")`
    # targets without production code ever reading it.
    #
    # registration/account_creation.py and account_seed.py are exempt: both were relocated
    # to the package root on 2026-10-17 because every internal dependency
    # (http_utils, auth_headers, codex_sentinel, config, http_client, storage)
    # lives at the top level, and accounts/ itself only consumed 2 symbols from
    # account_creation via a delayed import.  They are top-level auth/seed
    # helpers that registration and payments reach inward toward, not account
    # lifecycle workflows.  Relocation cut the sms_tool->accounts minority edge
    # set 9 -> 5 and the total module edge count 287 -> 277.
    _ALLOWED_TOP_LEVEL_ACCOUNT_HELPERS = {"registration/account_creation.py", "account_seed.py"}
    stray_accounts = sorted(
        p.name for p in PY_ROOT.glob("account_*.py") if p.name not in _ALLOWED_TOP_LEVEL_ACCOUNT_HELPERS
    )
    if stray_accounts:
        failures.append(
            "account workflow modules must live in sms_tool/accounts/, "
            f"found at package root: {', '.join(stray_accounts)}"
        )
    if not (PY_ROOT / "accounts" / "__init__.py").is_file():
        failures.append("missing sms_tool/accounts/__init__.py")
    _LEGACY_ACCOUNTS_PREFIXED_MODULES = {
        "account_2fa.py",
        "account_cleanup.py",
        "account_email_change.py",
        "account_events.py",
        "account_health.py",
        "account_health_queue.py",
        "account_identity.py",
        "account_import.py",
        "account_inspection.py",
        "account_lifecycle.py",
        "account_liveness.py",
        "account_models.py",
        "account_promotion.py",
        "account_recovery.py",
        "account_scan.py",
        "account_terminal.py",
    }
    new_prefixed_accounts = sorted(
        p.name for p in (PY_ROOT / "accounts").glob("account_*.py") if p.name not in _LEGACY_ACCOUNTS_PREFIXED_MODULES
    )
    if new_prefixed_accounts:
        failures.append(
            "new modules in sms_tool/accounts/ must not use the redundant 'account_' prefix "
            f"(follow recovery_batch.py / recovery_policy.py naming): {', '.join(new_prefixed_accounts)}"
        )
    failures.extend(provider_port_violations(PY_ROOT))
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
                # sms_tool/mailbox/poll.py is shared polling infrastructure that
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
