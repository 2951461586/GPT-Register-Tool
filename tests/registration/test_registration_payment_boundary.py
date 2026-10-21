import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REGISTRATION_MODULES = (
    "sms_tool/registration/orchestrator.py",
    "sms_tool/registration/handlers.py",
    "sms_tool/registration/state.py",
    "sms_tool/registration/progress.py",
    "sms_tool/registration/concurrency.py",
    "sms_tool/account_creation.py",
)
FORBIDDEN_PAYMENT_MODULES = {
    "payment_batch",
    "payment_link_manager",
    "payment_adapters",
    "regional_payment_adapter",
    "wallet_provider",
    "wallet_transport",
    "gcash_provider",
    "gcash_transport",
}


def test_registration_modules_do_not_import_payment_implementations():
    violations = []
    for relative in REGISTRATION_MODULES:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
            for name in names:
                leaf = name.rsplit(".", 1)[-1]
                if leaf in FORBIDDEN_PAYMENT_MODULES:
                    violations.append(f"{relative}:{getattr(node, 'lineno', 0)}:{name}")
    assert violations == []
