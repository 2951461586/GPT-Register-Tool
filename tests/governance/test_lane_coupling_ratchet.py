"""Tests for scripts/lane_coupling_ratchet.py.

Layers, in increasing order of "does it protect anything":

1. **The resolver, both directions** -- an edge scanner that finds nothing
   passes every "no new coupling" check.  Synthetic trees pin absolute vs
   relative vs package-relative vs function-local imports, and the
   ``_vendor``/``__pycache__`` exclusions.
2. **Each frozen fact can go red** -- a registration->payment import, a payment
   lane borrowing registration *workflow*, a new neutral consumer of payment
   code, a non-``cli`` root module importing an adapter layer, and a wider
   facade each produce their own violation.
3. **The allowed cases stay green** -- the payment lane borrowing Sentinel /
   proxy / auth-header infrastructure, and ``cli`` importing the adapter
   layers, must not be reported.
4. **The ratchet against the real tree** -- zero registration->payment edges,
   the baseline is well formed, and the real tree holds.

The regression this gate exists for: on 2026-10-18 the registration lane
imported ``paypal_proxy`` (for exit-country inference) and ``payment_operation``
(for the durable idempotency journal).  Both modules live at the package root,
so the directory-granularity import-layer ratchet could not see either edge.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lane_coupling_ratchet as lcr  # noqa: E402  # type: ignore


def _package(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a throwaway repo root with an ``sms_tool`` package.

    The resolver only keeps targets that exist as modules (that is what makes
    it a *first-party* edge scanner), so the modules a test imports have to
    exist.  They are seeded empty unless the test supplies a body.
    """
    seeded = {
        "paypal/proxy.py": "",
        "payment/__init__.py": "",
        "payment/contracts.py": "",
        "payment/catalog.py": "",
        "proxy/entry.py": "",
        "phone_proxy.py": "",
        "auth_headers.py": "",
        "registration/__init__.py": "",
        "registration/handlers.py": "",
        "sentinel/__init__.py": "",
        "commands/__init__.py": "",
        "commands/registration.py": "",
    }
    seeded.update(files)
    for name, body in seeded.items():
        path = tmp_path / "sms_tool" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (tmp_path / "sms_tool" / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the ratchet at a synthetic tree and hand back a builder."""

    def build(files: dict[str, str]) -> dict[str, list]:
        root = _package(tmp_path, files)
        monkeypatch.setattr(lcr, "ROOT", root)
        return lcr.analyze()

    return build


# --------------------------------------------------------------------------
# The resolver
# --------------------------------------------------------------------------


def test_absolute_import_is_an_edge(tree):
    current = tree({"registration/handlers.py": "from sms_tool.paypal import proxy\n"})
    assert ["sms_tool.registration.handlers", "sms_tool.paypal.proxy"] in current["registration_to_payment"]


def test_relative_import_is_an_edge(tree):
    current = tree({"registration/handlers.py": "from ..paypal import proxy\n"})
    assert ["sms_tool.registration.handlers", "sms_tool.paypal.proxy"] in current["registration_to_payment"]


def test_package_relative_import_resolves_against_the_package(tree):
    """``from .. import x`` inside ``sms_tool/registration_drivers/flow.py``."""
    current = tree(
        {
            "registration_drivers/__init__.py": "",
            "registration_drivers/flow.py": "from ..paypal import proxy\n",
        }
    )
    assert ["sms_tool.registration_drivers.flow", "sms_tool.paypal.proxy"] in current["registration_to_payment"]


def test_function_local_import_is_still_an_edge(tree):
    """A lazy import is the documented cycle-break, not an exemption."""
    current = tree(
        {
            "registration/handlers.py": (
                "def _geo(proxy):\n"
                "    from ..paypal.proxy import infer_proxy_country\n"
                "    return infer_proxy_country(proxy)\n"
            )
        }
    )
    assert ["sms_tool.registration.handlers", "sms_tool.paypal.proxy"] in current["registration_to_payment"]


def test_vendor_and_pycache_are_excluded(tree):
    current = tree(
        {
            "_vendor/vendored.py": "from ..paypal import proxy\n",
            "registration/handlers.py": "from ..paypal import proxy\n",
        }
    )
    sources = {source for source, _target in current["registration_to_payment"]}
    assert sources == {"sms_tool.registration.handlers"}


# --------------------------------------------------------------------------
# Each frozen fact can go red
# --------------------------------------------------------------------------


def test_registration_to_payment_is_a_violation(tree):
    current = tree({"registration/handlers.py": "from ..paypal import proxy\n"})
    problems = lcr.violations(current, {"registration_to_payment": []})
    assert any("registration lane imports the payment lane" in p for p in problems)


def test_payment_lane_may_not_borrow_registration_workflow(tree):
    current = tree({"pay_link/__init__.py": "", "pay_link/core.py": "from ..registration import handlers\n"})
    problems = lcr.violations(current, {"registration_to_payment": []})
    assert any("registration workflow" in p for p in problems)


def test_payment_lane_may_borrow_shared_infrastructure(tree):
    current = tree(
        {
            "pay_link/__init__.py": "",
            "pay_link/core.py": (
                "from .. import sentinel, phone_proxy\nfrom ..auth_headers import AUTH_FINGERPRINT_PROFILES\n"
            ),
        }
    )
    problems = lcr.violations(current, {"registration_to_payment": []})
    assert problems == []


def test_new_neutral_consumer_of_payment_code_is_a_violation(tree):
    current = tree({"operation_journal.py": "from .payment import contracts\n"})
    problems = lcr.violations(current, {"payment_importers_outside_lane": []})
    assert any("new non-payment consumer of payment code" in p for p in problems)


def test_non_cli_root_module_importing_adapters_is_a_violation(tree):
    # The rule is about *root* modules (``source.count(".") == 1``), so the fixture
    # has to stay at the package root; ``registration_finalize.py`` moved into the
    # registration package and would no longer exercise it.
    current = tree({"batch_runner.py": "from .commands import registration\n"})
    problems = lcr.violations(current, {"root_to_adapter_layers": []})
    assert any("only sms_tool.cli may" in p for p in problems)


def test_cli_importing_adapters_is_allowed(tree):
    current = tree({"cli.py": "from .commands import registration\n"})
    problems = lcr.violations(
        current,
        {
            "root_to_adapter_layers": [
                ["sms_tool.cli", "sms_tool.commands"],
                ["sms_tool.cli", "sms_tool.commands.registration"],
            ]
        },
    )
    assert problems == []


def test_facade_gaining_a_dependency_is_a_violation(tree):
    current = tree({"registration/orchestrator.py": "from ..payment import catalog\n"})
    problems = lcr.violations(current, {"registration_facade_fanout": ["sms_tool.registration.handlers"]})
    assert any("registration facade gained a dependency" in p for p in problems)


# --------------------------------------------------------------------------
# The ratchet against the real tree
# --------------------------------------------------------------------------


def test_baseline_is_present_and_well_formed():
    raw = json.loads(lcr.BASELINE.read_text(encoding="utf-8"))
    assert raw["schema"] == "lane_coupling.v1"
    assert set(raw) >= {
        "registration_to_payment",
        "payment_to_registration",
        "payment_importers_outside_lane",
        "root_to_adapter_layers",
        "registration_facade_fanout",
    }
    assert raw["registration_to_payment"] == [], "the one rule with no allowance must start empty"


def test_real_tree_has_zero_registration_to_payment_edges():
    """The headline fact: the two lanes do not import each other's code."""
    current = lcr.analyze()
    assert current["registration_to_payment"] == []


def test_real_tree_holds_the_baseline():
    problems = lcr.violations(lcr.analyze(), lcr.load_baseline())
    assert problems == [], "lane coupling regressed:\n  " + "\n  ".join(problems)


def test_the_fix_is_pinned_in_both_directions():
    """The two edges this ratchet was written for are gone, and the shells stay.

    ``proxy_entry`` is the definition site; ``paypal_proxy`` keeps a delegating
    shell. ``operation_journal`` is the definition site; ``payment_operation``
    keeps a re-export shell.  If either definition moves back, the first
    assertion fails; if either shell is deleted, importers break loudly.
    """
    found = lcr.edges()
    assert ("sms_tool.registration.handlers", "sms_tool.paypal.proxy") not in found
    assert ("sms_tool.registration.finalize", "sms_tool.paypal.proxy") not in found
    assert ("sms_tool.registration_drivers.browser_flow.orchestrator", "sms_tool.paypal.proxy") not in found
    assert ("sms_tool.commands.registration", "sms_tool.payment.operation") not in found
    assert ("sms_tool.registration.handlers", "sms_tool.proxy.entry") in found
    assert ("sms_tool.commands.registration", "sms_tool.operation_journal") in found


def test_cli_is_the_only_root_module_importing_the_adapter_layers():
    sources = {source for source, _target in lcr.analyze()["root_to_adapter_layers"]}
    assert sources == {lcr.ADAPTER_OWNER}, (
        "the directory ratchet's sms_tool -> commands/cli_parsers edges must all come from cli.py; "
        f"found {sorted(sources)}"
    )
