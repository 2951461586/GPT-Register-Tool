"""Tests for ``scripts/sunk_copy_parity.py``.

The guard exists because ``upi_link`` deliberately duplicates three helpers from
``paypal_link.gen_link`` (``_emit`` / ``_load_json`` /
``_normalize_hosted_checkout_url``) to keep the import direction
``gen_pp_link -> upi_link``.  A one-sided edit is invisible until the PayPal and
UPI lanes behave differently.

These tests pin the properties that make the guard meaningful:

* the predicate is trustworthy in BOTH directions (fixtures, including
  docstring-only differences that must stay invisible);
* every declared pair actually exists in both files, so a rename cannot make the
  check pass vacuously;
* the current tree is at parity, so the gate is green rather than skipped;
* **drift fails** -- proved by mutation, because a guard that cannot fail is not
  a guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sunk_copy_parity as parity  # noqa: E402  # pyright: ignore[reportMissingImports]


def test_predicate_fixtures_are_trustworthy():
    assert parity.predicates_are_trustworthy() == []


def test_declared_pairs_exist_in_both_files():
    records = parity.compare()
    assert records, "sunk_copy_parity.PAIRS is empty -- the guard would pass vacuously"
    missing = [record["function"] for record in records if not record["present"]]
    assert not missing, f"sunk copy missing from one side (renamed?): {missing}"


def test_current_tree_is_at_parity():
    drift = [record["function"] for record in parity.compare() if not record["identical"]]
    assert not drift, (
        f"sunk copies drifted between upi_link and gen_link: {drift}. "
        "Keep the two bodies identical (docstrings may differ), or stop calling "
        "it a sunk copy and import the canonical one."
    )


def test_docstring_difference_is_not_drift():
    a = 'def f(x):\n    """alpha."""\n    return x + 1\n'
    b = 'def f(x):\n    """beta."""\n    return x + 1\n'
    assert parity._function_signature(a, "f") == parity._function_signature(b, "f")


def test_real_body_difference_is_drift():
    a = "def f(x):\n    return x + 1\n"
    b = "def f(x):\n    return x - 1\n"
    assert parity._function_signature(a, "f") != parity._function_signature(b, "f")


def test_main_is_green_on_the_current_tree():
    assert parity.main([]) == 0


def test_main_reports_drift(tmp_path, monkeypatch, capsys):
    (tmp_path / "left.py").write_text("def f(x):\n    return x + 1\n", encoding="utf-8", newline="\n")
    (tmp_path / "right.py").write_text("def f(x):\n    return x - 1\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(parity, "ROOT", tmp_path)
    monkeypatch.setattr(parity, "PAIRS", (("left.py", "right.py", "f", "mutation test"),))

    assert parity.main([]) == 1
    assert "drift" in capsys.readouterr().out.lower()


def test_main_reports_a_missing_pair(tmp_path, monkeypatch, capsys):
    (tmp_path / "left.py").write_text("def f(x):\n    return x + 1\n", encoding="utf-8", newline="\n")
    (tmp_path / "right.py").write_text("def g(x):\n    return x + 1\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(parity, "ROOT", tmp_path)
    monkeypatch.setattr(parity, "PAIRS", (("left.py", "right.py", "f", "mutation test"),))

    assert parity.main([]) == 1
    assert "not found in both" in capsys.readouterr().out
