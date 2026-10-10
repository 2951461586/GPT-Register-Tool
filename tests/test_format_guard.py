"""Tests for ``scripts/format_guard.py``.

The guard exists because ``ruff format --check`` was never run on the protocol
registration lane, so on 2026-10-08 a mangled tuple in ``auth_flow/otp.py``
survived review: it was syntactically valid, and ``ruff check`` (E9/F63/F7/F82),
``compileall`` and the whole pytest suite are all blind to layout.

A scope predicate that can silently exclude the offender is decoration, so the
tests pin both halves: the scope *includes* the lane, and the checker *fires* on
a mangled file.  The real tree is checked too, but only when ``ruff`` is
installed -- CI installs it, so that is where the gate is authoritative.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "scripts" / "format_guard.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("format_guard", GUARD_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["format_guard"] = mod
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()

#: The exact shape the guard was written for: valid Python, mangled layout.
_MANGLED = 'def f():\n    return (\n    {        "ok": False,\n    }        , None)\n'
_CLEAN = 'def f():\n    return {"ok": False}, None\n'


# ------------------------------------------------------------------ scope


def test_scope_covers_the_protocol_registration_lane():
    scope = guard.scope_paths()
    assert "sms_tool/auth_flow/steps.py" in scope
    assert "sms_tool/auth_flow/otp.py" in scope
    assert "sms_tool/registration_handlers.py" in scope
    assert "sms_tool/registration_otp_stages.py" in scope


def test_scope_excludes_unrelated_modules():
    """Widening to the whole package would reformat ~100 unrelated files."""
    scope = guard.scope_paths()
    assert "sms_tool/cli.py" not in scope
    assert "sms_tool/auth_state.py" not in scope
    assert "sms_tool/registration_drivers/base.py" not in scope


def test_scope_has_no_pycache_entries():
    assert all("__pycache__" not in path for path in guard.scope_paths())


def test_every_scoped_path_exists():
    for path in guard.scope_paths():
        assert (ROOT / path).is_file(), path


# ------------------------------------------------------- predicate fires


@pytest.mark.skipif(importlib.util.find_spec("ruff") is None, reason="ruff not installed")
def test_check_fails_on_a_mangled_file(tmp_path: Path):
    (tmp_path / "mangled.py").write_text(_MANGLED, encoding="utf-8")
    returncode, output = guard.check(["mangled.py"], root=tmp_path)
    assert returncode != 0
    assert "reformat" in output.lower()


@pytest.mark.skipif(importlib.util.find_spec("ruff") is None, reason="ruff not installed")
def test_check_passes_on_a_clean_file(tmp_path: Path):
    (tmp_path / "clean.py").write_text(_CLEAN, encoding="utf-8")
    returncode, _ = guard.check(["clean.py"], root=tmp_path)
    assert returncode == 0


def test_the_mangled_fixture_is_actually_valid_python():
    """If the fixture stopped parsing, the gate would pass it for the wrong reason."""
    compile(_MANGLED, "<fixture>", "exec")


# --------------------------------------------------------------- real tree


@pytest.mark.skipif(importlib.util.find_spec("ruff") is None, reason="ruff not installed")
def test_the_lane_is_ruff_format_clean():
    assert guard.main([]) == 0


def test_list_mode_prints_the_scope(capsys: pytest.CaptureFixture[str]):
    assert guard.main(["--list"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert "sms_tool/auth_flow/steps.py" in printed
