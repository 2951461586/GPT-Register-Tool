"""Guard: ``generate_upi_qr_link`` has exactly one implementation and one facade.

Mirrors ``tests/test_payment_link_entrypoint_unique.py`` for the PayPal
entrypoint, applied to UPI.

Background
----------
UPI extraction used to be reachable three ways: the real definition in
``sms_tool/upi_link/`` (package: re-export facade ``__init__.py`` + layered
submodules incl. ``_extract.py``), a
re-export through the ``paypal_link`` package
(`gen_link` imported the whole ``_upi_*`` block, then ``paypal_link/__init__``
put it in ``__all__``), and ``sms_tool.gen_pp_link`` (a
``from .paypal_link import *`` facade). The CLI and the ``native_upi`` pay_link
adapter both imported it from ``gen_pp_link``, so a test patching one path was
invisible to the other -- exactly the failure mode
``test_payment_link_entrypoint_unique.py`` documents for ``generate_payment_link``.

What is asserted
----------------
1. exactly one ``def generate_upi_qr_link`` exists under ``sms_tool/`` (AST);
2. ``sms_tool.upi_link`` resolves to that definition;
3. the ``paypal_link`` facade no longer carries the UPI surface;
4. ``gen_pp_link.generate_upi_qr_link`` is the same object (compat shim);
5. ``UPI_CALL_OPTIONS`` matches the pipeline's own optional keyword params;
6. the CLI and the pay_link adapter both forward the options the old adapter
   whitelist silently dropped (``wait_paid`` / ``paid_timeout`` /
   ``require_server_upi_mandate``).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "sms_tool"
ENTRYPOINT = "generate_upi_qr_link"
REAL_MODULE = "sms_tool/upi_link/pipeline.py"
REAL_PACKAGE_PREFIX = "sms_tool/upi_link/"

# A representative slice of the UPI surface the paypal_link facade must not own.
UPI_NAMES = (
    "generate_upi_qr_link",
    "UPI_CHECKOUT_URL",
    "UPI_BILLING_IN",
    "_upi_extract_qr_from_html",
    "_method_cfg",
)


def _definition_sites() -> dict[str, int]:
    """Map ``relative_path -> lineno`` for every ``def generate_upi_qr_link``."""
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


def test_generate_upi_qr_link_has_exactly_one_definition():
    sites = _definition_sites()
    assert sites, f"no implementation of {ENTRYPOINT} left under sms_tool/"
    assert len(sites) == 1, (
        f"{ENTRYPOINT} is defined {len(sites)} times ({', '.join(sorted(sites))}). "
        "Two same-named implementations make patching in tests silently miss the "
        "other call path -- keep one definition and import it everywhere."
    )
    assert next(iter(sites)).startswith(REAL_PACKAGE_PREFIX), (
        f"{ENTRYPOINT} moved out of {REAL_PACKAGE_PREFIX} (now {next(iter(sites))}). "
        "Update this guard deliberately if that was intentional."
    )


def test_upi_link_is_the_single_owner():
    from sms_tool import upi_link

    fn = getattr(upi_link, ENTRYPOINT, None)
    assert fn is not None, f"sms_tool.upi_link lost {ENTRYPOINT}"
    assert fn.__module__ == "sms_tool.upi_link.pipeline", (
        f"sms_tool.upi_link.{ENTRYPOINT} resolves to {fn.__module__}, expected "
        "sms_tool.upi_link.pipeline (the package re-exports it; the definition "
        "must stay in the pipeline module)"
    )


def test_paypal_link_facade_drops_the_upi_surface():
    from sms_tool import paypal_link

    still_present = [name for name in UPI_NAMES if hasattr(paypal_link, name)]
    assert not still_present, (
        f"sms_tool.paypal_link re-exports UPI names {still_present}. UPI extraction "
        "belongs to sms_tool.upi_link; the paypal_link package owns PayPal only."
    )
    exported = set(getattr(paypal_link, "__all__", []))
    assert not (set(UPI_NAMES) & exported), "paypal_link.__all__ still lists UPI names"


def test_gen_pp_link_keeps_a_single_compat_shim():
    from sms_tool import gen_pp_link, upi_link

    assert gen_pp_link.generate_upi_qr_link is upi_link.generate_upi_qr_link


def test_public_option_set_matches_the_pipeline_signature():
    from sms_tool import upi_link

    params = inspect.signature(upi_link.generate_upi_qr_link).parameters
    optional = {
        name
        for name, param in params.items()
        if name not in {"access_token", "runtime_config"}
        and param.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    }
    assert optional == set(upi_link.UPI_CALL_OPTIONS), (
        "UPI_CALL_OPTIONS drifted from generate_upi_qr_link's optional parameters; "
        "update upi_link.upi_invocation so every entry point sees the same options."
    )


def test_cli_path_forwards_wait_options():
    from sms_tool import cli

    seen: dict = {}

    def fake_generate_upi_qr_link(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "url": "https://pay.openai.com/c/pay/cs_live_UPI"}

    cfg = {"upi": {}, "output": {"directory": "sessions"}}
    argv = [
        "chatgpt_phone_reg.py",
        "--generate-upi-qr",
        "--at",
        "at-test",
        "--wait-paid",
        "--require-server-upi-mandate",
    ]
    with (
        patch.object(cli, "CFG", cfg),
        patch("sys.argv", argv),
        patch("sms_tool.upi_link.generate_upi_qr_link", side_effect=fake_generate_upi_qr_link),
    ):
        cli.main()

    assert seen.get("wait_paid") is True
    assert seen.get("require_server_upi_mandate") is True


def test_pay_link_adapter_forwards_wait_options():
    from sms_tool.payment_contracts import PaymentRequest
    from sms_tool.pay_link.registry import build_default_payment_registry

    registry = build_default_payment_registry()
    request = PaymentRequest.create(
        payment_method="upi",
        access_token="at",
        options={"wait_paid": True, "paid_timeout": 12.5, "require_server_upi_mandate": True},
    )
    with patch("sms_tool.upi_link.generate_upi_qr_link", return_value={"ok": True, "url": "x"}) as mocked:
        registry.execute_mapping(request)

    kwargs = mocked.call_args.kwargs
    assert kwargs["wait_paid"] is True
    assert kwargs["paid_timeout"] == 12.5
    assert kwargs["require_server_upi_mandate"] is True
