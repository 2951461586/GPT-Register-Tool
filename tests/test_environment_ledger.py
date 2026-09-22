"""Guards for the time-boxed environment ledger (``store/environment_ledger.py``).

The ledger exists because ``batch_runner`` round-robins the proxy pool, so with a
ten-entry pool and concurrency above ten the same exit is handed to two live
accounts by construction and nothing recorded it.  These tests pin three things
that are easy to get wrong and expensive to get wrong quietly:

1. **The key must separate sticky sessions.**  This install's whole pool points at
   one host, so keying on ``host:port`` would collapse ten exits into one
   identity and report a collision for every concurrent account.  The session id
   inside the username is the only thing that distinguishes them.
2. **The window must actually open.**  A lease that is released or past its TTL
   has to free its exit again -- a permanent-uniqueness ledger would refuse to
   allocate the eleventh task out of a ten-IP pool.
3. **Reuse must be recorded, and must never paper over a profile clash.**
   Sharing an exit under saturation is unavoidable here; sharing a *fingerprint*
   is not, and ``allow_reuse`` must not become a way to do it silently.
"""

from __future__ import annotations

import time
from pathlib import Path

from sms_tool.accounts.account_identity import proxy_egress_key
from sms_tool.store import (
    acquire_environment_lease,
    environment_ledger_stats,
    init_database,
    list_environment_leases,
    record_environment_observations,
    release_environment_lease,
    sweep_expired_environment_leases,
)
from sms_tool.store.constants import SCHEMA_VERSION

# Two entries of the real pool shape: one host, two sticky session ids.
POOL_A = "http://w7wgt28979-region-VN-sid-AAAAAAAA-t-5:xum33c6k@us.lajiaohttp.net:2000"
POOL_B = "http://w7wgt28979-region-VN-sid-BBBBBBBB-t-5:xum33c6k@us.lajiaohttp.net:2000"


def _config(tmp_path: Path) -> dict:
    return {
        "chatgpt": {},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "runtime": {"directory": str(tmp_path)},
    }


def _keys():
    return proxy_egress_key(POOL_A), proxy_egress_key(POOL_B)


# --------------------------------------------------------------------------
# 1. The key must separate sticky sessions
# --------------------------------------------------------------------------

def test_egress_key_separates_sticky_sessions_on_one_host():
    """The pool-collapse trap.

    Every entry in this install's pool shares ``us.lajiaohttp.net:2000``; only the
    ``-sid-XXXXXXXX-`` token differs.  If the key ignored it, the ledger would see
    one identity for the whole pool and refuse the second concurrent account.
    """
    key_a, key_b = _keys()
    assert key_a == "http://us.lajiaohttp.net:2000|sid=AAAAAAAA"
    assert key_b == "http://us.lajiaohttp.net:2000|sid=BBBBBBBB"
    assert key_a != key_b


def test_egress_key_is_stable_and_credential_free():
    key_a, _ = _keys()
    assert proxy_egress_key(POOL_A) == key_a
    assert "xum33c6k" not in key_a, "the password must not reach the ledger"
    assert "w7wgt28979" not in key_a, "the account name must not reach the ledger"


def test_egress_key_of_nothing_is_empty():
    assert proxy_egress_key(None) == ""
    assert proxy_egress_key("") == ""


# --------------------------------------------------------------------------
# 2. Arbitration
# --------------------------------------------------------------------------

def test_a_free_exit_is_leased(tmp_path):
    config = _config(tmp_path)
    key_a, _ = _keys()
    lease = acquire_environment_lease(
        exit_key=key_a, fingerprint_key="chrome136", account_ref="a@x.test", batch_id="b1",
        runtime_config=config,
    )
    assert lease["ok"] is True
    assert lease["state"] == "leased"
    assert lease["lease_id"] > 0
    assert lease["active_holders"] == 1
    assert lease["expires_at"] > int(time.time())


def test_a_live_lease_refuses_the_same_exit_and_names_the_holder(tmp_path):
    """"busy" is not actionable; "held by a@x.test" is."""
    config = _config(tmp_path)
    key_a, _ = _keys()
    first = acquire_environment_lease(exit_key=key_a, account_ref="a@x.test", runtime_config=config)
    second = acquire_environment_lease(exit_key=key_a, account_ref="b@x.test", runtime_config=config)

    assert second["ok"] is False
    assert second["state"] == "conflict"
    assert second["conflict"] == "exit"
    assert second["holder"]["account_ref"] == "a@x.test"
    assert second["lease_id"] == 0
    assert first["lease_id"] > 0


def test_a_live_lease_refuses_the_same_fingerprint_on_another_exit(tmp_path):
    config = _config(tmp_path)
    key_a, key_b = _keys()
    acquire_environment_lease(exit_key=key_a, fingerprint_key="chrome136", runtime_config=config)
    clash = acquire_environment_lease(
        exit_key=key_b, fingerprint_key="chrome136", account_ref="c@x.test", runtime_config=config
    )

    assert clash["ok"] is False
    assert clash["conflict"] == "fingerprint"


def test_an_unkeyed_request_never_allocates(tmp_path):
    config = _config(tmp_path)
    lease = acquire_environment_lease(exit_key="", fingerprint_key="", runtime_config=config)
    assert lease["ok"] is False
    assert lease["state"] == "unkeyed"
    assert list_environment_leases(runtime_config=config) == []


# --------------------------------------------------------------------------
# 3. The window has to open again -- this is the capacity-wall guard
# --------------------------------------------------------------------------

def test_releasing_the_last_holder_frees_the_exit(tmp_path):
    config = _config(tmp_path)
    key_a, _ = _keys()
    lease = acquire_environment_lease(exit_key=key_a, fingerprint_key="chrome136", runtime_config=config)
    assert release_environment_lease(lease["lease_id"], reason="registered", runtime_config=config)

    assert environment_ledger_stats(runtime_config=config)["live"] == 0
    again = acquire_environment_lease(exit_key=key_a, fingerprint_key="chrome136", runtime_config=config)
    assert again["ok"] is True


def test_a_lease_past_its_ttl_frees_the_exit(tmp_path):
    """The reason the indexes are partial.

    A ten-IP pool with a fifty-account batch cannot have permanent uniqueness.
    """
    config = _config(tmp_path)
    key_a, _ = _keys()
    first = acquire_environment_lease(exit_key=key_a, account_ref="a@x.test", ttl_seconds=1, runtime_config=config)
    assert first["ok"] is True

    # Still inside the window: refused.
    assert acquire_environment_lease(exit_key=key_a, account_ref="b@x.test", runtime_config=config)["ok"] is False

    time.sleep(1.2)
    swept = sweep_expired_environment_leases(runtime_config=config)
    assert swept == 1

    second = acquire_environment_lease(exit_key=key_a, account_ref="b@x.test", runtime_config=config)
    assert second["ok"] is True, "an expired lease must not hold the exit forever"


def test_the_acquire_path_sweeps_expired_leases_by_itself(tmp_path):
    """No external janitor is required for the window to open.

    This is the property that matters in production: nothing calls
    ``sweep_expired_environment_leases`` on a schedule, so if ``acquire`` did not
    sweep, a task that died mid-attempt would hold its exit until someone ran a
    repair script.  Pinned on the observable effect (the stale row's state and
    reason), not just on the returned count.
    """
    config = _config(tmp_path)
    key_a, _ = _keys()
    stale = acquire_environment_lease(
        exit_key=key_a, account_ref="a@x.test", ttl_seconds=1, runtime_config=config
    )
    time.sleep(1.2)

    second = acquire_environment_lease(exit_key=key_a, account_ref="b@x.test", runtime_config=config)
    assert second["ok"] is True
    assert second["swept"] >= 1

    stale_row = [r for r in list_environment_leases(limit=10, runtime_config=config) if r["id"] == stale["lease_id"]][0]
    assert stale_row["state"] == "expired"
    assert stale_row["reason"] == "ttl_expired"
    assert stale_row["released_at"] > 0

    # And the window really did reopen: the live lease is the new one.
    live = list_environment_leases(state="leased", runtime_config=config)
    assert len(live) == 1
    assert live[0]["id"] == second["lease_id"]


# --------------------------------------------------------------------------
# 4. Saturation reuse: allowed, recorded, and never for a profile clash
# --------------------------------------------------------------------------

def test_saturated_pool_reuse_is_allowed_and_recorded(tmp_path):
    config = _config(tmp_path)
    key_a, _ = _keys()
    first = acquire_environment_lease(
        exit_key=key_a, fingerprint_key="chrome136", account_ref="a@x.test", runtime_config=config
    )
    shared = acquire_environment_lease(
        exit_key=key_a, fingerprint_key="safari18_0", account_ref="b@x.test",
        allow_reuse=True, runtime_config=config,
    )

    assert shared["ok"] is True
    assert shared["state"] == "reused"
    assert shared["reused"] is True
    assert shared["lease_id"] == first["lease_id"], "reuse shares the existing lease"
    assert shared["active_holders"] == 2

    row = list_environment_leases(state="leased", runtime_config=config)[0]
    assert row["reuse_count"] == 1
    assert row["reason"] == "pool_saturated_reuse"
    assert environment_ledger_stats(runtime_config=config)["reused_live"] == 1


def test_the_loser_of_a_precheck_race_still_shares_the_lease(tmp_path):
    """The pre-check is UX only; the partial unique index is the arbiter.

    Reproduces the race the full suite caught on 2026-09-22: under load two
    attempts passed the pre-check together, the loser reached the IntegrityError
    path, and that path ignored ``allow_reuse`` -- so the loser was handed no
    lease and the share went unrecorded, which is exactly the blind spot this
    ledger exists to remove.  ``_live_conflicts`` is blinded on its first call to
    stand in for "the other racer committed between my SELECT and my INSERT".
    """
    from unittest.mock import patch

    from sms_tool.store import environment_ledger as ledger

    config = _config(tmp_path)
    key_a, _ = _keys()
    first = acquire_environment_lease(
        exit_key=key_a, fingerprint_key="chrome136", account_ref="a@x.test", runtime_config=config
    )

    real = ledger._live_conflicts
    calls = {"n": 0}

    def blind_once(conn, exit_key, fingerprint_key, now):
        calls["n"] += 1
        if calls["n"] == 1:
            return {}, {}  # pretend the exit looked free
        return real(conn, exit_key, fingerprint_key, now)

    with patch.object(ledger, "_live_conflicts", blind_once):
        second = acquire_environment_lease(
            exit_key=key_a, fingerprint_key="safari18_0", account_ref="b@x.test",
            allow_reuse=True, runtime_config=config,
        )

    assert second["ok"] is True, "the loser must still get a lease"
    assert second["state"] == "reused"
    assert second["lease_id"] == first["lease_id"]

    row = list_environment_leases(state="leased", runtime_config=config)[0]
    assert row["reuse_count"] == 1
    assert row["active_holders"] == 2


def test_reuse_never_papers_over_a_fingerprint_clash(tmp_path):
    """Sharing an exit is unavoidable with a small pool; sharing a profile is not."""
    config = _config(tmp_path)
    key_a, key_b = _keys()
    acquire_environment_lease(exit_key=key_a, fingerprint_key="chrome136", runtime_config=config)
    clash = acquire_environment_lease(
        exit_key=key_b, fingerprint_key="chrome136", account_ref="c@x.test",
        allow_reuse=True, runtime_config=config,
    )

    assert clash["ok"] is False
    assert clash["conflict"] == "fingerprint"
    assert clash["reused"] is False


def test_shared_lease_is_reference_counted(tmp_path):
    """The first task to finish must not free an exit another one is still using."""
    config = _config(tmp_path)
    key_a, _ = _keys()
    first = acquire_environment_lease(exit_key=key_a, fingerprint_key="chrome136", account_ref="a@x.test", runtime_config=config)
    acquire_environment_lease(
        exit_key=key_a, fingerprint_key="safari18_0", account_ref="b@x.test",
        allow_reuse=True, runtime_config=config,
    )

    release_environment_lease(first["lease_id"], runtime_config=config)
    live = list_environment_leases(state="leased", runtime_config=config)
    assert len(live) == 1
    assert live[0]["active_holders"] == 1
    assert acquire_environment_lease(exit_key=key_a, account_ref="c@x.test", runtime_config=config)["ok"] is False

    release_environment_lease(first["lease_id"], runtime_config=config)
    assert environment_ledger_stats(runtime_config=config)["live"] == 0


def test_concurrent_releases_never_strand_the_lease(tmp_path):
    """Reference counting has to be atomic, not read-then-write.

    Two tasks finishing in the same instant used to both read
    ``active_holders`` as 2, both write back 1, and leave the lease ``leased``
    with nobody left to release it -- a zombie row that keeps its exit gated
    until the TTL expires.  Measured 2026-09-22: 2 failures in 30 runs of the
    two-worker batch test, which is how it was found.

    Four releasers make the overlap the normal case instead of a coin flip.
    Against the fixed (single-statement) implementation the outcome is
    deterministic, so this cannot go flaky on its own account.
    """
    import threading

    config = _config(tmp_path)
    key_a, _ = _keys()
    first = acquire_environment_lease(
        exit_key=key_a, account_ref="a@x.test", runtime_config=config
    )
    for index in range(3):
        acquired = acquire_environment_lease(
            exit_key=key_a,
            account_ref=f"extra{index}@x.test",
            allow_reuse=True,
            runtime_config=config,
        )
        assert acquired["state"] == "reused"

    lease_id = first["lease_id"]
    live = list_environment_leases(state="leased", runtime_config=config)
    assert [row["active_holders"] for row in live] == [4]

    barrier = threading.Barrier(4, timeout=10)

    def release():
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            # A timeout only makes the overlap less likely; it must not become
            # an error, or the test would fail for the wrong reason.
            pass
        release_environment_lease(lease_id, reason="registered", runtime_config=config)

    threads = [threading.Thread(target=release) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    row = list_environment_leases(limit=5, runtime_config=config)[0]
    assert row["active_holders"] == 0, row
    assert row["state"] == "released", row
    assert row["released_at"] > 0, row
    assert row["reason"] == "registered", row
    assert environment_ledger_stats(runtime_config=config)["live"] == 0


# --------------------------------------------------------------------------
# 5. The measured exit IP is observation, not a gate
# --------------------------------------------------------------------------

def test_two_live_leases_measured_at_one_ip_are_reported(tmp_path):
    """The configured keys said "different exits"; the wire said otherwise.

    This is the only way that drift is visible, since the IP is unknowable until
    after the registration ran.
    """
    config = _config(tmp_path)
    key_a, key_b = _keys()
    first = acquire_environment_lease(exit_key=key_a, account_ref="a@x.test", runtime_config=config)
    second = acquire_environment_lease(exit_key=key_b, account_ref="b@x.test", runtime_config=config)

    assert record_environment_observations(first["lease_id"], exit_ip="198.51.100.7", runtime_config=config)["exit_ip_collisions"] == []
    result = record_environment_observations(second["lease_id"], exit_ip="198.51.100.7", runtime_config=config)

    assert result["recorded"] is True
    assert result["exit_ip_collisions"] == ["a@x.test"]


def test_a_released_lease_does_not_count_as_an_ip_collision(tmp_path):
    config = _config(tmp_path)
    key_a, key_b = _keys()
    first = acquire_environment_lease(exit_key=key_a, account_ref="a@x.test", runtime_config=config)
    record_environment_observations(first["lease_id"], exit_ip="198.51.100.7", runtime_config=config)
    release_environment_lease(first["lease_id"], runtime_config=config)

    second = acquire_environment_lease(exit_key=key_b, account_ref="b@x.test", runtime_config=config)
    assert record_environment_observations(second["lease_id"], exit_ip="198.51.100.7", runtime_config=config)["exit_ip_collisions"] == []


def test_the_profile_is_recorded_post_hoc_and_a_live_clash_is_reported(tmp_path):
    """The driver picks the profile inside the attempt, so it cannot gate either.

    Same treatment as the measured IP: recorded, and a live clash surfaced.
    """
    config = _config(tmp_path)
    key_a, key_b = _keys()
    first = acquire_environment_lease(exit_key=key_a, account_ref="a@x.test", runtime_config=config)
    second = acquire_environment_lease(exit_key=key_b, account_ref="b@x.test", runtime_config=config)

    # Neither was keyed up front (the batch runner does not know the profile yet).
    assert record_environment_observations(
        first["lease_id"], fingerprint_key="chrome136", runtime_config=config
    )["fingerprint_collisions"] == []
    result = record_environment_observations(
        second["lease_id"], fingerprint_key="chrome136", runtime_config=config
    )

    assert result["recorded"] is True
    assert result["fingerprint_collisions"] == ["a@x.test"]

    row = [r for r in list_environment_leases(limit=5, runtime_config=config) if r["id"] == second["lease_id"]][0]
    # Recorded on the OBSERVED column: the gating column is unique among live
    # leases, so writing a real collision there would raise instead of report.
    assert row["observed_fingerprint_key"] == "chrome136"
    assert row["fingerprint_key"] == ""


def test_both_dimensions_are_recorded_in_one_call(tmp_path):
    config = _config(tmp_path)
    key_a, _ = _keys()
    lease = acquire_environment_lease(exit_key=key_a, account_ref="a@x.test", runtime_config=config)
    result = record_environment_observations(
        lease["lease_id"], exit_ip="198.51.100.7", fingerprint_key="safari18_0", runtime_config=config
    )

    assert result["recorded"] is True
    row = list_environment_leases(state="leased", runtime_config=config)[0]
    assert row["exit_ip"] == "198.51.100.7"
    assert row["observed_fingerprint_key"] == "safari18_0"


def test_recording_nothing_is_a_no_op(tmp_path):
    config = _config(tmp_path)
    key_a, _ = _keys()
    lease = acquire_environment_lease(exit_key=key_a, account_ref="a@x.test", runtime_config=config)
    assert record_environment_observations(lease["lease_id"], runtime_config=config)["recorded"] is False


def test_recorded_exit_ip_survives_the_release(tmp_path):
    config = _config(tmp_path)
    key_a, _ = _keys()
    lease = acquire_environment_lease(exit_key=key_a, account_ref="a@x.test", runtime_config=config)
    release_environment_lease(lease["lease_id"], reason="registered", exit_ip="198.51.100.7", runtime_config=config)

    row = [r for r in list_environment_leases(limit=5, runtime_config=config) if r["id"] == lease["lease_id"]][0]
    assert row["state"] == "released"
    assert row["exit_ip"] == "198.51.100.7"
    assert row["reason"] == "registered"


def test_recording_an_ip_for_an_unknown_lease_is_a_no_op(tmp_path):
    config = _config(tmp_path)
    assert record_environment_observations(0, exit_ip="198.51.100.7", runtime_config=config)["recorded"] is False
    assert record_environment_observations(9999, exit_ip="", runtime_config=config)["recorded"] is False


# --------------------------------------------------------------------------
# 6. Schema
# --------------------------------------------------------------------------

def test_the_ledger_table_is_created_and_the_version_is_stamped(tmp_path):
    config = _config(tmp_path)
    init_database(runtime_config=config)

    from sms_tool.store import _connect

    conn = _connect(runtime_config=config)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert "environment_ledger" in tables
    assert "idx_environment_ledger_live_exit" in indexes
    assert "idx_environment_ledger_live_fingerprint" in indexes
    assert version == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 2


def test_the_unique_indexes_are_partial_so_a_released_row_does_not_block(tmp_path):
    """A non-partial unique index would make the ledger a capacity wall."""
    config = _config(tmp_path)
    init_database(runtime_config=config)

    from sms_tool.store import _connect

    conn = _connect(runtime_config=config)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_environment_ledger_live_exit'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert "WHERE" in sql.upper()
    assert "leased" in sql
