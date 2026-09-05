"""Guard: tests must patch the config seam the production code actually reads.

``sms_tool/pay_link/*.py`` each bind ``current_config_data`` with
``from ..config import current_config_data``, so every one of those modules
owns a *separate* module-level name.  Two patch targets therefore look right
and are silently inert:

* ``patch("sms_tool.config.current_config_data", ...)`` -- rebinds the name in
  ``sms_tool.config`` only; ``pay_link.base`` keeps its own reference.
* ``patch.object(manager, "current_config_data", ...)`` where ``manager`` is
  the ``payment_link_manager`` back-compat shell -- nothing in ``pay_link``
  reads the shell's namespace.

Either one leaves the real merged config in play, which means the real paid
proxy pool, which means the route planner issues real geo probes and the test
passes or fails with the weather.  That is exactly how
``test_subprocess_timeout_has_distinct_retryable_terminal_contract`` was
flaky for a full day before it was traced.

The seam that works is ``sms_tool.pay_link.base.current_config_data`` (the one
``_config_data()`` resolves).  See ``tests/test_payment_result_contract.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent

INERT_TARGETS = {
    "sms_tool.config.current_config_data",
    "sms_tool.payment_link_manager.current_config_data",
}
SHELL_MODULES = {
    "sms_tool.payment_link_manager",
    "payment_link_manager",
}
CONFIG_ATTRS = {"current_config_data"}


def _iter_test_sources() -> list[Path]:
    return sorted(TESTS_ROOT.rglob("*.py"))


def _shell_aliases(tree: ast.Module) -> set[str]:
    """Names bound to the payment_link_manager shell in this module."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "sms_tool":
            for alias in node.names:
                if alias.name == "payment_link_manager":
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sms_tool.payment_link_manager":
                    aliases.add(alias.asname or alias.name.split(".")[-1])
    return aliases


def _first_arg_string(call: ast.Call) -> str:
    if not call.args:
        return ""
    first = call.args[0]
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else ""


def _patch_object_module(call: ast.Call) -> str:
    """Return the alias name used as patch.object()'s first argument."""
    if not call.args:
        return ""
    first = call.args[0]
    return first.id if isinstance(first, ast.Name) else ""


def test_no_test_patches_an_inert_config_seam():
    offenders: list[str] = []

    for path in _iter_test_sources():
        # utf-8-sig: some files in tests/ carry a BOM (test_mail_otp_web.py).
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        aliases = _shell_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                # `patch.object(...)` parses as Attribute(attr="object") on Name("patch");
                # reconstruct the dotted form so the whitelist below matches.
                owner = func.value.id if isinstance(func.value, ast.Name) else ""
                name = f"{owner}.{func.attr}" if owner else func.attr
            if name not in {"patch", "patch.object"}:
                continue
            if name == "patch":
                target = _first_arg_string(node)
                if target in INERT_TARGETS:
                    offenders.append(f"{path.relative_to(TESTS_ROOT)}:{node.lineno} -> {target}")
            elif name == "patch.object" and _patch_object_module(node) in aliases:
                # patch.object(<shell>, "<attr>", ...) -- only config attrs matter.
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    if node.args[1].value in CONFIG_ATTRS:
                        offenders.append(
                            f"{path.relative_to(TESTS_ROOT)}:{node.lineno} -> "
                            f"patch.object(shell, {node.args[1].value!r})"
                        )

    assert not offenders, (
        "these patch targets do not reach the code under test; the pay_link "
        "subpackage binds its own current_config_data: \n  "
        + "\n  ".join(offenders)
    )


def test_pay_link_base_is_the_documented_config_seam():
    """Sanity-check the guard itself: base must expose the seam it names."""
    from sms_tool.pay_link import base

    assert callable(base.current_config_data)
    assert base._config_data.__module__ == "sms_tool.pay_link.base"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
