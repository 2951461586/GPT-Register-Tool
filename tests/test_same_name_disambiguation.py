"""Rule 19 guard: the three same-name groups stay disambiguated.

On 2026-09-17 an AST audit found 25 definition sites across six same-named
functions and proved **every one** mutually distinct — the names are not
duplicates, they are three different contracts that happened to share a verb.
Rule 19 forbids merging them; this module pins the rename that makes the
difference legible:

``parse_proxy_pool``
    canonical: ``payment_routing`` (payment domain, Mapping-aware, rejects
    invalid provider entries). The ``commands/payment.py`` site is a shim onto
    it, so it keeps the name.
    divergent: ``proxy_routing.parse_lane_proxy_pool`` (registration / account
    -health lanes, lenient, normalises and de-duplicates).

``rotate_proxy_session``
    canonical: ``paypal_proxy`` (thin wrapper over ``proxy_entry.rotate_session``).
    divergent: ``pp_link_helpers.rotate_proxy_session_id`` (1-arg; rotates the
    session id and keeps the country) and
    ``direct_card_extract.rotate_direct_card_proxy_session`` (the ``services/``
    copy, which cannot reach ``sms_tool`` under Rule 10).

``payment_method_label``
    canonical: ``pay_link/registry`` (registry lookup).
    divergent: ``commands/helpers.cli_payment_method_label`` (adds an
    ``or "PayPal"`` fallback) and
    ``blik_qr_extract.current_payment_method_label`` (no-arg; reads the method
    the extractor is currently driving).

The census is a byte pre-filter plus ``ast.parse``: a file that does not
contain the name is never parsed, so the scan stays cheap.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("sms_tool", "services", "scripts")
SKIP_PARTS = {"dist", "runtime", "__pycache__", ".venv", "node_modules"}

# Every ``def <name>`` site the audited tree is allowed to contain, as
# ``relpath -> {name: expected_definition_count}``. A count of 0 pins the
# renamed-away form: re-introducing it is what this guard is here to stop.
EXPECTED_DEFS = {
    "sms_tool/payment_routing.py": {"parse_proxy_pool": 1},
    "sms_tool/commands/payment.py": {"parse_proxy_pool": 1},
    "sms_tool/proxy_routing.py": {"parse_lane_proxy_pool": 1, "parse_proxy_pool": 0},
    "sms_tool/paypal_proxy.py": {"rotate_proxy_session": 1},
    "sms_tool/pp_link_helpers.py": {"rotate_proxy_session_id": 1, "rotate_proxy_session": 0},
    "services/protocol-payment/direct_card/direct_card_extract.py": {
        "rotate_direct_card_proxy_session": 1,
        "rotate_proxy_session": 0,
    },
    "sms_tool/pay_link/registry.py": {"payment_method_label": 1},
    "sms_tool/commands/helpers.py": {"cli_payment_method_label": 1, "payment_method_label": 0},
    "services/protocol-payment/blik/blik_qr_extract.py": {
        "current_payment_method_label": 1,
        "payment_method_label": 0,
    },
}

# Names that are allowed to have *zero* definition sites anywhere else.
ALL_NAMES = sorted({name for names in EXPECTED_DEFS.values() for name in names})

# Re-export wiring: a rename is only finished when the package surface moves
# with it. ``gen_pp_link`` is a ``from .paypal_link import *`` facade, so it is
# the cheapest proof that the ``__all__`` lists were updated too.
EXPECTED_EXPORTS = [
    ("sms_tool.proxy_routing", "parse_lane_proxy_pool"),
    ("sms_tool.payment_routing", "parse_proxy_pool"),
    ("sms_tool.paypal_proxy", "rotate_proxy_session"),
    ("sms_tool.pp_link_helpers", "rotate_proxy_session_id"),
    ("sms_tool.paypal_link", "rotate_proxy_session_id"),
    ("sms_tool.paypal_link", "rotate_stage_proxy_session"),
    ("sms_tool.gen_pp_link", "rotate_proxy_session_id"),
    ("sms_tool.pay_link", "payment_method_label"),
    ("sms_tool.commands.helpers", "cli_payment_method_label"),
]


def _iter_py():
    for directory in SCAN_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if SKIP_PARTS & set(path.parts):
                continue
            yield path


def _def_sites(name: str) -> dict[str, int]:
    """Count ``def <name>`` per file, byte-pre-filtering before any parse."""
    needle = name.encode()
    found: dict[str, int] = {}
    for path in _iter_py():
        raw = path.read_bytes()
        if needle not in raw:
            continue
        tree = ast.parse(raw, filename=str(path))
        hits = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )
        if hits:
            found[path.relative_to(ROOT).as_posix()] = hits
    return found


@pytest.mark.parametrize("relpath,names", sorted(EXPECTED_DEFS.items()))
def test_definition_sites_match_the_rename(relpath, names):
    for name, expected in sorted(names.items()):
        sites = _def_sites(name)
        actual = sites.get(relpath, 0)
        assert actual == expected, (
            "%s: expected %d definition(s) of %s, found %d (whole tree: %s)"
            % (relpath, expected, name, actual, sites)
        )


@pytest.mark.parametrize("name", ALL_NAMES)
def test_no_unlisted_module_defines_the_name(name):
    listed = {rel for rel, names in EXPECTED_DEFS.items() if name in names}
    stray = {rel: n for rel, n in _def_sites(name).items() if rel not in listed}
    assert not stray, "%s is defined in a module the rename did not account for: %s" % (name, stray)


@pytest.mark.parametrize("module,name", EXPECTED_EXPORTS)
def test_the_renamed_symbol_is_reachable(module, name):
    assert hasattr(importlib.import_module(module), name), "%s.%s is missing" % (module, name)


def _body(relpath: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / relpath).read_bytes(), filename=relpath)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            node.body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
            # Erase the name: a different name alone must not pass for a
            # different body. Only signature + body prove divergence.
            node.name = "<fn>"
            return node
    raise AssertionError("%s has no def %s" % (relpath, name))


DIVERGENT_PAIRS = [
    ("sms_tool/payment_routing.py", "parse_proxy_pool", "sms_tool/proxy_routing.py", "parse_lane_proxy_pool"),
    ("sms_tool/paypal_proxy.py", "rotate_proxy_session", "sms_tool/pp_link_helpers.py", "rotate_proxy_session_id"),
    (
        "sms_tool/paypal_proxy.py",
        "rotate_proxy_session",
        "services/protocol-payment/direct_card/direct_card_extract.py",
        "rotate_direct_card_proxy_session",
    ),
    ("sms_tool/pay_link/registry.py", "payment_method_label", "sms_tool/commands/helpers.py", "cli_payment_method_label"),
    (
        "sms_tool/pay_link/registry.py",
        "payment_method_label",
        "services/protocol-payment/blik/blik_qr_extract.py",
        "current_payment_method_label",
    ),
]


@pytest.mark.parametrize("canon_rel,canon,div_rel,divergent", DIVERGENT_PAIRS)
def test_the_divergent_body_is_still_not_the_canonical_body(canon_rel, canon, div_rel, divergent):
    """Convergence would make these equal; Rule 19 forbids that."""
    left = ast.dump(_body(canon_rel, canon))
    right = ast.dump(_body(div_rel, divergent))
    assert left != right, "%s is now identical to %s -- Rule 19 forbids the merge" % (divergent, canon)
