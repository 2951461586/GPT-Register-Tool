"""Every field of the registration runtime state that is read must also be written."""

from __future__ import annotations

import ast
import sys
import textwrap
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sms_tool.registration.runtime import _FIELD_GROUPS  # noqa: E402

READ_BUT_NEVER_WRITTEN: dict[str, str] = {}
NEVER_READ_NEVER_WRITTEN: dict[str, str] = {}


def _attribute_counts(source: str) -> tuple[dict[str, int], dict[str, int]]:
    reads: dict[str, int] = defaultdict(int)
    writes: dict[str, int] = defaultdict(int)
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if isinstance(node, ast.Attribute) and node.attr in _FIELD_GROUPS:
            if isinstance(node.ctx, ast.Load):
                reads[node.attr] += 1
            elif isinstance(node.ctx, (ast.Store, ast.Del)):
                writes[node.attr] += 1
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                name = str(node.args[1].value)
                if name in _FIELD_GROUPS:
                    writes[name] += 1
    return dict(reads), dict(writes)


def _scan_package() -> tuple[dict[str, int], dict[str, int]]:
    reads: dict[str, int] = defaultdict(int)
    writes: dict[str, int] = defaultdict(int)
    for path in sorted((ROOT / "sms_tool").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        file_reads, file_writes = _attribute_counts(path.read_text(encoding="utf-8"))
        for name, count in file_reads.items():
            reads[name] += count
        for name, count in file_writes.items():
            writes[name] += count
    return dict(reads), dict(writes)


def _read_but_never_written(reads: dict[str, int], writes: dict[str, int], allowed: dict[str, str]) -> list[str]:
    return sorted(
        name for name in _FIELD_GROUPS if reads.get(name, 0) > 0 and writes.get(name, 0) == 0 and name not in allowed
    )


def _never_read_never_written(reads: dict[str, int], writes: dict[str, int], allowed: dict[str, str]) -> list[str]:
    return sorted(
        name for name in _FIELD_GROUPS if reads.get(name, 0) == 0 and writes.get(name, 0) == 0 and name not in allowed
    )


def test_it_counts_a_plain_read_and_write():
    reads, writes = _attribute_counts("state.oauth_result = state.at_probe")
    assert reads.get("at_probe") == 1
    assert writes.get("oauth_result") == 1


def test_it_counts_a_tuple_assignment_as_a_write():
    _reads, writes = _attribute_counts("s.success, s.error, s.registration_warning = verdict()")
    assert writes.get("success") == 1
    assert writes.get("error") == 1
    assert writes.get("registration_warning") == 1


def test_a_read_is_not_mistaken_for_a_write():
    _reads, writes = _attribute_counts("print(s.at_probe)")
    assert writes.get("at_probe", 0) == 0


def test_augmented_assignment_counts_as_a_write():
    _reads, writes = _attribute_counts("s.auth_flow_started += 1")
    assert writes.get("auth_flow_started") == 1


def test_a_read_without_a_write_is_flagged():
    assert _read_but_never_written({"at_probe": 3}, {}, {}) == ["at_probe"]


def test_a_read_with_a_write_is_not_flagged():
    assert _read_but_never_written({"at_probe": 3}, {"at_probe": 1}, {}) == []


def test_an_allowlisted_field_is_not_flagged():
    assert _read_but_never_written({"at_probe": 3}, {}, {"at_probe": "known"}) == []


def test_a_dead_field_is_flagged():
    assert _never_read_never_written({}, {}, {}) == sorted(_FIELD_GROUPS)


def test_a_field_that_is_only_written_is_not_dead():
    assert "at_probe" not in _never_read_never_written({}, {"at_probe": 1}, {})


def test_the_scan_reached_every_field():
    reads, writes = _scan_package()
    assert len(_FIELD_GROUPS) > 40
    covered = {name for name in _FIELD_GROUPS if reads.get(name) or writes.get(name)}
    assert len(covered) > len(_FIELD_GROUPS) - 5, f"scan only saw {len(covered)} of {len(_FIELD_GROUPS)} state fields"


def test_every_field_that_is_read_is_also_written():
    reads, writes = _scan_package()
    unexpected = _read_but_never_written(reads, writes, READ_BUT_NEVER_WRITTEN)
    assert unexpected == [], (
        "these fields are read by sms_tool/ but never assigned anywhere, so "
        "every reader silently gets the dataclass default: " + ", ".join(unexpected)
    )


def test_no_field_is_both_unread_and_unwritten():
    reads, writes = _scan_package()
    unexpected = _never_read_never_written(reads, writes, NEVER_READ_NEVER_WRITTEN)
    assert unexpected == [], "dead state fields (neither read nor written): " + ", ".join(unexpected)


def test_the_allowlists_carry_no_stale_entries():
    reads, writes = _scan_package()
    for name, reason in READ_BUT_NEVER_WRITTEN.items():
        assert name in _FIELD_GROUPS, f"{name}: not a state field"
        assert reads.get(name, 0) > 0, f"{name} is no longer read -- drop it from READ_BUT_NEVER_WRITTEN ({reason})"
        assert writes.get(name, 0) == 0, f"{name} is now written -- drop it from READ_BUT_NEVER_WRITTEN ({reason})"
    for name, reason in NEVER_READ_NEVER_WRITTEN.items():
        assert name in _FIELD_GROUPS, f"{name}: not a state field"
        assert (reads.get(name, 0), writes.get(name, 0)) == (0, 0), (
            f"{name} is now used -- drop it from NEVER_READ_NEVER_WRITTEN ({reason})"
        )
