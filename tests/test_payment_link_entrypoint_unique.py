"""Guard: ``generate_payment_link`` must have exactly one real implementation.

Background (round-3 audit, P1-2)
--------------------------------
``sms_tool/pay_link/core.py`` holds the real entrypoint. Until 2026-09-09
``sms_tool/paypal_link/gen_link.py`` *also* defined a same-named function whose
entire body was ``from ..payment_link_manager import generate_payment_link as
managed_generate`` followed by a straight pass-through call. Both were exported
(``pay_link.__init__`` and ``paypal_link.__init__``), so which one you got
depended on which module a call site happened to import from.

Nothing detects that state, which is exactly why it survived: a test that
patches ``sms_tool.payment_link_manager.generate_payment_link`` is completely
invisible to a caller that imported the ``paypal_link`` copy, and vice versa.
Deleting the stub fixes today's tree; this file is what stops a second one from
being added tomorrow.

What is asserted
----------------
1. exactly one ``def generate_payment_link`` exists anywhere under ``sms_tool/``
   (the scan is AST-based, so a re-export by import does not count as a
   definition);
2. the canonical public shell ``sms_tool.payment_link_manager`` resolves to that
   single implementation;
3. the ``paypal_link`` facade no longer carries the name at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "sms_tool"
ENTRYPOINT = "generate_payment_link"
REAL_PACKAGE_PREFIX = "sms_tool/pay_link/"


def _definition_sites() -> dict[str, int]:
    """Map ``relative_path -> lineno`` for every ``def generate_payment_link``."""
    sites: dict[str, int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == ENTRYPOINT:
                sites[path.relative_to(ROOT).as_posix()] = node.lineno
    return sites


def test_generate_payment_link_has_exactly_one_definition():
    sites = _definition_sites()
    assert sites, f"no implementation of {ENTRYPOINT} left under sms_tool/"
    assert len(sites) == 1, (
        f"{ENTRYPOINT} is defined {len(sites)} times ({', '.join(sorted(sites))}). "
        "Two same-named implementations make patching in tests silently miss the "
        "other call path -- keep one definition and import it everywhere."
    )
    only_path = next(iter(sites))
    assert only_path.startswith(REAL_PACKAGE_PREFIX), (
        f"{ENTRYPOINT} moved out of {REAL_PACKAGE_PREFIX} (now {only_path}). "
        "Update this guard deliberately if that was intentional."
    )


def test_manager_shell_points_at_the_single_implementation():
    from sms_tool import payment_link_manager

    fn = getattr(payment_link_manager, ENTRYPOINT, None)
    assert fn is not None, f"sms_tool.payment_link_manager lost {ENTRYPOINT}"
    assert fn.__module__ == "sms_tool.pay_link.core", (
        f"sms_tool.payment_link_manager.{ENTRYPOINT} resolves to "
        f"{fn.__module__}, expected sms_tool.pay_link.core"
    )


def test_paypal_link_facade_does_not_reexport_it():
    from sms_tool import paypal_link

    assert not hasattr(paypal_link, ENTRYPOINT), (
        f"sms_tool.paypal_link re-exports {ENTRYPOINT} again. The paypal_link "
        "package owns paypal-specific helpers; the unified entrypoint lives in "
        "pay_link and is re-exported by payment_link_manager."
    )
    assert ENTRYPOINT not in getattr(paypal_link, "__all__", [])
