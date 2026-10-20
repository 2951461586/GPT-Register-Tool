"""Promotion is probed right after each account is saved, not at batch end.

A fresh access token lives only minutes (2026-10-11: 12/12 alive at 1 min,
2/12 at 15 min), so a batch-end probe reported early accounts as ``AT失效``.
"""

import time
from dataclasses import replace

from sms_tool.commands.registration import (
    merge_promotion_reports,
    persist_registration_result,
    save_registration_results,
)
from tests.registration.test_registration_hot_persistence import _args, _context, _result, _runtime_cfg


def _report(email, ok=True, trial=False):
    return {
        "ok": ok,
        "total": 1,
        "success": 1 if ok else 0,
        "failed": 0 if ok else 1,
        "trial_eligible": 1 if trial else 0,
        "results": [{"email": email, "ok": ok}],
    }


def _ctx(tmp_path, events, *, deferred=()):
    cfg = _runtime_cfg(tmp_path / "accounts.sqlite3")

    def upsert(data, *, json_path):
        events.append(("save", data["email"]))
        return bool(json_path)

    def check(emails, **kwargs):
        events.append(("probe", tuple(emails), kwargs))
        return _report(emails[0], trial=emails[0].startswith("t"))

    return replace(_context(cfg, upsert), check_registered_promotions=check)


def _armed(**extra):
    args = _args()
    args.check_promotion_after_registration = True
    for key, value in extra.items():
        setattr(args, key, value)
    return args


def test_probe_runs_between_this_save_and_the_next_registration(tmp_path):
    events = []
    ctx = _ctx(tmp_path, events)
    args = _armed()

    persist_registration_result(args, _result("a@example.com"), tmp_path, ctx)
    persist_registration_result(args, _result("b@example.com"), tmp_path, ctx)

    assert [(kind, who) for kind, who, *_ in events] == [
        ("save", "a@example.com"),
        ("probe", ("a@example.com",)),
        ("save", "b@example.com"),
        ("probe", ("b@example.com",)),
    ]
    kwargs = events[1][2]
    assert kwargs["workers"] == 1
    assert kwargs["proxy"] is None


def test_probe_is_off_without_the_flag(tmp_path):
    events = []
    ctx = _ctx(tmp_path, events)
    persist_registration_result(_args(), _result("a@example.com"), tmp_path, ctx)
    assert [kind for kind, *_ in events] == ["save"]


def test_batch_end_does_not_reprobe_accounts_already_checked(tmp_path):
    events = []
    ctx = _ctx(tmp_path, events)
    args = _armed()
    results = [_result("a@example.com"), _result("tb@example.com")]
    for item in results:
        persist_registration_result(args, item, tmp_path, ctx)
    events.clear()

    report = save_registration_results(
        args,
        results,
        effective_count=2,
        base_dir=tmp_path,
        pipeline_started=time.time() - 1,
        mailbox_seconds=0,
        register_seconds=1,
        ctx=ctx,
    )

    assert [kind for kind, *_ in events] == []
    assert report["promotion"]["total"] == 2
    assert report["promotion"]["success"] == 2
    assert report["promotion"]["trial_eligible"] == 1
    assert report["health"]["promotion_completed"] is True


def test_batch_end_probes_only_accounts_without_an_immediate_result(tmp_path):
    events = []
    ctx = _ctx(tmp_path, events)
    args = _armed()
    done = _result("a@example.com")
    persist_registration_result(args, done, tmp_path, ctx)
    # Replayed/persisted-by-another-process style result: no promotion marker.
    late = _result("late@example.com")
    args_off = _args()
    persist_registration_result(args_off, late, tmp_path, ctx)
    events.clear()

    report = save_registration_results(
        args,
        [done, late],
        effective_count=2,
        base_dir=tmp_path,
        pipeline_started=time.time() - 1,
        mailbox_seconds=0,
        register_seconds=1,
        ctx=ctx,
    )

    probes = [event for event in events if event[0] == "probe"]
    assert [event[1] for event in probes] == [("late@example.com",)]
    assert report["promotion"]["total"] == 2


def test_a_raising_probe_never_breaks_persistence_and_falls_back_to_batch_end(tmp_path):
    events = []
    ctx = _ctx(tmp_path, events)

    def boom(emails, **kwargs):
        raise RuntimeError("probe exploded")

    broken = replace(ctx, check_registered_promotions=boom)
    args = _armed()
    data = _result("a@example.com")
    outcome = persist_registration_result(args, data, tmp_path, broken)

    assert outcome["status"] == "complete"
    assert outcome["db_saved"] == 1
    assert outcome["promotion_report"] is None

    events.clear()
    report = save_registration_results(
        args,
        [data],
        effective_count=1,
        base_dir=tmp_path,
        pipeline_started=time.time() - 1,
        mailbox_seconds=0,
        register_seconds=1,
        ctx=ctx,
    )
    assert [event[1] for event in events if event[0] == "probe"] == [("a@example.com",)]
    assert report["promotion"]["total"] == 1


def test_deferred_at_probe_accounts_are_not_probed_immediately(tmp_path):
    events = []
    ctx = _ctx(tmp_path, events)
    data = _result("a@example.com")
    data["registration_state"] = "at_probe_pending"
    outcome = persist_registration_result(_armed(), data, tmp_path, ctx)
    assert not outcome.get("promotion_checked")
    assert [kind for kind, *_ in events if kind == "probe"] == []


def test_merge_promotion_reports_sums_and_marks_unmeasured():
    assert merge_promotion_reports([]) is None
    assert merge_promotion_reports([None]) is None
    merged = merge_promotion_reports([_report("a@x", trial=True), _report("b@x", ok=False), None])
    assert merged["total"] == 2 and merged["success"] == 1 and merged["failed"] == 1
    assert merged["trial_eligible"] == 1
    assert merged["ok"] is False
    assert [item["email"] for item in merged["results"]] == ["a@x", "b@x"]
