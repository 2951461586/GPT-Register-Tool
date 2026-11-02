"""The registration batch summary is one pure function with a frozen key set.

Why this exists
---------------
``save_registration_results`` used to end with a 20-line dict literal, so the
payload the desktop host reads was the tail of a 90-line procedure: there was no
single place to point at when asking "what keys does a batch report carry?".

``build_registration_batch_summary`` is now that place, and this file pins two
properties:

* the exact key set (C# reads these keys by name and defaults every missing one
  to ``0``, so a rename degrades silently instead of failing loudly);
* that ``save_registration_results`` actually delegates to it, rather than
  growing a second copy of the literal back (the same "called exactly once"
  shape ``tests/registration/test_registration_result_contract.py`` uses).

The manifest-driven cross-language check is ``result_schema.json`` +
``scripts/result_schema_check.py``; the expected key set below is **read from
that manifest** rather than hand-listed, so adding a key means editing one file
and the two guards cannot disagree.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from sms_tool.commands.registration import build_registration_batch_summary

ROOT = Path(__file__).resolve().parents[2]
REGISTRATION_COMMAND = ROOT / "sms_tool" / "commands" / "registration.py"
MANIFEST_PATH = ROOT / "result_schema.json"

#: Read by ``SmsWorkbench.Interop.BackendResultInterpreter.BatchSummaryLabel``
#: and by the per-account result dialog.  Additive changes only -- update
#: ``result_schema.json``, this file follows.
EXPECTED_KEYS = frozenset(
    json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["families"]["registration.batch"]["keys"]
)


def _summary(**overrides):
    kwargs = {
        "effective_count": 3,
        "success_count": 2,
        "batch_id": "batch-1",
        "saved_count": 2,
        "db_saved_count": 3,
        "quality": None,
        "promotion_report": None,
        "health_job_ids": ["job-a", "job-a", "job-b"],
        "results": [
            {"success": True, "email": "a@example.com"},
            {"success": True, "email": "b@example.com"},
            {"success": False, "email": "c@example.com", "failure_class": "otp"},
        ],
    }
    kwargs.update(overrides)
    return build_registration_batch_summary(**kwargs)


def test_the_key_set_is_frozen():
    assert set(_summary()) == EXPECTED_KEYS


def test_ok_means_every_attempted_mailbox_succeeded():
    assert _summary(effective_count=2, success_count=2)["ok"] is True
    assert _summary(effective_count=3, success_count=2)["ok"] is False


def test_failed_is_clamped_and_derived_from_the_attempt_count():
    summary = _summary(effective_count=1, success_count=3)
    assert summary["failed"] == 0
    assert summary["total"] == 1


def test_health_counts_distinct_job_ids_not_duplicates():
    assert _summary()["health"] == {
        "queued": 2,
        "promotion_completed": False,
        "promotion_ok": None,
    }


def test_promotion_ok_only_reads_a_real_report():
    report = {"ok": True, "total": 2, "success": 2, "trial_eligible": 1}
    health = _summary(promotion_report=report)["health"]
    assert health["promotion_completed"] is True
    assert health["promotion_ok"] is True


def test_funnel_is_derived_from_the_same_results_and_attempt_count():
    funnel = _summary()["funnel"]
    assert funnel["attempted"] == 3
    assert funnel["registered"] == 2


def test_the_builder_is_pure_and_does_not_mutate_its_inputs():
    results = [
        {"success": True, "email": "a@example.com"},
        {"success": False, "email": "b@example.com", "failure_class": "otp"},
    ]
    health_job_ids = ["job-a", "job-a"]
    before = [dict(row) for row in results]

    build_registration_batch_summary(
        effective_count=2,
        success_count=1,
        batch_id="batch-1",
        saved_count=1,
        db_saved_count=1,
        quality=None,
        promotion_report=None,
        health_job_ids=health_job_ids,
        results=results,
    )

    assert results == before
    assert health_job_ids == ["job-a", "job-a"]


def _save_registration_results_body(source: str) -> str:
    """Source text of the procedure, or ``""`` when it is missing."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "save_registration_results":
            return ast.get_source_segment(source, node) or ""
    return ""


def test_save_registration_results_delegates_instead_of_re_inlining_the_dict():
    """One payload, one literal: the procedure must not grow a second copy."""
    body = _save_registration_results_body(REGISTRATION_COMMAND.read_text(encoding="utf-8-sig"))

    assert "build_registration_batch_summary(" in body
    assert body.count("build_registration_batch_summary(") == 1
    # The literal (or any hand-built copy of it) must not come back: these keys
    # are the ones a second copy would re-declare.
    for re_inlined in ('"session_saved": saved_count', '"db_saved": db_saved_count', '"batch_id": batch_id'):
        assert re_inlined not in body
