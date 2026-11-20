"""Pin the shared ratchet plumbing (``scripts/_baseline.py``).

Why this exists
---------------
Twenty-plus gates re-implemented baseline IO, and the copies had already drifted:
some wrote CRLF on Windows, some escaped non-ASCII keys and some did not, and a
few dropped the trailing newline — so re-freezing the same content produced a
different diff depending on which gate did it. ``scripts/_baseline.py`` is the
one copy new gates start from, which means it needs its own pin: a shared helper
with a silent bug would spread the bug to every future gate.

The assertions are deliberately about *bytes*: the file-format decisions (LF,
2-space indent, unescaped Chinese, caller-ordered keys) are the whole point.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import _baseline  # noqa: E402


def test_write_json_is_lf_with_a_trailing_newline_and_readable_chinese(tmp_path: Path) -> None:
    target = tmp_path / "baseline.json"

    _baseline.write_json(target, {"files": {"测试目录": 3}, "schema": "x.v1"})

    raw = target.read_bytes()
    assert b"\r\n" not in raw, "a CRLF baseline shows up as a 200-line diff on Linux"
    assert raw.endswith(b"\n")
    assert "测试目录".encode() in raw, "ensure_ascii=False keeps group names reviewable"


def test_write_json_keeps_the_callers_key_order(tmp_path: Path) -> None:
    """Baselines are curated for review; sorting them would hide the real change."""
    target = tmp_path / "baseline.json"

    _baseline.write_json(target, {"zebra": 1, "alpha": 2})

    assert list(json.loads(target.read_text(encoding="utf-8"))) == ["zebra", "alpha"]


def test_read_json_tolerates_a_bom(tmp_path: Path) -> None:
    target = tmp_path / "baseline.json"
    target.write_bytes('{"schema": "x.v1"}'.encode("utf-8-sig"))

    assert _baseline.read_json(target) == {"schema": "x.v1"}


def test_read_json_refuses_a_non_object(tmp_path: Path) -> None:
    target = tmp_path / "baseline.json"
    target.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(SystemExit):
        _baseline.read_json(target)


def test_the_update_flag_is_spelled_the_same_way_everywhere() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    _baseline.add_update_flag(parser)

    assert _baseline.update_requested(parser.parse_args(["--update-baseline"]))
    assert not _baseline.update_requested(parser.parse_args([]))


def test_print_failures_is_ascii_safe_and_returns_the_exit_code() -> None:
    import io

    stream = io.StringIO()
    code = _baseline.print_failures("ratchet FAILED:", ["tests/config: 9 > 8"], stream=stream)

    assert code == 1
    assert stream.getvalue().splitlines() == ["ratchet FAILED:", "  - tests/config: 9 > 8"]
    assert stream.getvalue().isascii(), "a non-ASCII bullet would raise on a cp1252 CI console"


def test_an_adopted_gate_produces_the_shared_format(tmp_path: Path) -> None:
    """One real consumer, so the helper is not pinned only against itself."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import unittest_style_ratchet as ratchet

    target = tmp_path / "unittest_style_baseline.json"
    _baseline.write_json(target, ratchet.build_baseline({"config": ["test_a.py"]}))

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "schema": "unittest_style.v1",
        "files": {"config": 1},
    }
