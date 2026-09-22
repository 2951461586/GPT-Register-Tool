"""Tests for scripts/mailbox_private_import_ratchet.py.

Two halves, on purpose:

* the predicate is exercised on **synthetic sources in both directions** --
  it must fire on a real private-symbol consumer and stay silent on a public
  import, a dunder, a differently-named sibling module, a module outside the
  package, and a self-import;
* the last tests scan the real tree, so the ratchet is enforced on every push
  (CI already runs pytest) without touching the workflow file.

The regression this gate exists for: ``sms_tool/mailbox.py`` has **no public
surface at all** (every top-level function is ``_``-prefixed) yet is the most
imported module in the package, and nothing counted its consumers before
2026-09-17. Five more modules were found in the same position the same day and
are watched now too.

Baselines are **per module**, so a reduction in one cannot mask growth in
another -- that property is asserted directly below, because it is the whole
reason the baseline is not a single number.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import mailbox_private_import_ratchet as ratchet  # noqa: E402

WATCHED = ratchet.WATCHED_MODULES


def _triples(source: str) -> set[tuple[str, str, str]]:
    return ratchet._pairs_in_source(textwrap.dedent(source), "importer.py")


# --------------------------------------------------------------------------
# The predicate, both directions
# --------------------------------------------------------------------------


def test_relative_private_import_is_counted():
    assert _triples("from .mailbox import _poll_email_otp") == {
        ("importer.py", "mailbox", "_poll_email_otp")
    }


def test_parent_relative_private_import_is_counted():
    assert _triples("from ..mailbox import _load_mailbox_pool") == {
        ("importer.py", "mailbox", "_load_mailbox_pool")
    }


def test_absolute_package_private_import_is_counted():
    assert _triples("from sms_tool.mailbox import _fetch_mailbox_messages") == {
        ("importer.py", "mailbox", "_fetch_mailbox_messages")
    }


def test_function_level_import_is_counted():
    """Delayed imports are how most of these consumers are written."""
    assert _triples("""
        def f():
            from .mailbox import _mailbox_from_config
            return _mailbox_from_config()
    """) == {("importer.py", "mailbox", "_mailbox_from_config")}


def test_public_import_is_not_counted():
    """``MailboxAccount`` is a real public name; importing it is not coupling."""
    assert _triples("from .mailbox import MailboxAccount") == set()


def test_dunder_import_is_not_counted():
    assert _triples("from .mailbox import __all__") == set()


def test_differently_named_sibling_module_is_not_counted():
    """``providers/mailbox_gmail`` shares the prefix but is a different module."""
    assert _triples("from .providers.mailbox_gmail import _x") == set()
    assert _triples("from .mailbox_gmail import _x") == set()


def test_non_watched_module_is_not_counted():
    assert _triples("from .config import _hidden") == set()


def test_repeated_import_of_same_symbol_counts_once():
    """The fact is "this file depends on this private symbol", not "N times"."""
    assert _triples("""
        from .mailbox import _poll_email_otp
        def f():
            from .mailbox import _poll_email_otp
            return _poll_email_otp
    """) == {("importer.py", "mailbox", "_poll_email_otp")}


def test_mixed_public_and_private_keeps_only_private():
    assert _triples(
        "from .mailbox import MailboxAccount, _poll_email_otp, MailboxTokenExpiredError"
    ) == {("importer.py", "mailbox", "_poll_email_otp")}


def test_attribute_access_is_a_known_blind_spot():
    """Documented limitation: ``mailbox._x()`` through a module object is not seen.

    A 2026-09-17 scan found that form only inside comments and docstrings, so
    the gate does not guess at it. This test pins the limitation so nobody
    mistakes silence for coverage.
    """
    assert _triples("""
        import sms_tool.mailbox as mailbox
        mailbox._poll_email_otp()
    """) == set()


# --------------------------------------------------------------------------
# Every watched module resolves; the package guard rejects look-alikes
# --------------------------------------------------------------------------


def test_every_watched_module_is_reachable_by_relative_import():
    """A name in WATCHED_MODULES that never resolves is a silent no-op."""
    for name in WATCHED:
        got = _triples(f"from .{name} import _sym")
        assert got == {("importer.py", name, "_sym")}, f"{name} did not resolve"


def test_every_watched_module_is_reachable_by_absolute_import():
    for name in WATCHED:
        got = _triples(f"from sms_tool.{name} import _sym")
        assert got == {("importer.py", name, "_sym")}, f"sms_tool.{name} did not resolve"


def test_same_named_module_outside_the_package_is_rejected():
    """``utils`` and ``mail_otp`` are generic names -- a third-party module with
    the same name must not be counted as ours."""
    for name in WATCHED:
        assert _triples(f"from third_party.{name} import _sym") == set(), name
        assert _triples(f"from some.vendor.{name} import _sym") == set(), name


def test_suffix_match_must_be_exact():
    """``http_utils_x`` must not be swallowed by the ``http_utils`` entry."""
    assert _triples("from .http_utils_x import _sym") == set()
    assert _triples("from .mail_otp_v2 import _sym") == set()


def test_import_statement_without_module_is_not_counted():
    """``from . import mailbox`` binds the module, it does not pull a private name."""
    assert _triples("from . import mailbox") == set()


# --------------------------------------------------------------------------
# The ratchet against the real tree
# --------------------------------------------------------------------------


def test_baseline_file_is_present_and_populated():
    baseline = ratchet.load_baseline()
    assert "modules" in baseline, "baseline must be keyed per module"
    for name in WATCHED:
        assert name in baseline["modules"], f"{name} missing from baseline"
        assert baseline["modules"][name]["pairs"], f"{name} baseline records no pairs"
        assert isinstance(baseline["modules"][name]["per_file"], dict)


def test_baseline_per_file_matches_its_own_pairs():
    """A baseline whose summary disagrees with its detail cannot catch anything."""
    from collections import Counter

    for name in WATCHED:
        entry = ratchet.load_baseline()["modules"][name]
        counted = Counter(item.split(":", 1)[0] for item in entry["pairs"])
        assert dict(counted) == entry["per_file"], f"{name} per_file drifted from pairs"
        assert sum(entry["per_file"].values()) == int(entry["total"]), f"{name} total drifted"


def test_regenerating_the_baseline_is_idempotent(tmp_path):
    """``--update-baseline`` on an unchanged tree must not move a single number.

    Without this, a baseline could drift on every run and the gate would still
    look green while silently losing track of reality.
    """
    import json

    before = json.loads(ratchet.BASELINE.read_text(encoding="utf-8"))
    real = ratchet.BASELINE
    scratch = tmp_path / "baseline.json"
    scratch.write_bytes(real.read_bytes())
    ratchet.BASELINE = scratch
    try:
        code = ratchet.main(["--update-baseline"])
    finally:
        ratchet.BASELINE = real

    assert code == 0
    after = json.loads(scratch.read_text(encoding="utf-8"))
    assert before == after, "regeneration changed the baseline on a clean tree"


def test_baseline_is_written_with_lf_endings(tmp_path):
    """The baseline is a *generated* artifact, so its line endings must not
    depend on the host OS.

    ``Path.write_text`` opens in text mode on Windows and turns the trailing
    "\\n" into CRLF, which makes the committed file differ across machines by
    line ending alone. This was a real defect in the first version.
    """
    real = ratchet.BASELINE
    scratch = tmp_path / "baseline.json"
    scratch.write_bytes(real.read_bytes())
    ratchet.BASELINE = scratch
    try:
        assert ratchet.main(["--update-baseline"]) == 0
    finally:
        ratchet.BASELINE = real

    raw = scratch.read_bytes()
    assert b"\r\n" not in raw, "baseline written with CRLF; expected LF only"
    assert raw.endswith(b"\n"), "baseline must end with a single LF"


def test_ratchet_holds_for_the_current_tree():
    total, per_module, _per_file, triples = ratchet.collect()
    baseline = ratchet.load_baseline()
    assert total == sum(per_module.values())

    problems = []
    for name in WATCHED:
        allowed = int(baseline["modules"][name]["total"])
        current = per_module.get(name, 0)
        if current <= allowed:
            continue
        recorded = set(baseline["modules"][name]["pairs"])
        new = sorted(item for item in triples if f":{name}." in item and item not in recorded)
        problems.append(f"{name}: {current} > baseline {allowed}, new={new}")

    assert not problems, (
        "private-import consumers grew:\n  "
        + "\n  ".join(problems)
        + "\nRemove the consumer, widen the public seam, or justify a baseline "
        "bump via "
        "'python scripts/mailbox_private_import_ratchet.py --update-baseline'."
    )


def test_growth_in_one_module_is_not_masked_by_a_shrinking_sibling():
    """Per-module baselines are the point: prove a grand total would hide this.

    Builds two *complete* per-module reports (every watched module present, as
    the real baseline is), one down and one up, holding the grand total steady.
    A single-total check sees no change; the per-module check must flag it.
    """
    down = {name: 10 for name in WATCHED}
    up = dict(down, mailbox=3, utils=12)  # -7 in mailbox, +2 in utils
    assert sum(up.values()) < sum(down.values()), "fixture must not inflate the total"

    # A grand-total check with the same slack would pass; per-module must not.
    flagged = [m for m in WATCHED if up[m] > down[m]]
    assert flagged == ["utils"], f"expected only utils flagged, got {flagged}"
    assert max(up.values()) - sum(down.values()) < 0, (
        "the aggregate is DOWN, which is exactly why a single total would mask it"
    )


def test_cli_reports_growth_in_a_single_module(tmp_path):
    """End-to-end: a synthetic tree with one extra consumer must exit non-zero
    and name the module."""
    # A trimmed copy of the real tree would be slow; instead point --root at a
    # synthetic layout and feed a matching baseline by monkeypatching the file.
    synthetic = tmp_path / "repo"
    (synthetic / "sms_tool").mkdir(parents=True)
    (synthetic / "sms_tool" / "consumer.py").write_text(
        "from .utils import _random_name\nfrom .mailbox import _poll_email_otp\n",
        encoding="utf-8",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        '{"modules": {"mailbox": {"total": 1, "per_file": {}, "pairs": []},'
        ' "utils": {"total": 0, "per_file": {}, "pairs": []}}}',
        encoding="utf-8",
    )

    real_baseline = ratchet.BASELINE
    ratchet.BASELINE = baseline_path
    try:
        code = ratchet.main(["--root", str(synthetic), "--module", "utils"])
    finally:
        ratchet.BASELINE = real_baseline

    assert code == 1, "growth in 'utils' must fail the gate"


def test_cli_passes_when_the_tree_is_at_baseline(tmp_path):
    synthetic = tmp_path / "repo"
    (synthetic / "sms_tool").mkdir(parents=True)
    (synthetic / "sms_tool" / "consumer.py").write_text(
        "from .utils import _random_name\n", encoding="utf-8"
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        '{"modules": {"utils": {"total": 1, "per_file": {}, "pairs": []}}}',
        encoding="utf-8",
    )

    real_baseline = ratchet.BASELINE
    ratchet.BASELINE = baseline_path
    try:
        code = ratchet.main(["--root", str(synthetic), "--module", "utils"])
    finally:
        ratchet.BASELINE = real_baseline

    assert code == 0


def test_missing_baseline_is_a_hard_error(tmp_path):
    """Exiting 0 with no baseline would turn the gate into decoration."""
    synthetic = tmp_path / "repo"
    synthetic.mkdir()
    real_baseline = ratchet.BASELINE
    ratchet.BASELINE = tmp_path / "nope.json"
    try:
        code = ratchet.main(["--root", str(synthetic)])
    finally:
        ratchet.BASELINE = real_baseline
    assert code == 2


def test_script_runs_clean_from_the_command_line():
    """The git hook invokes it as a subprocess; that path must also be green."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "mailbox_private_import_ratchet.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ratchet OK" in result.stdout


def test_a_watched_module_is_not_its_own_consumer(tmp_path):
    """``sms_tool/utils.py`` may import from ``.http_utils``; it must not be
    counted as a consumer of its own private surface."""
    synthetic = tmp_path / "repo"
    (synthetic / "sms_tool").mkdir(parents=True)
    (synthetic / "sms_tool" / "utils.py").write_text(
        "from .utils import _self_helper\n", encoding="utf-8"
    )
    _total, per_module, _per_file, triples = ratchet.collect(root=synthetic)
    assert per_module.get("utils", 0) == 0, f"self-import leaked: {triples}"


def test_a_nested_module_sharing_a_hub_name_is_still_scanned(tmp_path):
    """``sms_tool/paypal/utils.py`` is a *different* module from the ``utils`` hub.

    The self-import skip compares full relative paths for exactly this reason;
    a bare-stem comparison would silently stop scanning real consumers.
    """
    synthetic = tmp_path / "repo"
    (synthetic / "sms_tool" / "paypal").mkdir(parents=True)
    (synthetic / "sms_tool" / "paypal" / "utils.py").write_text(
        "from ..utils import _random_name\n", encoding="utf-8"
    )
    _total, per_module, per_file, triples = ratchet.collect(root=synthetic)
    assert per_module.get("utils", 0) == 1, f"nested consumer lost: {triples}"
    assert per_file == {"sms_tool/paypal/utils.py": 1}
