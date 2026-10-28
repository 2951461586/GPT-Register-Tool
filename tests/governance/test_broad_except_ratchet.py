"""The broad-except ratchet must actually gate (and must actually count).

The gate itself lives in ``scripts/broad_except_ratchet.py``; this module proves it
still sees what it claims to see. A ratchet whose counter silently stopped matching
would pass forever, which is worse than no ratchet -- that failure mode is exactly why
``tests/governance/test_bare_print_ratchet.py`` exists next to its script too.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "broad_except_ratchet.py"


def _load_ratchet():
    spec = importlib.util.spec_from_file_location("broad_except_ratchet", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["broad_except_ratchet"] = module
    spec.loader.exec_module(module)
    return module


ratchet = _load_ratchet()


def test_tree_is_within_the_frozen_allowance():
    """No file may exceed the count frozen in scripts/broad_except_baseline.json."""
    counts = ratchet.scan()
    problems = ratchet.check(counts, ratchet.load_baseline())
    assert problems == [], "broad-except grew:\n  " + "\n  ".join(problems)


def test_a_new_broad_handler_is_reported():
    """A file absent from the baseline has an allowance of zero."""
    counts = ratchet.scan()
    problems = ratchet.violations(counts, {"files": {}, "totals": {"broad": 0, "silent": 0}})
    assert problems, "an empty baseline must flag every file that has a broad handler"
    assert all("handlers" in problem for problem in problems)


def test_counter_separates_broad_from_narrow_and_silent(tmp_path):
    """The AST rules: broad vs narrow, and silent (bare ``pass``) vs explained."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "sample.py").write_text(
        "\n".join(
            [
                "def broad_silent():",
                "    try:",
                "        return 1",
                "    except Exception:",  # broad + silent
                "        pass",
                "",
                "def broad_explained():",
                "    try:",
                "        return 1",
                "    except Exception as exc:",  # broad, not silent
                "        return str(exc)",
                "",
                "def narrow():",
                "    try:",
                "        return 1",
                "    except (OSError, ValueError):",  # names what it expects
                "        return None",
                "",
                "def tuple_with_broad():",
                "    try:",
                "        return 1",
                "    except (ValueError, Exception):",  # broad by virtue of the tuple
                "        return None",
                "",
            ]
        ),
        encoding="utf-8",
    )

    counts = ratchet.scan(root=tmp_path, trees=("pkg",))

    assert counts == {"pkg/sample.py": {"broad": 3, "silent": 1}}


def test_global_budget_catches_trading_one_file_against_another():
    """Per-file allowances alone let a cleanup in one module pay for growth in another."""
    counts = {"pkg/a.py": {"broad": 1, "silent": 0}}
    baseline = {
        "files": {"pkg/a.py": {"broad": 1, "silent": 0}},
        "policy": {"max_broad": 0, "max_silent": 0},
    }
    problems = ratchet.check(counts, baseline)

    assert any("total broad handlers" in problem for problem in problems)


def test_whitelist_entry_without_a_reason_fails():
    counts = {"pkg/a.py": {"broad": 2, "silent": 0}}
    baseline = {
        "files": {"pkg/a.py": {"broad": 2, "silent": 0}},
        "policy": {"max_broad": 2, "max_silent": 0},
        "whitelist": {"pkg/a.py": {"broad": 2, "silent": 0, "reason": "   "}},
    }
    problems = ratchet.check(counts, baseline)

    assert any("without a reason" in problem for problem in problems)


def test_stale_whitelist_entry_fails():
    """Permission for a handler that no longer exists is not permission."""
    counts = {"pkg/b.py": {"broad": 1, "silent": 0}}
    baseline = {
        "files": {"pkg/b.py": {"broad": 1, "silent": 0}},
        "policy": {"max_broad": 1, "max_silent": 0},
        "whitelist": {"pkg/gone.py": {"broad": 1, "silent": 0, "reason": "used to be needed"}},
    }
    problems = ratchet.check(counts, baseline)

    assert any("no longer has a broad handler" in problem for problem in problems)


def test_shipped_whitelist_has_reasons():
    """Every reviewed entry in the real baseline states why it is accepted."""
    baseline = ratchet.load_baseline()
    whitelist = baseline.get("whitelist", {})
    assert whitelist, "the whitelist documents the accepted cases; an empty list is a stub"
    for name, entry in whitelist.items():
        assert entry.get("reason", "").strip(), name


def test_baseline_file_is_present_and_parsable():
    baseline = ratchet.load_baseline()
    assert isinstance(baseline.get("files"), dict)
    assert baseline["files"], "the frozen baseline must not be empty"
    assert baseline["totals"]["broad"] >= baseline["totals"]["silent"]
