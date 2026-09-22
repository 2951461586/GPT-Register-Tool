"""Tests for sms_tool/operator_output.py and scripts/bare_print_ratchet.py.

Two halves, matching the ratchet pattern already used by
test_mailbox_private_import_ratchet.py:

* the seam (``operator_output.emit``) is exercised for its one-source / two-
  channel contract -- stdout gets the line, the logger gets a record, and both
  carry the same text;
* the ratchet is exercised in both directions on synthetic sources (a bare
  ``print`` grows the count, ``safe_print``/``emit`` do not) and then on the
  real tree so the frozen baseline is enforced on every push.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bare_print_ratchet as ratchet  # noqa: E402

from sms_tool.operator_output import emit  # noqa: E402


class _Sink(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _logger():
    log = logging.Logger("t.operator_output")
    log.propagate = False
    return log


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

def test_emit_feeds_both_channels_from_one_source(capsys):
    log = _logger()
    sink = _Sink()
    log.addHandler(sink)

    emit(log, "stage %s done", "create_account")

    assert "stage create_account done" in capsys.readouterr().out
    assert [r.getMessage() for r in sink.records] == ["stage create_account done"]


def test_emit_passes_extra_fields_to_the_log(capsys):
    log = _logger()
    sink = _Sink()
    log.addHandler(sink)

    emit(log, "ready", extra={"event": "x", "account_ref": "abc"})

    assert sink.records[0].event == "x"
    assert sink.records[0].account_ref == "abc"


# ---------------------------------------------------------------------------
# The ratchet predicate, both directions
# ---------------------------------------------------------------------------

def _count(source: str) -> int:
    path = ROOT / "runtime" / "tmp" / "_ratchet_probe.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return ratchet._bare_print_count(path)


def test_bare_print_is_counted():
    assert _count("print(1)\nprint(2)\n") == 2


def test_safe_print_and_emit_are_not_counted():
    assert _count("safe_print(1)\nemit(log, 'x')\n") == 0


def test_attribute_print_is_not_counted():
    # ``console.print(...)`` / ``sys.stdout.print`` are not bare calls.
    assert _count("console.print(1)\n") == 0


# ---------------------------------------------------------------------------
# The real tree
# ---------------------------------------------------------------------------

def test_baseline_is_frozen_on_the_real_tree():
    current = ratchet.scan()
    baseline = json.loads(ratchet.BASELINE.read_text(encoding="utf-8"))
    for path, n in current.items():
        assert n <= baseline.get(path, 0), f"{path} grew: {baseline.get(path, 0)} -> {n}"
