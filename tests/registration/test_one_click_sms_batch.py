"""Offline batch outcomes for the one-click SMS command adapter."""

import json
import threading
import time
from argparse import Namespace
from unittest.mock import patch

import pytest

from sms_tool.commands import one_click


class FakePhonePool:
    phones = [object()]
    total_capacity = 1

    def reset_exhausted_slots(self):
        pass


def args_for(tmp_path, emails, workers):
    targets = tmp_path / "targets.txt"
    targets.write_text("\n".join(emails), encoding="utf-8")
    return Namespace(
        email_file=str(targets), email=None, session_file=None,
        chatai_mailbox_file=None, mailbox_file=None, workers=workers,
        phone_send_cooldown=None, phone_source="smsbower", proxy=None,
        refresh_timeout=60,
    )


def context():
    return one_click.OneClickCommandContext(
        load_mailbox_pool=lambda args: [],
        max_reuse=lambda args: 1,
        mailbox_snapshot=lambda mailbox: {},
        persist_failure=lambda data, path, email, result: {"persisted": True},
    )


def run_offline(args, ctx, *, seed, refresh, capsys, monkeypatch):
    events = []
    monkeypatch.setattr(one_click, "_emit_one_click_event", lambda **event: events.append(event))
    with (
        patch("sms_tool.phone_reuse.create_phone_pool", return_value=FakePhonePool()),
        patch("sms_tool.phone_reuse.print_phone_pool_status"),
        patch("sms_tool.session_refresh._load_seed_session", side_effect=seed),
        patch("sms_tool.codex_oauth.refresh_codex_oauth_session", side_effect=refresh),
        pytest.raises(SystemExit) as exit_info,
    ):
        one_click.one_click_sms(args, ctx)
    assert exit_info.value.code == 3
    summary = json.loads("{" + capsys.readouterr().out.rsplit("\n{", 1)[1])
    return summary, events


def test_serial_seed_failure_stops_before_next_rental_and_reports_skips(tmp_path, capsys, monkeypatch):
    emails = ["a@example.test", "b@example.test", "c@example.test"]
    called = []

    def seed(*, email, session_file):
        called.append(email)
        raise OSError("session unavailable")

    summary, events = run_offline(
        args_for(tmp_path, emails, 1), context(),
        seed=seed, refresh=lambda *a, **kw: {"ok": True},
        capsys=capsys, monkeypatch=monkeypatch,
    )
    assert called == emails[:1]
    assert (summary["total"], summary["success"], summary["failed"], summary["unknown"], summary["skipped"]) == (3, 0, 0, 1, 2)
    assert len(summary["results"]) == 3
    assert [row["batch_status"] for row in summary["results"]] == ["unknown", "skipped", "skipped"]
    assert len([event for event in events if event["stage"] == "batch_completed"]) == 1
    assert all("session unavailable" not in json.dumps(event) for event in events)


def test_parallel_exception_drains_started_workers_without_starting_third(tmp_path, capsys, monkeypatch):
    emails = ["a@example.test", "b@example.test", "c@example.test"]
    started = []
    other_started = threading.Event()

    def refresh(data, **kwargs):
        email = data["email"]
        started.append(email)
        if email == emails[0]:
            other_started.wait(timeout=2)
            raise RuntimeError("unexpected worker failure")
        if email == emails[1]:
            other_started.set()
            time.sleep(0.05)
        return {"ok": True, "persisted": True, "refresh_token_status": "updated"}

    summary, events = run_offline(
        args_for(tmp_path, emails, 2), context(),
        seed=lambda *, email, session_file: ({"email": email}, ""),
        refresh=refresh, capsys=capsys, monkeypatch=monkeypatch,
    )
    assert set(started) == set(emails[:2])
    assert (summary["total"], summary["success"], summary["failed"], summary["unknown"], summary["skipped"]) == (3, 1, 0, 1, 1)
    assert len([event for event in events if event.get("account_terminal")]) == 3
    assert len([event for event in events if event["stage"] == "batch_completed"]) == 1


def test_successful_remote_refresh_with_failed_save_fails_batch(tmp_path, capsys, monkeypatch):
    summary, events = run_offline(
        args_for(tmp_path, ["a@example.test"], 1), context(),
        seed=lambda *, email, session_file: ({"email": email}, ""),
        refresh=lambda data, **kw: {
            "ok": True, "persisted": False,
            "persistence": {"session_saved": True, "account_saved": False, "persisted": False, "error_code": "account_write_failed"},
        },
        capsys=capsys, monkeypatch=monkeypatch,
    )
    assert summary["ok"] is False
    assert (summary["success"], summary["failed"], summary["persist_failed"]) == (1, 0, 1)
    assert summary["results"][0]["ok"] is True
    assert any(event["stage"] == "failed" and event["failure_class"] == "persistence" for event in events)


def test_known_remote_failure_keeps_failed_count_separate_from_save_failure(tmp_path, capsys, monkeypatch):
    summary, _ = run_offline(
        args_for(tmp_path, ["a@example.test"], 1),
        one_click.OneClickCommandContext(
            load_mailbox_pool=lambda args: [], max_reuse=lambda args: 1,
            mailbox_snapshot=lambda mailbox: {},
            persist_failure=lambda *args: {"persisted": False, "error_code": "session_write_failed"},
        ),
        seed=lambda *, email, session_file: ({"email": email}, ""),
        refresh=lambda data, **kw: {"ok": False, "error": "phone_sms_timeout"},
        capsys=capsys, monkeypatch=monkeypatch,
    )
    assert (summary["success"], summary["failed"], summary["unknown"], summary["skipped"], summary["persist_failed"]) == (0, 1, 0, 0, 1)
    assert summary["results"][0]["persistence"]["error_code"] == "session_write_failed"
