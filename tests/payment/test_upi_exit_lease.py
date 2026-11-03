"""P1-4: one lane per exit, across processes.

``payment.batch`` runs up to four UPI workers and hands each cell a proxy by
index (``payment/routing.py`` ``pool_offset % len(pool)``).  With a pool smaller
than the worker count -- and always across two processes -- two lanes could dial
the same residential credential simultaneously: every request then looks like two
accounts behind one IP inside a few seconds.

``upi_link.exit_lease`` adds an ``O_EXCL`` lease file per exit with a heartbeat
timestamp and a TTL for abandoned holds.  It is opt-in
(``payment.proxy_lease.enabled`` / ``UPI_PROXY_LEASE``) and **fail-open**: when
every candidate is leased the full list is still returned, because a possible
collision is better than refusing to run.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from sms_tool.payment.routing import PaymentRoutePlanner
from sms_tool.payment import upi_exit_lease as exit_lease


@pytest.fixture
def lease_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "lease"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("UPI_PROXY_LEASE_DIR", str(directory))
    monkeypatch.setenv("UPI_PROXY_LEASE", "1")
    monkeypatch.delenv("UPI_PROXY_LEASE_TTL", raising=False)
    # ``release_all`` walks a module-level holdings map, so leftovers from a
    # previous test would be counted against this one.
    exit_lease.reset_holdings()
    return directory


POOL = ["http://u:p@10.0.0.1:8080", "http://u:p@10.0.0.2:8080", "http://u:p@10.0.0.3:8080"]


# ── the lease primitive ────────────────────────────────────────────────────
def test_acquire_is_exclusive_per_owner(lease_dir):
    first, second = POOL[0], POOL[1]
    assert exit_lease.acquire(first, owner="lane-a") is True
    assert exit_lease.acquire(first, owner="lane-b") is False
    assert exit_lease.acquire(second, owner="lane-b") is True
    assert exit_lease.holder(first) == "lane-a"


def test_acquire_is_idempotent_for_the_holder(lease_dir):
    exit = POOL[0]
    assert exit_lease.acquire(exit, owner="lane-a") is True
    assert exit_lease.acquire(exit, owner="lane-a") is True


def test_release_frees_the_exit(lease_dir):
    exit = POOL[0]
    exit_lease.acquire(exit, owner="lane-a")
    assert exit_lease.release(exit, owner="lane-a") is True
    assert exit_lease.holder(exit) == ""
    assert exit_lease.acquire(exit, owner="lane-b") is True


def test_release_by_a_non_holder_is_refused(lease_dir):
    exit = POOL[0]
    exit_lease.acquire(exit, owner="lane-a")
    assert exit_lease.release(exit, owner="lane-b") is False
    assert exit_lease.holder(exit) == "lane-a"


def test_abandoned_lease_expires_after_the_ttl(lease_dir, monkeypatch):
    monkeypatch.setenv("UPI_PROXY_LEASE_TTL", "1")
    exit = POOL[0]
    exit_lease.acquire(exit, owner="lane-a")
    assert exit_lease.holder(exit) == "lane-a"
    # Age the lease past its TTL instead of sleeping.
    path = next(lease_dir.glob("*.lease"))
    old = time.time() - 5
    path.write_text(f"lane-a\n{old}", encoding="utf-8")
    assert exit_lease.holder(exit) == ""
    assert exit_lease.acquire(exit, owner="lane-b") is True


def test_heartbeat_keeps_the_lease_alive(lease_dir, monkeypatch):
    monkeypatch.setenv("UPI_PROXY_LEASE_TTL", "1")
    exit = POOL[0]
    exit_lease.acquire(exit, owner="lane-a")
    assert exit_lease.heartbeat(exit, owner="lane-a") is True
    assert exit_lease.heartbeat(exit, owner="lane-b") is False


def test_filter_available_is_fail_open(lease_dir):
    for exit in POOL:
        exit_lease.acquire(exit, owner="someone-else")
    # Every candidate is taken by another lane: keep them all rather than stall.
    assert exit_lease.filter_available(POOL, owner="lane-a") == POOL


def test_filter_available_drops_other_lanes_exits(lease_dir):
    exit_lease.acquire(POOL[0], owner="someone-else")
    assert exit_lease.filter_available(POOL, owner="lane-a") == POOL[1:]


def test_leasing_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("UPI_PROXY_LEASE", raising=False)
    monkeypatch.setenv("UPI_PROXY_LEASE_DIR", str(tmp_path / "off"))
    assert exit_lease.enabled({}) is False
    # Disabled ⇒ acquire is a no-op that reports success and writes nothing.
    assert exit_lease.acquire(POOL[0], owner="lane-a") is True
    assert exit_lease.holder(POOL[0]) == ""


def test_the_raw_credential_never_reaches_the_lease_file(lease_dir):
    exit_lease.acquire(POOL[0], owner="lane-a")
    body = "".join(path.read_text(encoding="utf-8") for path in lease_dir.glob("*.lease"))
    assert "10.0.0.1" not in body
    assert "u:p" not in body


def test_release_all_covers_everything_this_owner_holds(lease_dir):
    exit_lease.acquire(POOL[0], owner="lane-a")
    exit_lease.acquire(POOL[1], owner="lane-a")
    exit_lease.acquire(POOL[2], owner="lane-b")
    assert exit_lease.release_all(owner="lane-a") == 2
    assert exit_lease.holder(POOL[0]) == "" and exit_lease.holder(POOL[1]) == ""
    assert exit_lease.holder(POOL[2]) == "lane-b"


def test_lease_context_manager_releases_on_error(lease_dir):
    with pytest.raises(RuntimeError):
        with exit_lease.lease(POOL[0], owner="lane-a") as ok:
            assert ok is True
            raise RuntimeError("boom")
    assert exit_lease.holder(POOL[0]) == ""


# ── planner integration ────────────────────────────────────────────────────
def _planner(owner: str, monkeypatch):
    monkeypatch.setenv("UPI_PROXY_LEASE", "1")
    seen: list[list[str]] = []

    def selector(pool, expected, stage):
        seen.append(list(pool))
        return pool[0], []

    planner = PaymentRoutePlanner(
        {"payment": {"proxy_lease": {"enabled": True}}},
        proxy_state=object(),
        selector=selector,
        lease_owner=owner,
    )
    return planner, seen


def test_planner_skips_exits_another_lane_holds(lease_dir, monkeypatch):
    planner_a, seen_a = _planner("lane-a", monkeypatch)
    plan_a = planner_a.plan("upi", options={"checkout_proxy_pool": POOL}, pool_offset=0)
    assert plan_a.checkout_proxy == POOL[0]

    planner_b, seen_b = _planner("lane-b", monkeypatch)
    plan_b = planner_b.plan("upi", options={"checkout_proxy_pool": POOL}, pool_offset=0)
    # Same offset, different lane ⇒ must not be handed the leased exit.
    assert plan_b.checkout_proxy != POOL[0]
    assert POOL[0] not in seen_b[0]


def test_planner_does_not_lease_without_an_owner(lease_dir, monkeypatch):
    planner, _seen = _planner("", monkeypatch)
    planner.plan("upi", options={"checkout_proxy_pool": POOL}, pool_offset=0)
    assert list(lease_dir.glob("*.lease")) == []


def test_planner_falls_back_when_every_exit_is_held(lease_dir, monkeypatch):
    for exit in POOL:
        exit_lease.acquire(exit, owner="other-process")
    planner, seen = _planner("lane-a", monkeypatch)
    plan = planner.plan("upi", options={"checkout_proxy_pool": POOL}, pool_offset=0)
    assert plan.checkout_proxy == POOL[0]
    assert seen[0] == POOL  # fail-open: the full pool is still offered
