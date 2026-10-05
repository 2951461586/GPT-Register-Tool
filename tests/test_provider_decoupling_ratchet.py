"""Tests for scripts/provider_decoupling_ratchet.py.

Layers, in increasing order of "does it actually protect anything":

1. **The classifier, both directions** -- a bucket assignment that puts
   everything in one pile passes a total-only check while protecting nothing.
   The synthetic fixture tree pins shim vs verbatim-copy vs fork vs
   re-signatured vs unrelated, and asserts the exact triple per file.
2. **The re-signatured regression** -- during development the fingerprint
   compared *bodies only*, so ``def shared_add(a, b, c=0): return a + b`` scored
   as ``identical`` to ``def shared_add(a, b): return a + b``. Converging that
   would change every caller's argument binding -- the precise failure
   ``docs/architecture.md`` Rule 19 forbids -- so it is pinned here.
3. **The self-test harness can go red** -- gutting the classifier must fail.
4. **The ratchet against the real tree** -- it holds, the summary is pinned, and
   the one *documented* divergence (blik's ``build_email``) is asserted to stay
   divergent rather than silently converge.

The regression this gate exists for: the ``common/`` migration is ~76% done and
was unmeasured, so a new private fork of an already-shared helper looked like a
new helper.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import provider_decoupling_ratchet as pdr  # noqa: E402  # type: ignore


@pytest.fixture()
def fixture_tree(tmp_path: Path) -> Path:
    return pdr._build_fixture_tree(tmp_path / "repo")


def _counts(analysis: pdr.Analysis, provider_file: str) -> dict[str, int]:
    return analysis.counts_for(provider_file)


# --------------------------------------------------------------------------
# The classifier, both directions
# --------------------------------------------------------------------------


def test_thin_shim_is_delegating_even_with_a_docstring(fixture_tree: Path):
    analysis = pdr.analyze(fixture_tree)
    assert _counts(analysis, "alpha/delegating.py") == {
        pdr.DELEGATING: 2,
        pdr.IDENTICAL: 0,
        pdr.DIVERGENT: 0,
    }


def test_verbatim_copy_is_identical(fixture_tree: Path):
    analysis = pdr.analyze(fixture_tree)
    assert _counts(analysis, "alpha/copied.py")[pdr.IDENTICAL] == 1


def test_independent_logic_is_divergent(fixture_tree: Path):
    analysis = pdr.analyze(fixture_tree)
    assert _counts(analysis, "alpha/forked.py") == {
        pdr.DELEGATING: 0,
        pdr.IDENTICAL: 0,
        pdr.DIVERGENT: 2,
    }


def test_resignatured_body_is_divergent_not_identical(fixture_tree: Path):
    """The pinned regression: same body, different signature => must not merge."""
    analysis = pdr.analyze(fixture_tree)
    assert _counts(analysis, "alpha/resignatured.py") == {
        pdr.DELEGATING: 0,
        pdr.IDENTICAL: 0,
        pdr.DIVERGENT: 1,
    }


def test_single_non_call_return_is_not_a_shim(fixture_tree: Path):
    analysis = pdr.analyze(fixture_tree)
    assert _counts(analysis, "alpha/lying.py")[pdr.DIVERGENT] == 1


def test_names_absent_from_common_contribute_nothing(fixture_tree: Path):
    analysis = pdr.analyze(fixture_tree)
    assert _counts(analysis, "alpha/unrelated.py") == {
        pdr.DELEGATING: 0,
        pdr.IDENTICAL: 0,
        pdr.DIVERGENT: 0,
    }


def test_logs_directory_is_excluded(fixture_tree: Path):
    analysis = pdr.analyze(fixture_tree)
    assert not any(p.provider_file.startswith("alpha/logs/") for p in analysis.pairs)


def test_exact_fixture_summary(fixture_tree: Path):
    analysis = pdr.analyze(fixture_tree)
    assert analysis.summary() == {
        "same_named_total": 7,
        pdr.DELEGATING: 2,
        pdr.IDENTICAL: 1,
        pdr.DIVERGENT: 4,
        "files_with_frozen_pairs": 4,
    }


# --------------------------------------------------------------------------
# The self-test harness must be able to go red
# --------------------------------------------------------------------------


def test_self_test_passes_on_the_shipped_fixtures():
    assert pdr.classifier_is_trustworthy() is None


def test_self_test_detects_a_classifier_that_finds_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdr, "analyze", lambda root=None: pdr.Analysis())
    assert pdr.classifier_is_trustworthy() is not None


def test_self_test_detects_a_single_bucket_classifier(monkeypatch: pytest.MonkeyPatch):
    """Dumping everything into one bucket must fail the fixture check."""
    real = pdr.analyze

    def flatten(root=None):
        analysis = real(root)
        for pair in analysis.pairs:
            pair.kind = pdr.DIVERGENT
        return analysis

    monkeypatch.setattr(pdr, "analyze", flatten)
    assert pdr.classifier_is_trustworthy() is not None


def test_cli_fails_loudly_when_the_classifier_is_gutted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdr, "classifier_is_trustworthy", lambda: "gutted")
    assert pdr.main([]) == 1


# --------------------------------------------------------------------------
# The ratchet against the real tree
# --------------------------------------------------------------------------


def test_baseline_is_present_and_well_formed():
    baseline = pdr.load_baseline()
    assert baseline, "provider_decoupling_baseline.json is missing or unreadable"
    assert isinstance(baseline.get("files"), dict)
    files = pdr._baseline_files(baseline)
    for kind in pdr.FROZEN:
        assert sum(b[kind] for b in files.values()) == baseline[kind]


def test_ratchet_holds_for_the_current_tree():
    analysis = pdr.analyze(ROOT)
    allowed = pdr._baseline_files(pdr.load_baseline())
    grown: list[str] = []
    for provider_file in sorted(set(analysis.provider_files) | set(allowed)):
        counts = analysis.counts_for(provider_file)
        for kind in pdr.FROZEN:
            limit = allowed.get(provider_file, {}).get(kind, 0)
            if counts[kind] > limit:
                grown.append(f"{provider_file}: {kind} {counts[kind]} > {limit}")
    assert not grown, "provider duplication grew:\n  " + "\n  ".join(grown)


def test_summary_facts_are_pinned():
    """238 same-named defs: 182 already migrated, 8 verbatim copies, 48 forks."""
    assert pdr.analyze(ROOT).summary() == {
        "same_named_total": 238,
        pdr.DELEGATING: 182,
        pdr.IDENTICAL: 8,
        pdr.DIVERGENT: 48,
        "files_with_frozen_pairs": 7,
    }


def test_the_migration_is_mostly_done_for_ideal_and_twint():
    """Pin the two already-migrated extractors so an inlining regression fails."""
    analysis = pdr.analyze(ROOT)
    for provider_file in (
        "ideal/ideal_qr_extract.py",
        "twint/twint_extract.py",
    ):
        counts = analysis.counts_for(provider_file)
        assert counts[pdr.DELEGATING] == 67
        assert counts[pdr.IDENTICAL] == 0
        assert counts[pdr.DIVERGENT] == 2


def test_blik_is_the_remaining_bulk_and_is_split_as_measured():
    analysis = pdr.analyze(ROOT)
    counts = analysis.counts_for("blik/blik_qr_extract.py")
    assert counts == {pdr.DELEGATING: 39, pdr.IDENTICAL: 8, pdr.DIVERGENT: 32}
    assert counts[pdr.IDENTICAL] + counts[pdr.DIVERGENT] == 40


def test_blik_build_email_stays_divergent():
    """The documented Rule-19 case.

    ``blik``'s ``build_email(first, last)`` reads module-level ``EMAIL_DOMAINS``;
    ``common``'s takes a ``ProviderProfile``. Same name, different contract --
    converging it changes who supplies the email domains, so it must stay
    divergent until someone records that decision.
    """
    analysis = pdr.analyze(ROOT)
    kinds = {p.kind for p in analysis.pairs if p.provider_file == "blik/blik_qr_extract.py" and p.name == "build_email"}
    assert kinds == {pdr.DIVERGENT}


def test_pix_has_no_same_named_definitions():
    """``pix`` was never forked from the same upstream, so it shares no names.

    Recorded explicitly so "0 divergent" is not mistaken for "fully migrated".
    """
    analysis = pdr.analyze(ROOT)
    for provider_file in ("pix/pix_core.py", "pix/pix_extract.py", "pix/run_pix.py"):
        assert analysis.counts_for(provider_file) == {
            pdr.DELEGATING: 0,
            pdr.IDENTICAL: 0,
            pdr.DIVERGENT: 0,
        }


def test_updating_the_baseline_writes_what_it_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "baseline.json"
    monkeypatch.setattr(pdr, "BASELINE", target)
    assert pdr.main(["--update-baseline", "--root", str(ROOT)]) == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    measured = {f: pdr.analyze(ROOT).counts_for(f) for f in sorted(set(pdr.analyze(ROOT).provider_files))}
    assert written["files"] == measured
