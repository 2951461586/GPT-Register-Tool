import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRATION_MODULES = (
    "sms_tool/registration.py",
    "sms_tool/registration_handlers.py",
    "sms_tool/registration_state.py",
    "sms_tool/registration_progress.py",
    "sms_tool/registration_concurrency.py",
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
                    violations.append(f"{relative}:{node.lineno}:{name}")
    assert violations == []
