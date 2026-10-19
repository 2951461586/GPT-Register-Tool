"""Guard the frozen ``sms_tool/`` root-module set.

Why this exists
---------------
The package root reached 155 modules before anything measured it: every coupling
ratchet counts *edges*, and a new root module adds no edge on the day it lands --
it only adds a place where the next edge can be born. ``root_module_ratchet``
freezes the exact names, so a new module must go to a domain subpackage (or
require an explicit baseline edit plus a decision).

Both directions are pinned: a new name fails, a removal passes and
``--update-baseline`` records it, and the baseline writer refuses to add names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import root_module_ratchet as ratchet  # noqa: E402  # type: ignore


def freeze(monkeypatch, tmp_path: Path, modules: set[str]) -> Path:
    target = tmp_path / "root_module_baseline.json"
    target.write_text(json.dumps({"modules": sorted(modules)}) + "\n", encoding="utf-8")
    monkeypatch.setattr(ratchet, "BASELINE", target)
    return target


def test_the_committed_tree_is_frozen(monkeypatch, tmp_path):
    freeze(monkeypatch, tmp_path, ratchet.collect_root_modules())
    assert ratchet.main([]) == 0


def test_a_new_root_module_fails_and_names_a_subpackage(monkeypatch, tmp_path, capsys):
    freeze(monkeypatch, tmp_path, {"a.py"})
    monkeypatch.setattr(ratchet, "collect_root_modules", lambda root=ratchet.ROOT: {"a.py", "b.py"})
    assert ratchet.main([]) == 1
    err = capsys.readouterr().err
    assert "b.py" in err
    assert "accounts" in err or "store" in err, "the failure must name a domain subpackage"


def test_removing_a_root_module_passes(monkeypatch, tmp_path, capsys):
    freeze(monkeypatch, tmp_path, {"a.py", "b.py"})
    monkeypatch.setattr(ratchet, "collect_root_modules", lambda root=ratchet.ROOT: {"a.py"})
    assert ratchet.main([]) == 0
    assert "b.py" in capsys.readouterr().out, "a pending baseline trim should be visible"


def test_update_baseline_records_a_removal(monkeypatch, tmp_path):
    target = freeze(monkeypatch, tmp_path, {"a.py", "b.py"})
    monkeypatch.setattr(ratchet, "collect_root_modules", lambda root=ratchet.ROOT: {"a.py"})
    assert ratchet.main(["--update-baseline"]) == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"modules": ["a.py"]}
    assert ratchet.main([]) == 0


def test_update_baseline_refuses_to_add_a_module(monkeypatch, tmp_path):
    target = freeze(monkeypatch, tmp_path, {"a.py"})
    monkeypatch.setattr(ratchet, "collect_root_modules", lambda root=ratchet.ROOT: {"a.py", "b.py"})
    assert ratchet.main(["--update-baseline"]) == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"modules": ["a.py"]}, "the baseline must not grow"


def test_the_baseline_is_sorted_and_lf(tmp_path):
    raw = (PROJECT_ROOT / "scripts/root_module_baseline.json").read_bytes()
    assert b"\r\n" not in raw
    modules = json.loads(raw.decode("utf-8"))["modules"]
    assert modules == sorted(modules)
