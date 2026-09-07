"""Tests for scripts/delayed_import_ratchet.py.

Like test_precommit_guard.py, the last test scans the real package. CI already
runs pytest, so the ratchet is enforced on every push without touching
.github/workflows/ci.yml.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import delayed_import_ratchet  # noqa: E402


def _count(source: str) -> int:
    return len(delayed_import_ratchet._delayed_imports(ast.parse(textwrap.dedent(source))))


def test_top_level_imports_are_not_counted():
    assert _count("import os\nimport sys\n") == 0


def test_import_inside_function_is_counted():
    assert _count("""
        def f():
            import os
    """) == 1


def test_class_body_import_is_not_delayed():
    # A class body executes at import time, so for cycle and startup purposes
    # it behaves like a top-level import.
    assert _count("""
        class C:
            import os
    """) == 0


def test_nested_scopes_inside_a_function_still_count():
    assert _count("""
        def f():
            if True:
                try:
                    from . import x
                except ImportError:
                    import y
    """) == 2


def test_baseline_file_is_present_and_populated():
    baseline = delayed_import_ratchet.load_baseline()
    assert int(baseline["total"]) > 0
    assert isinstance(baseline["per_file"], dict)


def test_ratchet_holds_for_the_current_tree():
    total, _per_file = delayed_import_ratchet.count_delayed_imports()
    allowed = int(delayed_import_ratchet.load_baseline()["total"])
    assert total <= allowed, (
        f"delayed imports grew: {total} > baseline {allowed}. "
        "Delete one, or justify a baseline bump via "
        "'python scripts/delayed_import_ratchet.py --update-baseline'."
    )
