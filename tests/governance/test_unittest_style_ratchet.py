"""Pin the ``unittest`` -> pytest migration so it can only move forward.

Why this exists
---------------
175 test files still declare ``unittest.TestCase`` next to 261 that use plain
pytest functions. pytest runs both, so nothing failed and nothing noticed the
split -- but a ``unittest`` class gets no fixtures, cannot be parametrised, and
its ``setUp`` teardown does not run when ``setUp`` itself raises.

A 175-file codemod is exactly the change this repository treats as unverifiable
(a dropped ``setUp`` leaves the tests passing, vacuously), so
``scripts/unittest_style_ratchet.py`` freezes the count per directory and only
lets it fall. This file pins the ratchet itself:

* the classifier recognises a ``TestCase`` subclass by any name, not just
  ``unittest.TestCase`` (files use both ``unittest.TestCase`` and a local alias);
* ``unittest.mock`` alone does not make a file "unittest style";
* a synthetic tree proves growth fails and shrinkage is allowed;
* the real tree is held to the committed baseline.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from textwrap import dedent

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import unittest_style_ratchet as ratchet  # noqa: E402


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(body), encoding="utf-8")


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------


def test_a_testcase_subclass_is_counted_however_it_is_spelled(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a/test_bare.py",
        """
        import unittest

        class Tests(unittest.TestCase):
            def test_x(self):
                pass
        """,
    )
    _write(
        tmp_path,
        "a/test_alias.py",
        """
        from unittest import TestCase

        class Tests(TestCase):
            def test_x(self):
                pass
        """,
    )
    _write(
        tmp_path,
        "a/test_mock_only.py",
        """
        from unittest import mock

        def test_x():
            assert mock.Mock() is not None
        """,
    )
    _write(
        tmp_path,
        "a/test_pytest_style.py",
        """
        import pytest

        def test_x(monkeypatch):
            monkeypatch.setenv("A", "B")
        """,
    )

    found = ratchet.scan(tmp_path)

    assert sorted(found["a"]) == ["test_alias.py", "test_bare.py"]


def test_nested_directories_are_grouped_by_their_own_name(tmp_path: Path) -> None:
    _write(tmp_path, "accounts/test_a.py", "import unittest\nclass T(unittest.TestCase):\n    pass\n")
    _write(tmp_path, "accounts/deep/test_b.py", "import unittest\nclass T(unittest.TestCase):\n    pass\n")

    found = ratchet.scan(tmp_path)

    assert sorted(found) == ["accounts", "accounts/deep"]


# --------------------------------------------------------------------------
# The ratchet
# --------------------------------------------------------------------------


def test_a_new_unittest_file_fails() -> None:
    problems = ratchet.violations({"config": ["test_new.py", "test_old.py"]}, {"config": 1})

    assert problems and "test_new.py" in problems[0]


def test_a_new_directory_fails() -> None:
    problems = ratchet.violations({"brand_new": ["test_a.py"]}, {})

    assert problems and "no baseline entry" in problems[0]


def test_migrating_a_file_is_allowed_and_reported_as_stale() -> None:
    found = {"config": ["test_old.py"]}

    assert ratchet.violations(found, {"config": 2}) == []
    assert ratchet.stale_entries(found, {"config": 2}), "a shrinking directory must ask for a tighter baseline"


def test_the_real_tree_holds_the_baseline() -> None:
    problems = ratchet.violations(ratchet.scan(ROOT / "tests"), ratchet.load_baseline())

    assert problems == [], "unittest-style debt grew:\n  " + "\n  ".join(problems)


def test_the_worked_example_is_converted() -> None:
    """The recipe this ratchet documents has one file to point at.

    The check is the ratchet's own classifier, not a substring: the file's
    docstring *names* `unittest.TestCase` to explain the migration, and pinning
    the prose would make the test fail for the wrong reason.
    """
    source = (ROOT / "tests" / "config" / "test_logging_crash_hooks.py").read_text(encoding="utf-8")

    assert not ratchet._is_unittest_file(ast.parse(source))
    assert "@pytest.fixture" in source


@pytest.mark.parametrize("directory", ["config", "proxy", "accounts"])
def test_a_migrated_directory_never_regrows(directory: str) -> None:
    baseline = ratchet.load_baseline()
    assert directory in baseline, f"{directory} disappeared from the baseline"
    assert ratchet.violations(ratchet.scan(ROOT / "tests"), baseline) == []
