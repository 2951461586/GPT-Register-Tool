"""Rotation of the append-only payment run trail.

``runtime/payment_link_runs.jsonl`` is reconciliation state, so it sits in
``scripts/runtime_retention.py``'s ``NEVER_DELETE`` set -- nothing may delete it.
That also means nothing used to *bound* it: it was 5.7 MiB of complete records on
2026-10-20 and only grows. Rotation is the bound: once the live file crosses
``ROTATE_AFTER_BYTES`` it is renamed aside with a UTC stamp and a fresh file is
started, and the retention planner retires the archives after 30 days.
"""

import json
import re

import pytest

from sms_tool.pay_link import persistence


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "payment_link_runs.jsonl"
    monkeypatch.setattr(persistence, "_state_path", lambda: path)
    return path


def test_a_small_trail_is_appended_in_place(state_file):
    persistence._persist_run({"email": "a@example.test"})
    persistence._persist_run({"email": "b@example.test"})

    lines = [json.loads(line)["email"] for line in state_file.read_text(encoding="utf-8").splitlines()]
    assert lines == ["a@example.test", "b@example.test"]
    assert list(state_file.parent.glob("payment_link_runs.*.jsonl")) == []


def test_the_trail_rotates_once_it_crosses_the_cap(state_file, monkeypatch):
    monkeypatch.setattr(persistence, "ROTATE_AFTER_BYTES", 64)
    state_file.write_text('{"filler": "x"}\n' * 8, encoding="utf-8")

    persistence._persist_run({"email": "a@example.test"})

    archives = list(state_file.parent.glob("payment_link_runs.*.jsonl"))
    assert len(archives) == 1
    assert re.fullmatch(r"payment_link_runs\.\d{8}T\d{6}Z\.jsonl", archives[0].name)
    assert archives[0].read_text(encoding="utf-8").startswith('{"filler"')
    assert [json.loads(line)["email"] for line in state_file.read_text(encoding="utf-8").splitlines()] == [
        "a@example.test"
    ]


def test_a_missing_trail_is_created_not_rotated(state_file):
    persistence._persist_run({"email": "a@example.test"})

    assert list(state_file.parent.glob("payment_link_runs.*.jsonl")) == []
    assert len(state_file.read_text(encoding="utf-8").splitlines()) == 1


def test_two_rotations_in_the_same_second_do_not_overwrite_each_other(state_file, monkeypatch):
    monkeypatch.setattr(persistence, "ROTATE_AFTER_BYTES", 64)
    monkeypatch.setattr(persistence.time, "strftime", lambda fmt, _struct: "20261020T120000Z")

    state_file.write_text('{"filler": "x"}\n' * 8, encoding="utf-8")
    persistence._persist_run({"email": "a@example.test"})
    state_file.write_text('{"filler": "y"}\n' * 8, encoding="utf-8")
    persistence._persist_run({"email": "b@example.test"})

    archives = sorted(path.name for path in state_file.parent.glob("payment_link_runs.*.jsonl"))
    assert archives == [
        "payment_link_runs.20261020T120000Z-1.jsonl",
        "payment_link_runs.20261020T120000Z.jsonl",
    ]
