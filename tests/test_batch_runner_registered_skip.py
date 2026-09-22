"""Skipping mailboxes that already have a registered account.

Measured 2026-09-14 on this install: ``mailbox_tokens.txt`` held 791 addresses
and 751 of them (94.9%) were already ``status='registered'`` in
``accounts.sqlite3``.  Signing one of those up again cannot succeed -- the
server answers ``user_already_exists`` -- but the signup lane only finds that
out *after* spending an email OTP on the way to ``/about-you``.

These tests pin the skip and, just as importantly, the ways it must **not**
fire: a non-``registered`` row, an unreadable database, and the off switch.
"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from sms_tool import batch_runner
from sms_tool.batch_runner import _drop_already_registered


class _Mailbox:
    def __init__(self, email):
        self.email = email


def _pool(*emails):
    return [_Mailbox(email) for email in emails]


class _Records:
    """Patch **both** account lookups ``_drop_already_registered`` performs.

    ``batch_runner`` binds both symbols at import time, so patching the
    compatibility shell (``sms_tool.storage``) would have no effect.

    The alias-base lookup is patched too, and *defaults to empty*.  Left alone it
    would call the real ``list_account_records()`` and read the developer's
    ``runtime/accounts.sqlite3`` -- the same machine-state dependency
    ``_EmptyGuard`` exists to avoid, except invisible, because none of the
    original cases use a ``+tag`` address and so never observed the reorder it
    could cause.

    Entering yields the ``get_account_records`` mock, so the existing
    ``with _records(...) as lookup:`` / ``lookup.assert_not_called()`` form keeps
    working.
    """

    def __init__(self, mapping, bases=(), base_error=None, base_lookup=None):
        self._lookup = Mock(return_value=mapping)
        if base_lookup is not None:
            self._bases = base_lookup
        elif base_error is not None:
            self._bases = Mock(side_effect=base_error)
        else:
            self._bases = Mock(return_value=[{"email": email} for email in bases])
        self._patches = [
            patch("sms_tool.batch_runner.get_account_records", self._lookup),
            patch("sms_tool.batch_runner.list_account_records", self._bases),
        ]

    def __enter__(self):
        for item in self._patches:
            item.start()
        return self._lookup

    def __exit__(self, *exc_info):
        for item in reversed(self._patches):
            item.stop()
        return False


def _records(mapping, bases=()):
    return _Records(mapping, bases)


class _EmptyGuard:
    """Default stand-in: no cooldown, no dead end, never touches disk.

    Without this the filter would read the real
    ``runtime/registration_retry_guard.json``, so a leftover dead-end entry on
    the developer's machine could make these tests pass or fail by accident.
    """

    def __init__(self, *_args, **_kwargs):
        pass

    def check(self, _email):
        return {"deferred": False, "dead_end": False, "consecutive": 0, "remaining_seconds": 0}

    def dead_end_emails(self):
        return set()

    def blocked_email_states(self):
        return {}

    def record(self, *_args, **_kwargs):
        pass

    def mark_dead_end(self, *_args, **_kwargs):
        pass


@pytest.fixture(autouse=True)
def _isolate_retry_guard(monkeypatch):
    monkeypatch.setattr(batch_runner, "RegistrationRetryGuard", _EmptyGuard)
    monkeypatch.setattr(batch_runner, "get_registration_checkpoints", lambda _emails: {})


def _guard_with(dead_emails):
    """A guard that reports exactly ``dead_emails`` as dead ends.

    Both entry points are covered: ``dead_end_emails()`` (the pool filter) and
    ``check()`` (the per-account last line of defence).
    """
    dead = {str(email).strip().casefold() for email in dead_emails}

    class _Guard(_EmptyGuard):
        def dead_end_emails(self):
            return set(dead)

        def check(self, email):
            hit = str(email or "").strip().casefold() in dead
            return {
                "deferred": hit,
                "dead_end": hit,
                "dead_end_reason": "user_already_exists" if hit else "",
                "consecutive": 0,
                "remaining_seconds": 0,
            }

        def blocked_email_states(self):
            return {email: "dead_end" for email in dead}

    return patch.object(batch_runner, "RegistrationRetryGuard", _Guard)


def _drop(pool):
    """Only the two legacy outputs -- the dead-end list has its own tests."""
    kept, skipped, _ = _drop_already_registered(pool)
    return kept, skipped


def _drop_all(pool):
    return _drop_already_registered(pool)


# --------------------------------------------------------------------------
# the predicate
# --------------------------------------------------------------------------

def test_a_registered_mailbox_is_dropped_and_the_rest_kept():
    with _records({"used@example.com": {"status": "registered"}}):
        kept, skipped = _drop(_pool("used@example.com", "fresh@example.com"))

    assert [m.email for m in kept] == ["fresh@example.com"]
    assert skipped == ["used@example.com"]


def test_a_status_other_than_registered_is_not_dropped():
    """A half-finished row must stay retryable, or the skip becomes a data loss."""
    with _records({"half@example.com": {"status": "failed"}}):
        kept, skipped = _drop(_pool("half@example.com"))

    assert [m.email for m in kept] == ["half@example.com"]
    assert skipped == []


def test_case_and_surrounding_space_do_not_hide_a_registered_row():
    """The lookup key is normalised, so the skip must still fire.

    ``skipped`` reports the stripped email as written by the pool, not the
    lower-cased lookup key -- the preview line has to stay recognisable.
    """
    with _records({"used@example.com": {"status": "Registered"}}):
        kept, skipped = _drop(_pool("  USED@Example.com  "))

    assert kept == []
    assert skipped == ["USED@Example.com"]


def test_a_lookup_failure_keeps_every_mailbox():
    """An unreadable account database must not silently empty a batch."""
    pool = _pool("a@example.com", "b@example.com")
    with patch("sms_tool.batch_runner.get_account_records", side_effect=RuntimeError("db is gone")):
        kept, skipped = _drop(pool)

    assert kept == pool
    assert skipped == []


def test_an_empty_pool_is_returned_untouched():
    with _records({}) as lookup:
        kept, skipped = _drop([])

    assert kept == [] and skipped == []
    lookup.assert_not_called()


def test_the_switch_turns_the_skip_off():
    pool = _pool("used@example.com")
    with (
        patch.object(batch_runner, "CFG", {"registration": {"skip_registered_mailboxes": False}}),
        _records({"used@example.com": {"status": "registered"}}) as lookup,
    ):
        kept, skipped = _drop(pool)

    assert kept == pool and skipped == []
    lookup.assert_not_called()


# --------------------------------------------------------------------------
# end to end: the signup lane must not be reached
# --------------------------------------------------------------------------

def test_a_fully_registered_pool_never_reaches_the_signup_lane():
    calls = []
    pool = _pool("a@example.com", "b@example.com")
    with _records({"a@example.com": {"status": "registered"}, "b@example.com": {"status": "registered"}}):
        results = batch_runner.run_batch_impl(
            count=2,
            mailboxes=pool,
            workers=1,
            max_attempts=1,
            run_email_func=lambda **kwargs: calls.append(kwargs) or {"success": True},
        )

    assert results == []
    assert calls == [], "a registered address reached run_email_func and would have burned an OTP"


def test_only_the_fresh_mailbox_reaches_the_signup_lane():
    seen = []
    pool = _pool("used@example.com", "fresh@example.com")
    with _records({"used@example.com": {"status": "registered"}}):
        results = batch_runner.run_batch_impl(
            count=2,
            mailboxes=pool,
            workers=1,
            max_attempts=1,
            run_email_func=lambda **kwargs: seen.append(kwargs["mailbox"].email) or {"success": True},
        )

    assert seen == ["fresh@example.com"]
    assert len(results) == 1


# --------------------------------------------------------------------------
# the command layer filters first; the inner filter must stay quiet
# --------------------------------------------------------------------------

def test_the_public_filter_reports_once_and_is_idempotent(capsys):
    """The command layer calls this before sizing or billing the batch."""
    pool = _pool("used@example.com", "fresh@example.com")
    with _records({"used@example.com": {"status": "registered"}}):
        kept = batch_runner.filter_registered_mailboxes(pool)
        first = capsys.readouterr().out
        batch_runner.filter_registered_mailboxes(kept)
        second = capsys.readouterr().out

    assert [m.email for m in kept] == ["fresh@example.com"]
    assert "Skipped 1 mailbox" in first
    assert second == "", f"the second pass reported again: {second!r}"


def test_run_batch_impl_stays_quiet_after_the_caller_filtered(capsys):
    """Double filtering must not double-report (the command layer already did)."""
    pool = _pool("used@example.com", "fresh@example.com")
    with _records({"used@example.com": {"status": "registered"}}):
        kept = batch_runner.filter_registered_mailboxes(pool)
        capsys.readouterr()
        results = batch_runner.run_batch_impl(
            count=1,
            mailboxes=kept,
            workers=1,
            max_attempts=1,
            run_email_func=lambda **kwargs: {"success": True},
        )
        tail = capsys.readouterr().out

    assert len(results) == 1
    assert "already have a registered account" not in tail


# --------------------------------------------------------------------------
# wiring guards: the filter must run *before* the batch is sized or billed
# --------------------------------------------------------------------------

def _source(relative):
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")


def test_the_main_cli_filters_before_effective_count_is_computed():
    """``effective_count`` is the report denominator, so skipped mailboxes must not inflate it.

    Filtering only inside ``run_batch_impl`` (which also happens, as a last line
    of defence) leaves the denominator at the pre-filter size and reports every
    skipped mailbox as a failure.
    """
    src = _source("sms_tool/cli.py")
    assert "filter_registered_mailboxes(mailboxes)" in src
    assert src.index("filter_registered_mailboxes(mailboxes)") < src.index("effective_count = requested_count")


def test_the_target_at200_loop_filters_before_counting_purchases():
    """``purchased``/``spent`` must not grow for mailboxes that are never attempted.

    Without this, a pool that is entirely registered lets the replenishment loop
    re-load the same list every round: ``active`` never grows, so the loop keeps
    going until ``max_purchases``, and ``spent`` keeps accumulating.
    """
    src = _source("sms_tool/commands/registration.py")
    assert "filter_registered_mailboxes(loaded_mailboxes)" in src
    assert src.index("filter_registered_mailboxes(loaded_mailboxes)") < src.index("purchased += len(mailboxes)")


# --------------------------------------------------------------------------
# dead ends: the server said "already registered" and we hold no credentials
# --------------------------------------------------------------------------

def test_a_dead_end_mailbox_is_skipped_without_an_account_row():
    """The row is *absent* from ``accounts.sqlite3`` -- that is the whole point.

    A ``user_already_exists`` address we never stored credentials for cannot be
    found by the database lookup, so the retry guard is the only memory of it.
    """
    with _records({}), _guard_with(["dead@example.com"]):
        kept, skipped, skipped_dead = _drop_all(_pool("dead@example.com", "fresh@example.com"))

    assert [m.email for m in kept] == ["fresh@example.com"]
    assert skipped == []
    assert skipped_dead == ["dead@example.com"]


def test_a_fresh_mailbox_is_never_treated_as_a_dead_end():
    """The inverse guard: a wrong-keyed set would silently empty a whole pool."""
    with _records({}), _guard_with(["other@example.com"]):
        kept, skipped, skipped_dead = _drop_all(_pool("fresh@example.com"))

    assert [m.email for m in kept] == ["fresh@example.com"]
    assert skipped == [] and skipped_dead == []


def test_dead_end_matching_is_case_and_space_insensitive():
    with _records({}), _guard_with(["DEAD@Example.com"]):
        kept, _skipped, skipped_dead = _drop_all(_pool("  dead@example.com  "))

    assert kept == []
    assert skipped_dead == ["dead@example.com"]


def test_a_dead_end_is_reported_separately_from_a_registered_row(capsys):
    """Two reasons, two lines: only one of them means "we hold the account"."""
    with _records({"used@example.com": {"status": "registered"}}), _guard_with(["dead@example.com"]):
        kept = batch_runner.filter_registered_mailboxes(
            _pool("used@example.com", "dead@example.com", "fresh@example.com")
        )
        out = capsys.readouterr().out

    assert [m.email for m in kept] == ["fresh@example.com"]
    assert "already have a registered account" in out
    assert "the server already reports as registered" in out


def test_a_dead_end_never_reaches_the_signup_lane():
    calls = []
    with _records({}), _guard_with(["dead@example.com"]):
        results = batch_runner.run_batch_impl(
            count=1,
            mailboxes=_pool("dead@example.com"),
            workers=1,
            max_attempts=1,
            run_email_func=lambda **kwargs: calls.append(kwargs) or {"success": True},
        )

    assert results == []
    assert calls == [], "a known dead end reached run_email_func and would have burned an OTP"


def test_expired_recovery_checkpoint_is_filtered_before_batch_sizing(monkeypatch, capsys):
    checkpoint = {
        "state": "auth_session_pending",
        "payload": {
            "registration_state": "auth_session_pending",
            "create_ok": True,
            "session_recovery_started_at": 1,
            "session_recovery_attempts": 0,
            "session_cookies": [
                {
                    "name": "fixture",
                    "value": "synthetic",
                    "domain": "auth.example.test",
                }
            ],
        },
    }
    monkeypatch.setattr(
        batch_runner,
        "get_registration_checkpoints",
        lambda emails: {"stale@example.com": checkpoint},
    )
    with _records({}):
        kept = batch_runner.filter_registered_mailboxes(
            _pool("stale@example.com", "fresh@example.com")
        )

    assert [item.email for item in kept] == ["fresh@example.com"]
    assert "unrecoverable registration state" in capsys.readouterr().out


def test_valid_recovery_checkpoint_remains_eligible(monkeypatch):
    import time

    checkpoint = {
        "state": "auth_session_pending",
        "payload": {
            "registration_state": "auth_session_pending",
            "create_ok": True,
            "session_recovery_started_at": int(time.time()),
            "session_recovery_attempts": 0,
            "session_cookies": [
                {
                    "name": "fixture",
                    "value": "synthetic",
                    "domain": "auth.example.test",
                }
            ],
        },
    }
    monkeypatch.setattr(
        batch_runner,
        "get_registration_checkpoints",
        lambda emails: {"resume@example.com": checkpoint},
    )
    with _records({}):
        kept, _registered, blocked = _drop_all(_pool("resume@example.com"))

    assert [item.email for item in kept] == ["resume@example.com"]
    assert blocked == []


def test_the_inner_guard_also_stops_a_dead_end_the_filter_let_through():
    """The pool filter is the entry point, but it is not the only way in.

    ``skip_registered_mailboxes=false`` deliberately passes everything through
    the filter, so the per-account guard has to stop the dead end on its own --
    otherwise that switch would turn "try a registered address anyway" into
    "burn an OTP on a known dead end".
    """
    calls = []
    with (
        patch.object(
            batch_runner, "CFG", {"registration": {"skip_registered_mailboxes": False}}
        ),
        _records({}),
        _guard_with(["dead@example.com"]),
    ):
        results = batch_runner.run_batch_impl(
            count=1,
            mailboxes=_pool("dead@example.com"),
            workers=1,
            max_attempts=1,
            run_email_func=lambda **kwargs: calls.append(kwargs) or {"success": True},
        )

    assert calls == [], "the inner guard let a dead end reach run_email_func"
    assert len(results) == 1
    assert results[0]["error"] == "registration_dead_end"
    assert results[0]["retryable"] is False
    assert results[0]["dropped"] is True


def test_the_inner_guard_reports_otp_pending_quarantine_without_retry(monkeypatch):
    class _QuarantinedGuard(_EmptyGuard):
        def check(self, _email):
            return {
                "deferred": True,
                "dead_end": False,
                "quarantined": True,
                "quarantine_reason": "email_otp_send_stuck",
                "remaining_seconds": 0,
            }

    monkeypatch.setattr(batch_runner, "RegistrationRetryGuard", _QuarantinedGuard)
    with (
        patch.object(
            batch_runner, "CFG", {"registration": {"skip_registered_mailboxes": False}}
        ),
        _records({}),
    ):
        results = batch_runner.run_batch_impl(
            count=1,
            mailboxes=_pool("pending@example.com"),
            workers=1,
            run_email_func=lambda **_kwargs: pytest.fail("quarantined mailbox ran"),
        )

    assert results[0]["error"] == "registration_otp_pending_quarantined"
    assert results[0]["retryable"] is False
    assert results[0]["future_batch_eligible"] is False
    assert results[0]["retry_disposition"] == "otp_pending_quarantine"


# --------------------------------------------------------------------------
# alias/base conflicts: reordered, never skipped
# --------------------------------------------------------------------------

def test_an_alias_over_an_occupied_base_is_deferred_not_skipped():
    """Measured 2026-09-15: 292 of that day's 305 ``user_already_exists`` were
    aliases whose base already held an account.

    Strong enough to reorder on -- *not* strong enough to skip on: 99 aliases on
    this install did register against an already-occupied base, so dropping them
    would lose real signups.  The mailbox stays in the batch, behind the fresh
    ones.
    """
    with _records({}, bases=["taken@example.com"]):
        kept, skipped, skipped_dead = _drop_all(
            _pool("taken+oai01@example.com", "fresh@example.com")
        )

    assert [m.email for m in kept] == ["fresh@example.com", "taken+oai01@example.com"]
    assert skipped == [] and skipped_dead == []


def test_a_plain_address_over_an_occupied_base_is_not_deferred():
    """``base == address`` here, so it cannot collide with *itself*.

    Only an alias can conflict with its own base row; a plain address that
    already holds one is caught by the ``registered`` branch instead.  Drop the
    ``base != normalized`` guard and this case silently reorders against nothing.
    """
    with _records({}, bases=["taken@example.com"]):
        kept, _skipped, _dead = _drop_all(_pool("taken@example.com", "fresh@example.com"))

    assert [m.email for m in kept] == ["taken@example.com", "fresh@example.com"]


def test_deferred_mailboxes_keep_their_relative_order():
    """Both halves of the split are stable; only the split point moves."""
    with _records({}, bases=["b@example.com"]):
        kept, _s, _d = _drop_all(
            _pool(
                "b+oai01@example.com",
                "f1@example.com",
                "b+oai02@example.com",
                "f2@example.com",
            )
        )

    assert [m.email for m in kept] == [
        "f1@example.com",
        "f2@example.com",
        "b+oai01@example.com",
        "b+oai02@example.com",
    ]


def test_the_alias_base_is_case_and_space_insensitive():
    """``+oaiNN`` is appended to the local part, so both sides fold to lower."""
    with _records({}, bases=["Taken@Example.com"]):
        kept, _s, _d = _drop_all(_pool("TAKEN+oai01@example.com", "fresh@example.com"))

    assert [m.email for m in kept] == ["fresh@example.com", "TAKEN+oai01@example.com"]


def test_the_deprioritize_switch_keeps_the_callers_order():
    with (
        patch.object(
            batch_runner,
            "CFG",
            {"registration": {"deprioritize_base_conflicts": False}},
        ),
        _records({}, bases=["taken@example.com"]),
    ):
        kept, _s, _d = _drop_all(_pool("taken+oai01@example.com", "fresh@example.com"))

    assert [m.email for m in kept] == ["taken+oai01@example.com", "fresh@example.com"]


def test_an_unreadable_account_database_does_not_reorder_the_batch():
    """A failure must degrade to "no known bases", not to a shuffle."""
    with _Records({}, base_error=RuntimeError("database is locked")):
        kept, _s, _d = _drop_all(_pool("taken+oai01@example.com", "fresh@example.com"))

    assert [m.email for m in kept] == ["taken+oai01@example.com", "fresh@example.com"]


def test_a_base_registered_between_two_calls_defers_the_alias_on_the_second():
    """Pins the cross-batch self-heal -- which is why no in-run bookkeeping exists.

    ``_drop_already_registered`` runs once per ``run_batch_impl`` and re-reads
    the account rows every time (``store/accounts.py`` caches nothing), so a
    variant that lands in round N is already visible to round N+1.  A cached or
    progress-log-derived base set would have to be maintained by hand; the
    database is the memory.
    """
    fresh_then_taken = Mock(side_effect=[[], [{"email": "base+oai01@example.com"}]])
    pool = _pool("base+oai02@example.com", "fresh@example.com")
    with _Records({}, base_lookup=fresh_then_taken):
        kept_first, _s1, _d1 = _drop_all(pool)
        kept_second, _s2, _d2 = _drop_all(pool)

    assert [m.email for m in kept_first] == ["base+oai02@example.com", "fresh@example.com"]
    assert [m.email for m in kept_second] == ["fresh@example.com", "base+oai02@example.com"]


def test_a_deferred_alias_is_not_announced_as_skipped(capsys):
    """Deferred mailboxes still run, so no skip line may claim otherwise."""
    with _records({}, bases=["taken@example.com"]):
        kept = batch_runner.filter_registered_mailboxes(
            _pool("taken+oai01@example.com", "fresh@example.com")
        )
        out = capsys.readouterr().out

    assert len(kept) == 2
    assert "Skipped" not in out


@pytest.mark.parametrize(
    "address, expected",
    [
        ("local+oai01@example.com", "local@example.com"),
        ("LOCAL+oai01@Example.COM", "local@example.com"),
        ("local@example.com", "local@example.com"),
        ("local+@example.com", "local@example.com"),
        ("local+oai01@sub.example.com", "local@sub.example.com"),
        ("no-at-sign", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_alias_base_normalization(address, expected):
    assert batch_runner._alias_base_email(address) == expected


def test_alias_base_normalization_never_merges_a_tag_into_the_domain():
    """The ``+`` split must not touch the domain side.

    ``local@ex+ample.com`` is a legal address whose *domain* contains a plus; a
    naive ``split("+")`` would fold it onto ``local@ex`` and reorder it against
    an unrelated base.
    """
    assert batch_runner._alias_base_email("local@ex+ample.com") == "local@ex+ample.com"
