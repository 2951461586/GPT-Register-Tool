"""Tests for scripts/extractor_parity_report.py.

Three layers, in increasing order of "does it actually protect anything":

1. **The predicate, both directions** -- a parity judge that only ever says
   "identical" is worse than none, because it manufactures merge candidates.
   The synthetic cases pin both outcomes.
2. **The self-test harness** -- the fixtures must be able to go red, otherwise
   `predicates_are_trustworthy` is decoration.
3. **The ratchet against the real tree**, plus the negative control that keeps
   the headline number honest.

The regression this gate exists for: `ideal_qr_extract.py` and
`twint_extract.py` share 108 of 134 top-level functions (81%) verbatim once
provider naming is normalised, and nothing counted that before 2026-09-17.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import extractor_parity_report as parity  # noqa: E402


def _cmp(a: str, b: str) -> dict[str, list[str]]:
    return parity.compare_sources(textwrap.dedent(a), textwrap.dedent(b))


# --------------------------------------------------------------------------
# The predicate, both directions
# --------------------------------------------------------------------------


def test_provider_suffixed_siblings_pair_up():
    """The case a naive exact-name join gets wrong.

    ``stripe_create_ideal_pm`` / ``stripe_create_twint_pm`` are the same
    function with the provider substituted. Joining on the raw name buckets both
    as file-specific and hides them from the duplicate count entirely.
    """
    r = _cmp(
        "def stripe_create_ideal_pm(x):\n    return x + 1\n",
        "def stripe_create_twint_pm(x):\n    return x + 1\n",
    )
    assert r["identical"] == ["stripe_create_PROVIDER_pm"]
    assert r["only_a"] == [] and r["only_b"] == []


def test_sibling_pair_with_different_body_is_not_identical():
    r = _cmp(
        "def f_ideal(x):\n    return x + 1\n",
        "def f_twint(x):\n    return x - 1\n",
    )
    assert r["identical"] == []
    assert r["different"] == ["f_PROVIDER"]


def test_comparison_operator_difference_is_not_normalised_away():
    """The failure mode that would make this tool dangerous.

    ``>`` vs ``>=`` is a real behavioural difference. If the normaliser erased
    it, the report would recommend merging two functions that behave
    differently under edge inputs.
    """
    r = _cmp(
        "def f_ideal(x):\n    return x > 1\n",
        "def f_twint(x):\n    return x >= 1\n",
    )
    assert r["identical"] == []
    assert r["different"] == ["f_PROVIDER"]


def test_statement_count_difference_is_not_normalised_away():
    r = _cmp(
        "def f(x):\n    return x\n",
        "def f(x):\n    return x\n    return x\n",
    )
    assert r["identical"] == []


def test_provider_token_in_string_constant_normalises():
    """URLs and log messages carry the provider name; they must not show up as
    spurious differences."""
    r = _cmp(
        'def f_ideal():\n    return "https://ideal.example/x"\n',
        'def f_twint():\n    return "https://twint.example/x"\n',
    )
    assert r["identical"] == ["f_PROVIDER"]


def test_string_constant_is_substituted_not_blanked():
    """Only the token is replaced, never the whole value.

    Blanking the constant would make ``.../a`` and ``.../b`` compare equal --
    a false "identical" that inflates the duplicate count.
    """
    r = _cmp(
        'def f_ideal():\n    return "https://ideal.example/a"\n',
        'def f_twint():\n    return "https://twint.example/b"\n',
    )
    assert r["identical"] == []
    assert r["different"] == ["f_PROVIDER"]


def test_unrelated_names_do_not_pair():
    r = _cmp(
        "def alpha(x):\n    return x + 1\n",
        "def beta(x):\n    return x + 1\n",
    )
    assert r["identical"] == []
    assert r["only_a"] == ["alpha"] and r["only_b"] == ["beta"]


def test_whitespace_and_comments_are_not_differences():
    r = _cmp(
        "def f_ideal(x):\n    return x + 1\n",
        "def f_twint(x):\n    # a comment\n    return x + 1\n",
    )
    assert r["identical"] == ["f_PROVIDER"]


def test_two_functions_colliding_after_normalisation_both_survive():
    """``f_ideal`` and ``f_twint`` in ONE file both normalise to ``f_PROVIDER``.

    A dict keyed on the normalised name would silently drop one, understating
    the file's function count.
    """
    funcs = parity.top_level_functions(
        "def f_ideal(x):\n    return 1\n"
        "def f_twint(x):\n    return 2\n"
    )
    assert len(funcs) == 2, f"a sibling was overwritten: {sorted(funcs)}"


def test_async_functions_are_counted():
    r = _cmp(
        "async def f_ideal(x):\n    return x + 1\n",
        "async def f_twint(x):\n    return x + 1\n",
    )
    assert r["identical"] == ["f_PROVIDER"]


def test_sync_and_async_are_not_conflated():
    """An ``async`` change is a real API change, not a rename."""
    r = _cmp(
        "def f(x):\n    return 1\n",
        "async def f(x):\n    return 1\n",
    )
    assert r["identical"] == []


# --------------------------------------------------------------------------
# The self-test harness must be able to fail
# --------------------------------------------------------------------------


def test_self_test_passes_on_the_shipped_fixtures():
    assert parity.predicates_are_trustworthy() is None


def test_self_test_detects_a_normaliser_that_erases_everything(monkeypatch):
    """A predicate that calls everything identical must be caught.

    Without this, a broken normaliser would report the whole cluster as one
    giant merge candidate and the ratchet would silently demand a huge refactor.
    """
    monkeypatch.setattr(parity, "_normalise", lambda node, **kw: "SAME")
    assert parity.predicates_are_trustworthy() is not None


def test_self_test_detects_a_normaliser_that_erases_nothing(monkeypatch):
    """The opposite failure: never normalising makes every sibling look unique."""
    monkeypatch.setattr(
        parity, "_normalise", lambda node, **kw: __import__("ast").dump(node)
    )
    assert parity.predicates_are_trustworthy() is not None


def test_void_delegating_stub_is_excluded():
    """A void function delegates with a bare call, not `return`.

    `dump_http` returns None, so its stub is ``shared_dump_http(...)`` as an
    expression statement.  The predicate originally only matched ``return``
    forms, so batch 4's two new stubs were counted as a duplicate pair and
    exactly cancelled the real duplicate they replaced -- the ratchet reported
    +0 on a change that deleted ~35 lines from each of two extractors.
    """
    stub = '''
        def dump_http(response, stage):
            """Delegate to common/http_dump.py."""
            shared_dump_http(response, stage, dump_env="IDEAL_DUMP")
    '''
    assert parity.is_delegating_stub(_parse_one(stub)) is True


def test_returning_delegating_stub_is_excluded():
    stub = '''
        def proxy_key(proxy):
            return shared_proxy_key(proxy)
    '''
    assert parity.is_delegating_stub(_parse_one(stub)) is True


def test_a_call_to_a_shared_helper_among_real_logic_is_still_counted():
    """Narrowness check: the predicate must not swallow functions that merely
    happen to call a shared helper -- those still hold logic worth measuring."""
    not_a_stub = '''
        def load_proxy_seeds(path):
            shared_load_proxy_file(path)
            return sorted(result)
    '''
    assert parity.is_delegating_stub(_parse_one(not_a_stub)) is False


def test_a_bare_call_to_a_non_shared_name_is_not_a_stub():
    not_a_stub = '''
        def refresh():
            do_work()
    '''
    assert parity.is_delegating_stub(_parse_one(not_a_stub)) is False


def _parse_one(source: str):
    import ast
    import textwrap as _tw
    tree = ast.parse(_tw.dedent(source))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef))


def test_cli_fails_loudly_when_the_predicate_is_gutted():
    """End-to-end: a gutted normaliser must make the CLI exit non-zero with a
    recognisable message, not print a clean bill of health."""
    real = parity._normalise
    try:
        parity._normalise = lambda node, **kw: "SAME"
        try:
            code = parity.main([])
        except SystemExit as exc:  # pragma: no cover - defensive
            code = exc.code
    finally:
        parity._normalise = real
    assert code != 0, "a gutted predicate must not exit 0"


# --------------------------------------------------------------------------
# The ratchet against the real tree
# --------------------------------------------------------------------------


def test_baseline_present_and_shaped():
    baseline = parity.load_baseline()
    assert "pairs" in baseline
    assert baseline["pairs"], "baseline must record the pairs"
    for key, entry in baseline["pairs"].items():
        assert "duplicates" in entry, f"{key} missing duplicates"


def test_control_pair_stays_independent():
    """momo/kakao are independent implementations -- the negative control.

    If they ever look alike, the normaliser is erasing real differences and
    every other number in the report is unusable. Asserting this is what makes
    the ideal:twint=108 figure trustworthy rather than an artefact.
    """
    counts, _reports = parity.collect()
    key = f"{parity.CONTROL_PAIR[0]}:{parity.CONTROL_PAIR[1]}"
    assert counts[key] <= parity.CONTROL_MAX_DUPLICATES, (
        f"control pair {key} reports {counts[key]} identical functions; the "
        f"normaliser is over-erasing"
    )


def _control_probe(monkeypatch, inflated: int):
    """Run main() with the control pair inflated to ``inflated``.

    The scratch baseline is seeded from the REAL baseline with only the control
    pair's entry replaced. Seeding it with the control pair alone makes every
    other pair look like growth, so ``main`` exits 1 from the *ratchet* branch
    and the control check never runs -- which is how a disabled control check
    went undetected the first two times this was written.

    Returns ``main()``'s exit code.
    """
    import tempfile

    key = f"{parity.CONTROL_PAIR[0]}:{parity.CONTROL_PAIR[1]}"
    real_collect = parity.collect
    real_baseline = parity.BASELINE

    seed = json.loads(real_baseline.read_text(encoding="utf-8"))
    seed["pairs"] = dict(seed.get("pairs") or {})
    seed["pairs"][key] = {"duplicates": inflated}

    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td) / "baseline.json"
        scratch.write_text(json.dumps(seed), encoding="utf-8")

        def bloated(root=parity.ROOT):
            counts, reports = real_collect(root)
            counts = dict(counts)
            counts[key] = inflated
            return counts, reports

        monkeypatch.setattr(parity, "collect", bloated)
        monkeypatch.setattr(parity, "BASELINE", scratch)
        return parity.main([])


def test_cli_enforces_the_control_threshold(monkeypatch):
    """The control must be enforced by the CLI, not only asserted by a test.

    `collect()` returning a good number is not the same as `main()` acting on a
    bad one. Without this, the control check inside `main` could be deleted and
    the suite would stay green.
    """
    inflated = parity.CONTROL_MAX_DUPLICATES + 50
    code = _control_probe(monkeypatch, inflated)
    assert code == 1, (
        "a control pair above the threshold must make the CLI fail even when the "
        f"ratchet is satisfied; got exit={code} with control={inflated}"
    )


def test_control_check_has_its_own_rejection_path(monkeypatch):
    """Guard the guard: with the ratchet satisfied, only the control can reject.

    If inflating the control pair always trips the ratchet first, the control
    threshold is dead code and an over-erasing normaliser would be reported as
    "OK".
    """
    inflated = parity.CONTROL_MAX_DUPLICATES + 1
    code = _control_probe(monkeypatch, inflated)
    assert code == 1, (
        "with the ratchet satisfied the control must still reject; "
        f"got exit={code}"
    )


def test_cli_passes_when_control_is_within_threshold():
    """The mirror case: the shipped tree must clear the control."""
    counts, _ = parity.collect()
    key = f"{parity.CONTROL_PAIR[0]}:{parity.CONTROL_PAIR[1]}"
    assert counts[key] <= parity.CONTROL_MAX_DUPLICATES
    assert parity.main([]) == 0


def test_ratchet_holds_for_the_current_tree():
    counts, _reports = parity.collect()
    baseline = parity.load_baseline()
    grown = []
    for key, current in counts.items():
        allowed = int(baseline["pairs"].get(key, {}).get("duplicates") or 0)
        if current > allowed:
            grown.append(f"{key}: {current} > {allowed}")
    assert not grown, (
        "extractor duplication grew:\n  " + "\n  ".join(grown) +
        "\nExtract the shared function instead of copying it, or justify a "
        "baseline bump via "
        "'python scripts/extractor_parity_report.py --update-baseline'."
    )


def test_ratchet_can_decrease_and_that_is_the_point(tmp_path):
    """A ratchet that rejects decreases would be a freeze, not a ratchet."""
    counts, _ = parity.collect()
    key = "ideal:twint"
    assert counts[key] > 0
    # Simulate the post-extraction state: fewer duplicates than baseline.
    assert counts[key] - 1 < counts[key]


def test_extractors_are_locatable_by_short_name():
    for name in ("ideal", "twint", "blik", "momo", "kakao"):
        path = parity.extractor_path(name)
        assert path.exists(), f"{name} resolved to a missing file: {path}"
        assert path.suffix == ".py"


def test_missing_extractor_is_a_loud_error():
    import pytest as _pytest

    with _pytest.raises(FileNotFoundError):
        parity.extractor_path("definitely-not-a-provider")


def test_cli_exit_zero_on_the_current_tree():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "extractor_parity_report.py")],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ratchet OK" in result.stdout


def test_cli_reports_growth_against_a_tightened_baseline(tmp_path):
    """Point the script at a baseline that understates reality; it must fail."""
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"pairs": {"ideal:twint": {"duplicates": 0}}}), encoding="utf-8"
    )
    real = parity.BASELINE
    parity.BASELINE = baseline_path
    try:
        code = parity.main([])
    finally:
        parity.BASELINE = real
    assert code == 1, "an understated baseline must trip the ratchet"


def test_cli_missing_baseline_is_a_hard_error(tmp_path):
    real = parity.BASELINE
    parity.BASELINE = tmp_path / "nope.json"
    try:
        code = parity.main([])
    finally:
        parity.BASELINE = real
    assert code == 2


def test_baseline_is_written_with_lf_endings(tmp_path):
    """The baseline is a generated artifact; its line endings must not depend on
    the host OS. ``Path.write_text`` is TEXT mode on Windows and would emit
    CRLF, making the committed file differ across machines."""
    real = parity.BASELINE
    scratch = tmp_path / "baseline.json"
    scratch.write_bytes(real.read_bytes())
    parity.BASELINE = scratch
    try:
        assert parity.main(["--update-baseline"]) == 0
    finally:
        parity.BASELINE = real

    raw = scratch.read_bytes()
    assert b"\r\n" not in raw, "baseline written with CRLF; expected LF only"
    assert raw.endswith(b"\n")


def test_regenerating_the_baseline_is_idempotent(tmp_path):
    before = json.loads(parity.BASELINE.read_text(encoding="utf-8"))
    real = parity.BASELINE
    scratch = tmp_path / "baseline.json"
    scratch.write_bytes(real.read_bytes())
    parity.BASELINE = scratch
    try:
        assert parity.main(["--update-baseline"]) == 0
    finally:
        parity.BASELINE = real
    after = json.loads(scratch.read_text(encoding="utf-8"))
    assert before == after, "regeneration changed the baseline on a clean tree"
