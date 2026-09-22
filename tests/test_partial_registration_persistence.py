"""``partial_registered`` must reach the ``accounts`` table, not just the audit log.

Measured 2026-09-15: ``registration_audit`` held 330 rows naming
``existing_account_user_already_exists`` while ``accounts`` held **zero**
``partial_registered`` rows.  The UI's "半注册" guards
(``MainWindow.Register.cs`` / ``MainWindow.Pools.cs``) read the *accounts*
table, so the guard written to stop those addresses from being re-registered
never fired -- and every batch re-selected them as "unregistered mailboxes" and
spent another email code.

The mechanism was an early return: ``if not success and not deferred_probe:
return`` ran before the upsert, and a partial registration has ``success=False``
*and* no access token, so it never got past it.

These tests pin the partial branch and, just as importantly, that the branch did
not turn ordinary failures into writes.
"""

from __future__ import annotations

import types

import pytest

import sms_tool.storage as storage_mod
from sms_tool.commands.registration import _PERSISTENCE_KEY, _persist_registration_result_core


@pytest.fixture(autouse=True)
def _storage_audit(monkeypatch):
    """Capture the audit rows without opening the real database.

    ``_persist_registration_result_core`` imports ``record_registration_audit``
    from ``sms_tool.storage`` at call time -- not from ``ctx`` -- so leaving it
    alone makes the real ``init_database`` run and every case dies on
    ``ConfigError`` before reaching the upsert.
    """
    records: list[dict] = []

    def record(data, **kwargs):
        records.append({"data": dict(data), **kwargs})
        return True

    monkeypatch.setattr(storage_mod, "record_registration_audit", record)
    return records


def _ctx(*, access_token="", upsert_result=True):
    """A context whose ``build_session_file`` mirrors the real one for this case.

    The real builder carries ``registration_state`` through and does **not**
    invent an access token, which is what makes a partial registration partial.
    """
    calls: list[tuple[dict, object]] = []

    def build_session_file(data):
        return {
            "email": data.get("email") or "unknown",
            "access_token": data.get("access_token") or access_token,
            "registration_state": data.get("registration_state") or "",
        }

    def upsert_account(session_data, json_path=None):
        calls.append((dict(session_data), json_path))
        return upsert_result

    return (
        types.SimpleNamespace(
            runtime_config={"runtime": {"directory": "."}, "output": {}},
            build_session_file=build_session_file,
            upsert_account=upsert_account,
            enqueue_post_registration_checks=lambda *a, **k: [],
            record_registration_audit=lambda *a, **k: None,
            check_registered_promotions=lambda *a, **k: None,
            import_registered_accounts=lambda *a, **k: None,
        ),
        calls,
    )


def _args(batch_id="b1"):
    return types.SimpleNamespace(registration_batch_id=batch_id)


def _partial(email="half@example.test"):
    return {
        "success": False,
        "email": email,
        "error": "existing_account_user_already_exists:continue_to_login",
        "registration_state": "partial_registered",
    }


# --------------------------------------------------------------------------
# the partial branch
# --------------------------------------------------------------------------

def test_a_partial_registration_is_upserted_even_without_an_access_token(tmp_path):
    ctx, calls = _ctx()

    marker = _persist_registration_result_core(_args(), _partial(), str(tmp_path), ctx)

    assert len(calls) == 1, "the account row is exactly what the UI guard reads"
    session_data, json_path = calls[0]
    assert session_data["registration_state"] == "partial_registered"
    assert json_path == "", "no session file exists to point at"
    assert marker["db_saved"] == 1
    assert marker["db_completed"] is True
    assert marker["status"] == "complete"
    assert marker["import_email"] == "half@example.test"
    assert "error_type" not in marker


def test_a_partial_row_carries_the_batch_id(tmp_path):
    """Otherwise the row cannot be grouped with the run that produced it.

    Measured 2026-09-15: all 71 ``partial_registered`` rows landed with an
    empty ``batch_id``.  ``build_session_file`` does not emit the field and the
    normal path assigns it only *after* this branch's early return, so the
    partial rows were unattributable.
    """
    ctx, calls = _ctx()

    _persist_registration_result_core(_args(batch_id="batch-42"), _partial(), str(tmp_path), ctx)

    session_data, _ = calls[0]
    assert session_data["batch_id"] == "batch-42"


def test_a_partial_row_survives_an_absent_batch_id(tmp_path):
    """An unattributed row still beats no row at all."""
    ctx, calls = _ctx()

    _persist_registration_result_core(_args(batch_id=""), _partial(), str(tmp_path), ctx)

    session_data, _ = calls[0]
    assert session_data["batch_id"] == ""
    assert session_data["registration_state"] == "partial_registered"


def test_the_partial_persist_is_idempotent_within_the_process(tmp_path):
    ctx, calls = _ctx()
    data = _partial()

    first = _persist_registration_result_core(_args(), data, str(tmp_path), ctx)
    second = _persist_registration_result_core(_args(), data, str(tmp_path), ctx)

    assert first is second
    assert len(calls) == 1, "the in-memory marker must block the second upsert"


def test_a_partial_registration_reports_the_upsert_result_honestly(tmp_path):
    ctx, calls = _ctx(upsert_result=False)

    marker = _persist_registration_result_core(_args(), _partial(), str(tmp_path), ctx)

    assert len(calls) == 1
    assert marker["db_saved"] == 0, "a refused upsert must not be reported as saved"
    assert marker["status"] == "complete"


def test_a_partial_registration_with_a_token_is_not_marked_active(tmp_path):
    """``active`` would tell the UI the account is usable; it is not."""
    ctx, calls = _ctx(access_token="tok")
    data = _partial()

    _persist_registration_result_core(_args(), data, str(tmp_path), ctx)

    assert calls[0][0]["registration_state"] == "partial_registered"


def test_the_partial_line_is_printed_once(tmp_path, capsys):
    ctx, _calls = _ctx()
    data = _partial()

    _persist_registration_result_core(_args(), data, str(tmp_path), ctx)
    _persist_registration_result_core(_args(), data, str(tmp_path), ctx)

    assert capsys.readouterr().out.count("Partial registration recorded") == 1


# --------------------------------------------------------------------------
# the branch must not swallow ordinary failures
# --------------------------------------------------------------------------

def test_an_ordinary_failure_is_still_not_written_to_the_accounts_table(tmp_path):
    """The whole point of the early return -- and it must survive the change."""
    ctx, calls = _ctx()
    data = {"success": False, "email": "fresh@example.test", "error": "create_account_failed"}

    marker = _persist_registration_result_core(_args(), data, str(tmp_path), ctx)

    assert calls == [], "a plain failure must never create an account row"
    assert marker["status"] == "complete"


def test_a_failure_that_merely_mentions_partial_in_its_error_is_not_partial(tmp_path):
    """Only ``registration_state`` selects the branch, never the error text."""
    ctx, calls = _ctx()
    data = {
        "success": False,
        "email": "fresh@example.test",
        "error": "registration_state=partial_registered was reported by the server",
    }

    _persist_registration_result_core(_args(), data, str(tmp_path), ctx)

    assert calls == []


@pytest.mark.parametrize(
    "state",
    [
        "",
        "pending",
        "failed",
        "at_probe_pending",
        "PARTIAL",
        # Boundary: these *contain* the marker.  The read side
        # (``mailbox_registration_status``) compares with ``==``, so a substring
        # match here would write a row that the guard can never recognise --
        # the exact split this change exists to close.
        "not_partial_registered",
        "partial_registered_pending",
    ],
)
def test_only_the_exact_partial_state_selects_the_branch(tmp_path, state):
    ctx, calls = _ctx()
    data = {"success": False, "email": "fresh@example.test", "error": "boom"}
    if state:
        data["registration_state"] = state

    _persist_registration_result_core(_args(), data, str(tmp_path), ctx)

    assert calls == []


def test_the_state_match_is_case_and_space_insensitive(tmp_path):
    ctx, calls = _ctx()
    data = {"success": False, "email": "half@example.test", "registration_state": "  Partial_Registered  "}

    _persist_registration_result_core(_args(), data, str(tmp_path), ctx)

    assert len(calls) == 1


def test_a_successful_registration_still_takes_the_normal_path(tmp_path):
    ctx, calls = _ctx()
    data = {"success": True, "email": "ok@example.test", "access_token": "tok"}

    marker = _persist_registration_result_core(_args(), data, str(tmp_path), ctx)

    assert calls[0][0]["registration_state"] == "active"
    assert marker["session_saved"] == 1


# --------------------------------------------------------------------------
# the audit row has to agree with the accounts row
# --------------------------------------------------------------------------

def test_the_audit_row_for_a_partial_is_not_recorded_as_a_plain_failure(tmp_path, _storage_audit):
    """Two stores, one verdict: ``state='failed'`` here while ``accounts`` holds a
    ``partial_registered`` row is what made the earlier 330-vs-0 split invisible."""
    ctx, _calls = _ctx()

    _persist_registration_result_core(_args(), _partial(), str(tmp_path), ctx)

    assert len(_storage_audit) == 1
    assert _storage_audit[0]["state"] == "partial_registered"


def test_the_audit_row_for_an_ordinary_failure_stays_failed(tmp_path, _storage_audit):
    ctx, _calls = _ctx()
    data = {"success": False, "email": "fresh@example.test", "error": "create_account_failed"}

    _persist_registration_result_core(_args(), data, str(tmp_path), ctx)

    assert [row["state"] for row in _storage_audit] == ["failed"]
