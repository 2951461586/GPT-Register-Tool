"""Pin which module owns each registration stage body after the 2026-10-24 split.

``RegistrationEmailWorkflow`` keeps thin delegates so the 31 existing tests and
every ``patch.object(RegistrationEmailWorkflow, ...)`` keep working. That is a
deliberate compatibility seam, not a licence to leave a second copy of the body
behind: this guard asserts the body lives in exactly one owning module and that
handlers only forwards to it.

It also freezes ``handlers.py``'s line budget. The file is still large because it
keeps the stage order, the lifecycle and 31 two-line delegates; the next split
(auth/register) is expected to lower the ceiling, never raise it.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRATION = ROOT / "sms_tool" / "registration"
HANDLERS = REGISTRATION / "handlers.py"

#: workflow method -> (owning module file, free-function name)
MOVED_STAGES = {
    "_apply_reference_lane": ("bootstrap_stages.py", "apply_reference_lane"),
    "_run_bootstrap": ("bootstrap_stages.py", "run_bootstrap"),
    "_bootstrap": ("bootstrap_stages.py", "bootstrap"),
    "_device_identity_seeded_by_email_enabled": (
        "identity_stages.py",
        "device_identity_seeded_by_email_enabled",
    ),
    "_device_context": ("identity_stages.py", "device_context"),
    "extract_sentinel": ("identity_stages.py", "extract_sentinel"),
    "prepare_identity": ("identity_stages.py", "prepare_identity"),
    "_fingerprint_geo_metadata": ("identity_stages.py", "fingerprint_geo_metadata"),
    "_with_password_claim": ("auth_stages.py", "with_password_claim"),
    "_password_lane_active": ("auth_stages.py", "password_lane_active"),
    "auth_flow": ("auth_stages.py", "auth_flow"),
    "user_register": ("register_stages.py", "user_register"),
    "_user_register_passwordless": ("register_stages.py", "user_register_passwordless"),
    "_abort_user_register_failure": ("register_stages.py", "abort_user_register_failure"),
    "_post_user_register": ("register_stages.py", "post_user_register"),
}

#: Ceiling for handlers.py; lower it when the next body moves out.
HANDLERS_LINE_BUDGET = 560


def _class_methods(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    workflow = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RegistrationEmailWorkflow"
    )
    return {node.name: node for node in workflow.body if isinstance(node, ast.FunctionDef)}


def _module_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_each_stage_body_is_defined_in_its_owning_module():
    for module, function in MOVED_STAGES.values():
        assert function in _module_functions(REGISTRATION / module), f"{module} no longer defines {function}"


def test_handlers_only_delegates_the_moved_stages():
    methods = _class_methods(HANDLERS)
    for method, (module, function) in MOVED_STAGES.items():
        assert method in methods, f"handlers lost the {method} delegate; patch.object on the class would break"
        body = [
            statement
            for statement in methods[method].body
            if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
        ]
        assert len(body) == 1, f"{method} carries more than a delegate; its body belongs in {module}"
        statement = body[0]
        assert isinstance(statement, ast.Return), f"{method} must return the owning module's call"
        call = statement.value
        assert isinstance(call, ast.Call), f"{method} must delegate, not compute"
        alias = "_" + module.removesuffix(".py")
        assert ast.unparse(call.func) == f"{alias}.{function}", (
            f"{method} delegates to {ast.unparse(call.func)} instead of {alias}.{function}"
        )


def test_handlers_did_not_regrow_the_moved_bodies():
    line_count = len(HANDLERS.read_text(encoding="utf-8").splitlines())
    assert line_count <= HANDLERS_LINE_BUDGET, (
        f"handlers.py grew to {line_count} lines (budget {HANDLERS_LINE_BUDGET}); "
        "a new stage body must go into its owner module, with only a delegate left here"
    )


def test_the_fingerprint_helper_keeps_its_historical_import_path():
    from sms_tool.registration import handlers, identity_stages

    assert handlers._apply_protocol_fingerprint is identity_stages._apply_protocol_fingerprint
