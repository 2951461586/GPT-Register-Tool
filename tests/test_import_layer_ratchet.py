"""Tests for scripts/import_layer_ratchet.py.

Layers, in increasing order of "does it actually protect anything":

1. **The resolver, both directions** -- an import resolver that returns nothing
   (or everything) manufactures false confidence. The synthetic fixture tree
   pins absolute vs relative vs package-relative vs same-directory vs delayed
   vs wildcard vs prose, and the exact edge count for each.
2. **The package-relative regression** -- reading ``sms_tool/upi_link/__init__.py``
   as belonging to ``sms_tool`` rather than ``sms_tool/upi_link`` invents 12
   phantom ``sms_tool`` edges and inflates every ``sms_tool -> ...`` pair. This
   was a real bug in the ad-hoc analysis that produced the 2026-10-04 numbers,
   so it is pinned here rather than trusted.
3. **The self-test harness can go red** -- gutting the resolver must fail.
4. **The ratchet against the real tree** -- it holds at the frozen baseline, the
   summarised facts are pinned, and ``_vendor`` stays out.

The regression this gate exists for: ``docs/architecture.md`` fixes a layer
model, but no gate measured it, so on 2026-10-04 the review graph reported one
strongly connected component spanning all 14 directories with 470 import
statements and nothing could tell a new back-edge from an old one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import import_layer_ratchet as ilr  # noqa: E402  # type: ignore


@pytest.fixture()
def fixture_tree(tmp_path: Path) -> Path:
    """A throwaway repo root holding the pinned ``sms_tool`` fixture tree."""
    return ilr._build_fixture_tree(tmp_path / "repo")


# --------------------------------------------------------------------------
# The resolver, both directions
# --------------------------------------------------------------------------


def test_absolute_cross_directory_import_becomes_two_edges(fixture_tree: Path):
    counts = ilr.analyze(fixture_tree).pair_counts()
    assert counts["sms_tool -> sms_tool/geo"] == 2


def test_relative_level_two_import_crosses_the_boundary(fixture_tree: Path):
    counts = ilr.analyze(fixture_tree).pair_counts()
    assert counts["sms_tool/accounts -> sms_tool/geo"] == 1
    assert counts["sms_tool/geo -> sms_tool/accounts"] == 1


def test_wildcard_reexport_is_counted(fixture_tree: Path):
    """``from .accounts import *`` in a facade is a real edge, not prose."""
    counts = ilr.analyze(fixture_tree).pair_counts()
    assert counts["sms_tool -> sms_tool/accounts"] == 1


def test_package_relative_self_import_is_not_an_edge(fixture_tree: Path):
    """The 12-phantom-edge regression.

    ``sms_tool/accounts/__init__.py`` does ``from . import helper`` and
    ``from .helper import thing``. Both stay inside ``sms_tool/accounts`` and
    must never be scored as ``sms_tool -> sms_tool/accounts``.
    """
    counts = ilr.analyze(fixture_tree).pair_counts()
    assert counts.get("sms_tool/accounts -> sms_tool/accounts", 0) == 0


def test_same_directory_imports_are_never_edges(fixture_tree: Path):
    counts = ilr.analyze(fixture_tree).pair_counts()
    for source_dir in ("sms_tool", "sms_tool/accounts", "sms_tool/geo"):
        assert counts.get(f"{source_dir} -> {source_dir}", 0) == 0


def test_stdlib_and_prose_are_not_edges(fixture_tree: Path):
    """``import os`` and a comment naming a module must not score."""
    analysis = ilr.analyze(fixture_tree)
    assert not any(e.source_module.endswith("accounts.notes") for e in analysis.edges)


def test_exact_fixture_totals(fixture_tree: Path):
    analysis = ilr.analyze(fixture_tree)
    assert analysis.totals() == {
        "total_module_edges": 6,
        "ordered_pairs": 5,
        "mutual_pairs": 2,
        "minority_edges": 2,
        "delayed_cross_dir_edges": 1,
    }


def test_function_local_import_is_delayed_not_module_scope(fixture_tree: Path):
    """A delayed import is the sanctioned cycle-break, so it is reported but
    explicitly outside the frozen metric."""
    analysis = ilr.analyze(fixture_tree)
    module_pairs = {(e.source_module, e.target_dir) for e in analysis.module_edges}
    assert ("sms_tool.gamma", "sms_tool/accounts") not in module_pairs

    delayed = {(e.source_module, e.target_dir) for e in analysis.delayed_edges}
    assert ("sms_tool.gamma", "sms_tool/accounts") in delayed


def test_vendor_tree_is_excluded(fixture_tree: Path):
    analysis = ilr.analyze(fixture_tree)
    assert not any("_vendor" in e.source_module for e in analysis.edges)


def test_minority_direction_picks_the_weaker_way(fixture_tree: Path):
    """``sms_tool -> sms_tool/geo`` (2) is dominant; ``geo -> sms_tool`` is absent,
    so the scored minority edges are only the genuinely mutual pairs."""
    analysis = ilr.analyze(fixture_tree)
    minority = {(e.source_dir, e.target_dir) for e in analysis.minority_edges()}
    assert minority == {
        ("sms_tool/accounts", "sms_tool"),
        ("sms_tool/geo", "sms_tool/accounts"),
    }


# --------------------------------------------------------------------------
# The self-test harness must be able to go red
# --------------------------------------------------------------------------


def test_self_test_passes_on_the_shipped_fixtures():
    assert ilr.resolver_is_trustworthy() is None


def test_self_test_detects_a_resolver_that_finds_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ilr, "analyze", lambda root=None: ilr.Analysis())
    assert ilr.resolver_is_trustworthy() is not None


def test_self_test_detects_a_resolver_that_invents_edges(monkeypatch: pytest.MonkeyPatch):
    """A resolver that scores every pair would pass a naive total-only check."""
    real = ilr.analyze
    monkeypatch.setattr(ilr, "analyze", lambda root=None: real(root) if root else real())
    monkeypatch.setattr(
        ilr,
        "FIXTURE_EXPECTED",
        ilr.FIXTURE_EXPECTED + (("sms_tool", "sms_tool/never", 5),),
    )
    assert ilr.resolver_is_trustworthy() is not None


def test_cli_fails_loudly_when_the_resolver_is_gutted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ilr, "resolver_is_trustworthy", lambda: "gutted")
    assert ilr.main([]) == 1


# --------------------------------------------------------------------------
# The ratchet against the real tree
# --------------------------------------------------------------------------


def test_baseline_is_present_and_well_formed():
    baseline = ilr.load_baseline()
    assert baseline, "import_layer_baseline.json is missing or unreadable"
    assert isinstance(baseline.get("pairs"), dict)
    assert baseline["total_module_edges"] == sum(baseline["pairs"].values())


def test_ratchet_holds_for_the_current_tree():
    """The frozen per-pair counts must not have grown."""
    analysis = ilr.analyze(ROOT)
    counts = analysis.pair_counts()
    allowed = ilr._baseline_pairs(ilr.load_baseline())
    grown = [
        f"{pair}: {counts[pair]} > {allowed.get(pair, 0)}"
        for pair in sorted(counts)
        if counts[pair] > allowed.get(pair, 0)
    ]
    assert not grown, "module-level cross-directory edges grew:\n  " + "\n  ".join(grown)


def test_summary_facts_are_pinned():
    """Pin the decomposition so a silent metric change is caught.

    470 raw directory-level edges decompose into 287 module-scope edges (frozen
    here) plus 186 delayed imports (frozen by ``delayed_import_ratchet.py``).

    186 (was 185) / 287 (was 288) on 2026-10-08: the PayPal browser lane stopped
    importing the protocol lane. ``paypal/orchestrator.py`` held a module-level
    ``from ..paypal_reverse import try_reverse_pay``; it now takes the adapter as
    the injected ``reverse_pay`` parameter (Boundary Rule 21), and the command
    adapter resolves it inside one named helper. A module-level import there
    would have grown the frozen ``sms_tool/commands -> sms_tool`` pair, so the
    helper is function-local -- the remedy this ratchet's own failure text names.
    Net: one module-scope edge left the graph (288 -> 287) and one delayed
    cross-directory edge joined it (185 -> 186). The ``sms_tool/paypal ->
    sms_tool`` pair drops 9 -> 8.

    185 (was 184) on 2026-10-07: ``proxy_edge_probe.edge_challenge_verdict``
    gained one delayed ``from .accounts.account_terminal import
    text_has_account_deactivated``.  ``account_terminal`` is the single owner of
    the deactivation vocabulary (rule 3 of the P0-B S0 judgement: an
    ``account_deactivated`` 403 is not an exit challenge), and a module-level
    import would have grown the ``sms_tool -> sms_tool/accounts`` pair that the
    ratchet freezes -- the same remedy its own failure text names, and the same
    shape ``payment_auth``/``payment_batch`` already use for that package.

    184 (was 183) on 2026-10-06: ``auth_flow/signup.py`` gained one delayed
    ``from ..operator_output import emit`` for the password-page prime's
    operator-facing warnings -- the import-layer ratchet names a function-local
    import as the sanctioned seam for an ``auth_flow -> sms_tool`` edge, and it
    stays delayed because only that one reporting path needs it. Earlier the
    same day, ``pay_link/adapters.py`` added one for the Checkout-create
    Sentinel pair (183 was 182). Two baselines plus this literal all have to
    move together, which is the point of the pin.
    """
    totals = ilr.analyze(ROOT).totals()
    assert totals == {
        "total_module_edges": 287,
        "ordered_pairs": 31,
        "mutual_pairs": 11,
        "minority_edges": 60,
        "delayed_cross_dir_edges": 186,
    }


def test_vendor_tree_stays_out_of_the_real_scan():
    analysis = ilr.analyze(ROOT)
    assert not any("_vendor" in e.source_module for e in analysis.edges), (
        "vendored code is not ours to re-layer and must stay out of the ratchet"
    )


def test_updating_the_baseline_cannot_silently_raise_a_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``--update-baseline`` is the reviewed lowering path; it must write what it
    measured, so a later run against that file passes."""
    target = tmp_path / "baseline.json"
    monkeypatch.setattr(ilr, "BASELINE", target)
    assert ilr.main(["--update-baseline", "--root", str(ROOT)]) == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["pairs"] == ilr.analyze(ROOT).pair_counts()
    monkeypatch.setattr(ilr, "load_baseline", lambda *a, **k: written)
    counts = ilr.analyze(ROOT).pair_counts()
    assert not [p for p in counts if counts[p] > written["pairs"].get(p, 0)]
