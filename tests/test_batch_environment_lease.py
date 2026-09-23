"""The batch runner's environment-lease wiring (``batch_runner`` helpers).

``batch_runner`` round-robins ``proxy_pool[(i + offset) % n]``, so with a
ten-entry pool any concurrency above ten hands the same exit to two live
accounts by construction.  Nothing recorded that before this.  These tests pin
the wiring, not the ledger itself (that is
``tests/test_environment_ledger.py``):

* the lease is keyed on the *configured* egress of the attempt,
* it is released even when the attempt failed, carrying the outcome,
* the measured IP comes from the geo cache and never triggers a probe, and
* a ledger failure can never take a batch down -- it is an observation channel.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from sms_tool import storage
from sms_tool.batch_runner import (
    _cached_exit_ip,
    _hold_environment_lease,
    _release_environment_lease,
)
from sms_tool.geo import ProxyGeo, remember_proxy_geo, reset_shared_geo_resolver
from sms_tool.store import environment_ledger_stats, list_environment_leases

# Two entries of the pool shape: one host, two sticky session ids.
#
# 🔴 The account and password are SYNTHETIC placeholders, not a live proxy
# account.  An earlier revision hard-coded a real one here, which put a
# working credential into a public repository.  Only the host + sid pair is
# load-bearing (`proxy_egress_key` drops the credentials), and the host is
# already named in docs/registration-and-proxy-architecture.md.
POOL_ACCOUNT = "testacct1"
POOL_PASSWORD = "testpw12"
POOL_A = f"http://{POOL_ACCOUNT}-region-VN-sid-AAAAAAAA-t-5:{POOL_PASSWORD}@us.lajiaohttp.net:2000"
POOL_B = f"http://{POOL_ACCOUNT}-region-VN-sid-BBBBBBBB-t-5:{POOL_PASSWORD}@us.lajiaohttp.net:2000"


class _TempDb:
    """Redirect the store at a throwaway database.

    ``_hold_environment_lease`` / ``_release_environment_lease`` deliberately take
    no ``runtime_config`` (the batch runner never threads one), so they resolve
    the database through ``sms_tool.storage.database_path`` -- the same seam the
    rest of the suite patches.
    """

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "accounts.sqlite3"
        self._patch = patch.object(storage, "database_path", return_value=self.path)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._tmp.cleanup()
        return False


def setup_function(_func=None):
    reset_shared_geo_resolver()


def teardown_function(_func=None):
    reset_shared_geo_resolver()


# --------------------------------------------------------------------------
# Acquire
# --------------------------------------------------------------------------

def test_hold_leases_the_configured_egress_of_the_attempt():
    with _TempDb() as db:
        lease = _hold_environment_lease(POOL_A, account_ref="a@x.test", batch_id="b1")

        assert lease["ok"] is True
        rows = list_environment_leases(state="leased")
        assert len(rows) == 1
        assert rows[0]["exit_key"] == "http://us.lajiaohttp.net:2000|sid=AAAAAAAA"
        assert rows[0]["account_ref"] == "a@x.test"
        assert rows[0]["batch_id"] == "b1"
        assert db.path.exists()


def test_two_live_attempts_on_one_egress_share_it_and_say_so():
    """Ten exits cannot serve a fifty-account batch exclusively.

    Sharing is unavoidable, so it is recorded rather than refused -- the point is
    that the correlation stops being invisible.
    """
    with _TempDb():
        first = _hold_environment_lease(POOL_A, account_ref="a@x.test", batch_id="b1")
        second = _hold_environment_lease(POOL_A, account_ref="b@x.test", batch_id="b1")

        assert first["ok"] is True
        assert second["ok"] is True
        assert second["state"] == "reused"
        assert second["lease_id"] == first["lease_id"]

        rows = list_environment_leases(state="leased")
        assert len(rows) == 1
        assert rows[0]["active_holders"] == 2
        assert rows[0]["reuse_count"] == 1
        assert rows[0]["reason"] == "pool_saturated_reuse"
        assert environment_ledger_stats()["reused_live"] == 1


def test_a_different_session_id_is_a_different_lease():
    with _TempDb():
        assert _hold_environment_lease(POOL_A, account_ref="a@x.test", batch_id="b1")["ok"]
        assert _hold_environment_lease(POOL_B, account_ref="b@x.test", batch_id="b1")["ok"]
        assert len(list_environment_leases(state="leased")) == 2


def test_a_ledger_failure_never_breaks_the_attempt():
    """It is an observation channel; it must not be able to take a batch down.

    Patched on ``batch_runner``, not on ``sms_tool.store``: the runner binds these
    symbols at import time, so patching the shell would silently leave the real
    function in place and the test would pass without testing anything.
    """
    with patch("sms_tool.batch_runner.acquire_environment_lease", side_effect=RuntimeError("db gone")):
        assert _hold_environment_lease(POOL_A, account_ref="a@x.test", batch_id="b1") == {}


def test_an_empty_egress_never_allocates():
    with _TempDb():
        lease = _hold_environment_lease("", account_ref="a@x.test", batch_id="b1")
        assert lease["ok"] is False
        assert list_environment_leases() == []


# --------------------------------------------------------------------------
# Release
# --------------------------------------------------------------------------

def test_release_records_the_outcome_and_frees_the_egress():
    with _TempDb():
        lease = _hold_environment_lease(POOL_A, account_ref="a@x.test", batch_id="b1")
        _release_environment_lease(lease, {"success": True, "auth_fingerprint_profile": "chrome136"}, POOL_A)

        assert environment_ledger_stats()["live"] == 0
        row = list_environment_leases(limit=5)[0]
        assert row["state"] == "released"
        assert row["reason"] == "registered"
        assert row["observed_fingerprint_key"] == "chrome136"


def test_release_carries_the_failure_class_as_the_reason():
    with _TempDb():
        lease = _hold_environment_lease(POOL_A, account_ref="a@x.test", batch_id="b1")
        _release_environment_lease(lease, {"success": False, "failure_class": "network"}, POOL_A)

        row = list_environment_leases(limit=5)[0]
        assert row["state"] == "released"
        assert row["reason"] == "network"


def test_release_reads_the_measured_exit_ip_from_the_geo_cache():
    """Seeded the way an upstream stage would have seeded it -- no probe here."""
    with _TempDb():
        remember_proxy_geo(POOL_A, ProxyGeo(ip="198.51.100.7", country="US"))
        lease = _hold_environment_lease(POOL_A, account_ref="a@x.test", batch_id="b1")
        _release_environment_lease(lease, {"success": True}, POOL_A)

        row = list_environment_leases(limit=5)[0]
        assert row["exit_ip"] == "198.51.100.7"


def test_release_without_a_cached_measurement_leaves_the_ip_blank():
    with _TempDb():
        lease = _hold_environment_lease(POOL_A, account_ref="a@x.test", batch_id="b1")
        _release_environment_lease(lease, {"success": True}, POOL_A)

        assert list_environment_leases(limit=5)[0]["exit_ip"] == ""


def test_release_is_a_no_op_without_a_lease():
    with _TempDb():
        _release_environment_lease({}, {"success": True}, POOL_A)
        _release_environment_lease(None, {"success": True}, POOL_A)
        _release_environment_lease({"lease_id": 0}, {"success": True}, POOL_A)
        assert list_environment_leases() == []


def test_release_survives_a_non_dict_result():
    with _TempDb():
        lease = _hold_environment_lease(POOL_A, account_ref="a@x.test", batch_id="b1")
        _release_environment_lease(lease, "not-a-dict", POOL_A)

        row = list_environment_leases(limit=5)[0]
        assert row["state"] == "released"
        assert row["reason"] == "failed"


def test_release_survives_a_ledger_failure():
    """Both halves of the release are best-effort.

    The request has already gone out by the time this runs, so a bookkeeping
    failure must not turn a success into a failure.  The lease is left as it was,
    which the TTL is there to clean up.
    """
    with _TempDb():
        lease = _hold_environment_lease(POOL_A, account_ref="a@x.test", batch_id="b1")
        with patch("sms_tool.batch_runner.record_environment_observations", side_effect=RuntimeError("db gone")), \
             patch("sms_tool.batch_runner.release_environment_lease", side_effect=RuntimeError("db gone")):
            _release_environment_lease(lease, {"success": True}, POOL_A)

        # Nothing was recorded because both writes failed -- and nothing raised.
        assert list_environment_leases(state="leased")[0]["state"] == "leased"


# --------------------------------------------------------------------------
# The IP read must never probe
# --------------------------------------------------------------------------

def test_cached_exit_ip_reads_the_cache_without_probing():
    remember_proxy_geo(POOL_A, ProxyGeo(ip="198.51.100.7", country="US"))

    with patch("sms_tool.geo.resolver.GeoResolver.resolve", side_effect=AssertionError("must not probe")):
        assert _cached_exit_ip(POOL_A) == "198.51.100.7"


def test_cached_exit_ip_is_blank_when_nothing_was_measured():
    with patch("sms_tool.geo.resolver.GeoResolver.resolve", side_effect=AssertionError("must not probe")):
        assert _cached_exit_ip(POOL_A) == ""
        assert _cached_exit_ip("") == ""
        assert _cached_exit_ip(None) == ""


# --------------------------------------------------------------------------
# End to end through run_batch_impl
# --------------------------------------------------------------------------

class _NoopGuard:
    def __init__(self, *_args, **_kwargs):
        pass

    def blocked_email_states(self):
        return {}

    def check(self, _email):
        return {"deferred": False, "dead_end": False, "quarantined": False}

    def record(self, *_args, **_kwargs):
        pass


def _run_batch_over(*, proxy_pool, mailboxes, run_email, workers, count):
    from sms_tool.batch_runner import run_batch_impl

    with patch("sms_tool.batch_runner.CFG", {"email_registration": {}, "registration": {"driver": "protocol"}}), \
         patch("sms_tool.batch_runner.RegistrationRetryGuard", _NoopGuard), \
         patch("sms_tool.batch_runner.ProxyHealthTracker"), \
         patch("sms_tool.batch_runner.get_account_records", return_value={}), \
         patch("sms_tool.batch_runner.list_account_records", return_value=[]), \
         patch("sms_tool.batch_runner.get_registration_checkpoints", return_value={}):
        return run_batch_impl(
            count=count,
            proxy_pool=list(proxy_pool),
            mailboxes=list(mailboxes),
            workers=workers,
            max_attempts=1,
            retry_delay_seconds=0,
            run_email_func=run_email,
            registration_driver="protocol",
        )


def test_every_attempt_in_a_batch_is_accounted_for():
    """No attempt may go unrecorded -- the invariant the 2026-09-22 race broke.

    Two accounts, one non-sticky pool entry (so ``refresh_proxy_sid`` is a no-op
    and both attempts really do share one configured exit).  Whether the second
    attempt finds the first lease through the pre-check or through the unique
    index is a race, and which of the two becomes the owner is too -- so the
    assertions are written as the invariant that must hold either way:

        (rows created) + (shares recorded) == (attempts made)

    A dropped lease shows up here as 1 + 0 != 2, which is exactly how the race
    that silently unrecorded the share was caught.
    """
    import threading
    from types import SimpleNamespace

    with _TempDb():
        mailboxes = [SimpleNamespace(email="a@x.test"), SimpleNamespace(email="b@x.test")]
        proxy = "http://user:pass@plain.proxy.example:8080"
        barrier = threading.Barrier(2, timeout=10)

        def run_email(**kwargs):
            try:
                # Force the two attempts to overlap; a timeout here must not turn
                # into a failed registration, it only makes the share less likely.
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return {
                "success": True,
                "email": kwargs.get("mailbox").email,
                "auth_fingerprint_profile": "chrome136",
            }

        results = _run_batch_over(
            proxy_pool=[proxy], mailboxes=mailboxes, run_email=run_email, workers=2, count=2
        )

        assert len(results) == 2
        assert all(result.get("success") for result in results)

        rows = list_environment_leases(limit=10)
        assert rows, "the batch must leave a record at all"
        assert all(row["exit_key"] == "http://plain.proxy.example:8080" for row in rows), \
            "a non-sticky entry has exactly one configured egress"
        assert all(row["state"] == "released" for row in rows)
        assert all(row["active_holders"] == 0 for row in rows)
        assert all(row["reason"] == "registered" for row in rows)
        assert all(row["observed_fingerprint_key"] == "chrome136" for row in rows)

        assert len(rows) + sum(int(row["reuse_count"]) for row in rows) == 2, (
            f"every attempt must be accounted for: rows={len(rows)}, "
            f"reuse={[row['reuse_count'] for row in rows]}"
        )


def test_a_sticky_pool_entry_gives_each_attempt_its_own_key():
    """The lajiao shape: one host, and a fresh sticky session per attempt.

    ``refresh_proxy_sid`` mints a new session id, so the configured keys differ
    even though the pool entry is the same -- which is why the *measured* exit IP
    is the dimension that actually tells us whether two accounts shared a wire.
    """
    from types import SimpleNamespace

    with _TempDb():
        mailboxes = [SimpleNamespace(email="a@x.test"), SimpleNamespace(email="b@x.test")]

        def run_email(**kwargs):
            return {"success": True, "email": kwargs.get("mailbox").email}

        results = _run_batch_over(
            proxy_pool=[POOL_A], mailboxes=mailboxes, run_email=run_email, workers=1, count=2
        )

        assert len(results) == 2
        rows = list_environment_leases(limit=10)
        assert len(rows) == 2
        keys = {row["exit_key"] for row in rows}
        assert len(keys) == 2, "a refreshed session id must produce a distinct key"
        assert all(key.startswith("http://us.lajiaohttp.net:2000|sid=") for key in keys)
        assert all(row["state"] == "released" for row in rows)
        assert all(row["reuse_count"] == 0 for row in rows)
        assert environment_ledger_stats()["live"] == 0


def test_a_failed_attempt_still_releases_its_lease():
    """A crashed or failed registration must not squat on an exit until the TTL."""
    from types import SimpleNamespace

    with _TempDb():
        mailbox = SimpleNamespace(email="boom@x.test")

        def run_email(**_kwargs):
            raise RuntimeError("worker blew up")

        results = _run_batch_over(
            proxy_pool=[POOL_A], mailboxes=[mailbox], run_email=run_email, workers=1, count=1
        )

        assert len(results) == 1
        rows = list_environment_leases(limit=10)
        assert len(rows) == 1
        assert rows[0]["state"] == "released"
        assert rows[0]["active_holders"] == 0
        assert environment_ledger_stats()["live"] == 0
