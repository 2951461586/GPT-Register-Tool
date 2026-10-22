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

ROOT = Path(__file__).resolve().parents[2]
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
    """The fixture's own totals, NOT the production ones.

    ``scripts/module_move.py``'s ``sync_pinned_totals`` used to rewrite every
    ``"total_module_edges": N`` in this file, which silently replaced this
    fixture pin with the production numbers and left the test comparing a
    seven-module fixture against the real tree.
    """
    analysis = ilr.analyze(fixture_tree)
    assert analysis.totals() == {
        "total_module_edges": 6,
        "ordered_pairs": 5,
        "mutual_pairs": 2,
        "minority_edges": 2,
        "delayed_cross_dir_edges": 1,
        "type_only_edges": 0,
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
    # Bind first, narrow the binding: ``isinstance(baseline.get("pairs"), dict)``
    # proves nothing about a *separate* ``baseline["pairs"]`` subscript, so
    # pyright still saw ``object`` at ``.values()``.
    pairs = baseline.get("pairs")
    assert isinstance(pairs, dict)
    assert baseline["total_module_edges"] == sum(pairs.values())


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

    470 raw directory-level edges decompose into 264 module-scope edges (frozen
    here) plus 190 delayed cross-directory imports (frozen by
    ``delayed_import_ratchet.py``).

    276 (was 277) / 54 minority (was 56) on 2026-10-17: the pure auth wire
    helpers (``_is_existing_login_redirect``, ``_with_query_param``,
    ``_invalid_state_auth_response``, ``_auth_request_headers`` and the OTP
    subject constant) moved out of ``auth_flow/steps.py`` into the top-level
    contract module ``sms_tool/auth_wire.py``. ``otp_strategy`` and
    ``phone_registration`` now import them (and ``http_utils``'s
    ``_absolute_url`` / ``_json_or_raw``) directly instead of through the
    ``auth_flow`` package facade, which drops the
    ``sms_tool -> sms_tool/auth_flow`` pair 4 -> 2. The pair
    ``sms_tool/auth_flow -> sms_tool`` grows 10 -> 11 because ``steps`` now
    imports ``auth_wire``; that is the sanctioned remedy for a
    minority-direction edge (move the shared helper into a leaf both sides may
    import), and the totals still fall.

    277 (was 287) / 29 pairs (was 31) / 56 minority (was 60) / 188 delayed (was
    187) on 2026-10-17: ``accounts/account_creation.py`` and
    ``accounts/account_seed.py`` moved to the package root. Both had every
    internal dependency at the top level already (``http_utils``,
    ``auth_headers``, ``codex_sentinel``, ``config``, ``http_client``,
    ``storage``) and ``accounts/`` consumed only two ``account_creation``
    symbols through a delayed import, so the move turned four
    ``sms_tool -> sms_tool/accounts`` edges into same-directory imports. The
    two ordered pairs that disappeared (31 -> 29) are exactly the pairs those
    edges left behind; the delayed count grew by one because
    ``account_recovery`` keeps its function-local import of the moved module.

    187 (was 186) on 2026-10-11: ``codex_oauth._password_login_and_exchange``
    completes a saved TOTP challenge through ``auth_flow.totp`` (the single owner
    of that exchange) via one function-local import. A module-level import grew
    the frozen ``sms_tool -> sms_tool/auth_flow`` pair 4 -> 5, so it stays
    delayed -- the remedy this ratchet's own failure text names.

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

    264 (was 267) / 42 minority (was 45) / 190 delayed (was 188) on 2026-10-21:
    provider adapters stopped importing the two application concerns they were
    only *receiving*. ``providers/mailbox_gmail`` and ``providers/mailbox_graph``
    imported ``mailbox_pool_writer.apply_rotation``, and
    ``providers/mailbox_remail`` imported ``desktop_ipc.progress_dots_enabled``;
    both now arrive through ``sms_tool/providers/ports.py``, which imports them
    lazily -- the remedy this ratchet's failure text names for a documented DI
    seam. Three module-scope edges leave (267 -> 264, minority 45 -> 42) and two
    delayed cross-directory edges join (188 -> 190); ``delayed_import_ratchet``
    records 415 -> 417, each with a written reason in
    ``delayed_import_reasons.json``. Pairs and mutual pairs are unchanged: the
    seam stays inside the same ``sms_tool/providers -> sms_tool`` pair.

    190 (was 188) / 267 (was 276) / 28 pairs (was 29) / 10 mutual (was 11) / 45
    minority (was 54) on 2026-10-18: the CLI entrypoint stopped importing every
    command adapter at module scope. Seven adapters plus ``batch_runner``,
    ``storage`` and the helper hooks are now resolved by ``_LazyModule`` proxies
    and per-symbol resolvers, so nine ``sms_tool -> sms_tool/commands`` edges
    leave the graph and that pair disappears (hence 28 pairs, 10 mutual, 45
    minority). Two cross-directory dependencies become function-local seams
    (``commands.email_change`` and ``commands.helpers``), which is the +2 here;
    ``delayed_import_ratchet`` records the wider 414 -> 421 because seven eager
    adapter imports are now local. That is the trade Rule 15 prescribes: a 30 ms
    help screen must not pay for the payment/browser stack, and the import-surface
    test pins the result.
    """
    totals = ilr.analyze(ROOT).totals()
    # 2026-10-23: 437 -> 453. Wiring every caller to its host authority
    # (`sms_tool/endpoints.py`) added 16 module-level edges, all into that one
    # pure-constants leaf module. The alternative -- making each call site a
    # function-local import -- moved 41 statements into
    # `delayed_import_ratchet`, which hides the coupling instead of recording
    # it. This is the reviewed increase: a leaf of string constants is the
    # cheapest edge in the tree, and `endpoints_literal_ratchet` requires it.
    assert totals == {
        "total_module_edges": 453,
        "ordered_pairs": 66,
        "mutual_pairs": 18,
        "minority_edges": 49,
        # 2026-10-21: 188 -> 190 when the provider ports seam (providers/ports.py)
        # took over the mailbox_pool_writer and desktop_ipc imports as delayed
        # ones. Before that, 190 -> 188 when store/connection.py stopped
        # importing the sms_tool.storage shell inside _init_cache_key/_connect.
        "delayed_cross_dir_edges": 253,
        # Imports under `if TYPE_CHECKING:` never run, so they are not runtime
        # edges. All eleven in the tree today are same-directory, hence 0 here;
        # the metric exists so a cross-directory one is visible rather than silent.
        "type_only_edges": 0,
    }


def test_a_type_checking_import_is_not_a_runtime_edge(tmp_path: Path):
    """The registration stage cluster looked like a 9-module cycle until these
    were classified: every stage names the workflow for annotations only."""

    nl = chr(10)
    for rel, text in {
        "__init__.py": "",
        "handlers.py": "from .stages import run" + nl,
        "stages.py": (
            "from typing import TYPE_CHECKING"
            + nl
            + "if TYPE_CHECKING:"
            + nl
            + "    from .handlers import Workflow"
            + nl
        ),
    }.items():
        path = tmp_path / "sms_tool" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline=nl)

    analysis = ilr.analyze(tmp_path)
    assert [(e.source_module, e.target_module) for e in analysis.all_module_edges()] == [
        ("sms_tool.handlers", "sms_tool.stages")
    ]
    # The annotation-only edge must not appear in any runtime bucket: only the
    # real handlers -> stages edge is a same-directory runtime edge.
    assert analysis.type_edges == []
    assert [(e.source_module, e.target_module) for e in analysis.same_directory] == [
        ("sms_tool.handlers", "sms_tool.stages")
    ]


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


# --------------------------------------------------------------------------
# Layer direction: the reviewed module map in scripts/layer_manifest.json.
# --------------------------------------------------------------------------

LAYER_MANIFEST = {
    "packages": {"adapters": 5, "flows": 3},
    "module_overrides": {},
    "root_modules": {"leaf.py": 5, "workflow.py": 3, "cli.py": 2},
    "allow": {},
}


def layers_from(**overrides) -> ilr.Layers:
    payload = dict(LAYER_MANIFEST)
    payload.update(overrides)
    return ilr.load_layers_payload(payload)


def analysis_of(tmp_path: Path, files: dict[str, str]) -> tuple[ilr.Analysis, list[str]]:
    for rel, text in files.items():
        path = tmp_path / "sms_tool" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _files, modules, _is_package = ilr._index(tmp_path)
    return ilr.analyze(tmp_path), list(modules)


def test_an_inner_module_importing_an_outer_one_is_a_violation(tmp_path):
    analysis, modules = analysis_of(
        tmp_path,
        {
            "__init__.py": "",
            "leaf.py": "from .workflow import thing\n",
            "workflow.py": "thing = 1\n",
        },
    )
    unclassified, allowed, unallowed = ilr.direction_violations(analysis, layers_from(), modules)
    assert unclassified == []
    assert allowed == []
    assert [edge.key for edge in unallowed] == ["sms_tool.leaf -> sms_tool.workflow"]


def test_an_outer_module_importing_an_inner_one_is_legal(tmp_path):
    analysis, modules = analysis_of(
        tmp_path,
        {
            "__init__.py": "",
            "leaf.py": "thing = 1\n",
            "cli.py": "from .leaf import thing\n",
        },
    )
    _unclassified, _allowed, unallowed = ilr.direction_violations(analysis, layers_from(), modules)
    assert unallowed == []


def test_same_layer_edges_are_legal(tmp_path):
    analysis, modules = analysis_of(
        tmp_path,
        {
            "__init__.py": "",
            "adapters/__init__.py": "",
            "adapters/a.py": "from .b import thing\n",
            "adapters/b.py": "thing = 1\n",
        },
    )
    _unclassified, _allowed, unallowed = ilr.direction_violations(analysis, layers_from(), modules)
    assert unallowed == []


def test_an_allow_entry_absorbs_a_documented_inward_edge(tmp_path):
    analysis, modules = analysis_of(
        tmp_path,
        {
            "__init__.py": "",
            "leaf.py": "from .workflow import thing\n",
            "workflow.py": "thing = 1\n",
        },
    )
    layers = layers_from(
        allow={
            "sms_tool.leaf -> sms_tool.workflow": {
                "reason": "the leaf is the documented facade for this workflow",
                "owner": "sms_tool/leaf.py",
            }
        }
    )
    _unclassified, allowed, unallowed = ilr.direction_violations(analysis, layers, modules)
    assert unallowed == []
    assert [edge.key for edge in allowed] == ["sms_tool.leaf -> sms_tool.workflow"]


def test_a_module_no_rule_claims_is_reported(tmp_path):
    analysis, modules = analysis_of(
        tmp_path,
        {
            "__init__.py": "",
            "unclaimed.py": "thing = 1\n",
        },
    )
    unclassified, _allowed, _unallowed = ilr.direction_violations(analysis, layers_from(), modules)
    assert unclassified == ["sms_tool.unclaimed"]


def test_a_same_directory_inward_edge_is_still_checked(tmp_path):
    """A package __init__ importing its own higher-layer submodule never crosses a
    directory boundary, so the per-pair ratchet cannot see it -- the layer check must."""

    analysis, modules = analysis_of(
        tmp_path,
        {
            "__init__.py": "",
            "adapters/__init__.py": "from .high import thing\n",
            "adapters/high.py": "thing = 1\n",
        },
    )
    layers = layers_from(module_overrides={"adapters/high": 3})
    _unclassified, _allowed, unallowed = ilr.direction_violations(analysis, layers, modules)
    assert [edge.key for edge in unallowed] == ["sms_tool.adapters -> sms_tool.adapters.high"]


def test_a_layer_outside_one_to_five_is_rejected():
    with pytest.raises(ValueError, match="layer 1..5"):
        layers_from(root_modules={"leaf.py": 9})


def test_an_allow_entry_needs_a_written_reason():
    with pytest.raises(ValueError, match="at least 5 words"):
        layers_from(allow={"sms_tool.a -> sms_tool.b": {"reason": "because"}})


def test_the_committed_manifest_claims_every_module():
    layers = ilr.load_layers()
    _files, modules, _is_package = ilr._index(ROOT)
    assert [m for m in modules if m != "sms_tool" and ilr.layer_of(m, layers) is None] == []


def test_the_committed_tree_has_no_unallowed_inward_edge():
    layers = ilr.load_layers()
    _files, modules, _is_package = ilr._index(ROOT)
    _unclassified, _allowed, unallowed = ilr.direction_violations(ilr.analyze(ROOT), layers, list(modules))
    assert unallowed == []
