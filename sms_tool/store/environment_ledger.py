"""Time-boxed ledger of the egress/fingerprint environment each task holds.

Why this exists
---------------
Two registrations that run at the same time must not leave through the same
exit, and must not present the same fingerprint profile -- that correlation is
exactly what the registrar is looking for.  Nothing enforced either before this
module: ``batch_runner`` round-robins ``proxy_pool[(i + offset) % n]``, so with a
10-entry pool and any concurrency above 10 the same exit is handed to two live
accounts by construction, silently.

What it does NOT do
-------------------
It is not a capacity gate.  This install's pool is ten entries, so a batch of
fifty *must* reuse exits; a permanent-uniqueness ledger (the shape the reference
project uses) would simply refuse to allocate the eleventh task.  The lease is
therefore **time-boxed**: uniqueness is enforced only among leases that are live
*right now*, and a lease that is released or past its TTL frees its exit again.

Reuse is allowed, but never silent.  ``acquire_environment_lease(allow_reuse=True)``
records the sharing on the row (``active_holders`` / ``reuse_count`` / ``reason``)
so "we ran 50 accounts over 10 exits" is a query, not a guess.

Mechanism borrowed, code not
----------------------------
The reference implementation is ``gpt-auto-register-simulated-qc-source``
(``webui/db.py:115-133`` unique indexes, ``webui/environment.py:261-266`` retry on
collision).  That project is AGPL-3.0 and this repository carries no licence, so
only the mechanism -- a database UNIQUE constraint acting as the concurrency
arbiter -- was taken.  No code was copied.

The arbiter here is a **partial** unique index (``WHERE state='leased'``), which
the reference cannot express because it has no TTL: it enforces ``exit_ip`` and
``fingerprint_signature`` forever.  The partial predicate is what turns a hard
gate into a time window.

Two dimensions, deliberately asymmetric
---------------------------------------
* ``exit_key`` -- the *configured* egress identity from
  ``account_identity.proxy_egress_key`` (endpoint + sticky session id).  Known
  before the work starts, so it is the thing that can actually gate allocation.
* ``fingerprint_key`` -- the profile.  Also known up front.
* ``fingerprint_key`` -- the *pre-allocated* profile, when the caller knows it
  up front.  Gating, like ``exit_key``: unique among live leases.
* ``exit_ip`` / ``observed_fingerprint_key`` -- what the wire actually showed.
  The exit IP is measured, and the profile is chosen by the driver deep inside
  the attempt, so neither is knowable in time to gate anything.  They are
  recorded and cross-checked instead, which is the only way "two session ids came
  out of one address" or "two live accounts ran the same profile" becomes
  visible.  Making them unique would just make the post-hoc write raise --
  measured: recording a colliding profile on a live lease violates the index.

So each dimension has a gating column and an observed column, and the observed
ones are deliberately unconstrained.
"""

from __future__ import annotations

import sqlite3
import time

from ..config import ConfigInput

from .connection import _connect, init_database

# A registration round measures in the low minutes; 30 minutes is generous
# enough to cover a slow one plus its retries while still bounding how long a
# crashed task can squat on an exit.  Deliberately a module constant rather than
# a runtime.json knob: adding config surface for a value nobody has needed to
# tune is not worth the migration.
DEFAULT_LEASE_TTL_SECONDS = 1800

# Sweep reasons, so the ledger can explain itself without a join.
REASON_TTL_EXPIRED = "ttl_expired"
REASON_POOL_SATURATED = "pool_saturated_reuse"

STATE_LEASED = "leased"
STATE_RELEASED = "released"
STATE_EXPIRED = "expired"


def _now() -> int:
    return int(time.time())


def _ledger_connect(runtime_config: ConfigInput = None):
    """Connection with the schema guaranteed.

    Every public entry point in this module goes through here rather than calling
    ``_connect`` directly.  ``init_database`` is memoized per (process, path) so
    this is a dict lookup on the hot path -- but the ledger is also driven from
    one-off audit scripts, and those would otherwise hit
    ``no such table: environment_ledger`` on a database created before this
    module existed.
    """
    init_database(runtime_config=runtime_config)
    return _connect(runtime_config=runtime_config)


def exit_key_from_affinity(affinity) -> str:
    """``exit_key`` for a stored ``proxy_affinity`` mapping.

    Kept here (rather than only in ``account_identity``) because the ledger is
    also fed from persisted accounts during audits, where the affinity is already
    a plain dict and re-parsing the proxy URL would be lossy.
    """
    if not isinstance(affinity, dict) or not affinity:
        return ""
    endpoint = "{scheme}://{host}:{port}".format(
        scheme=str(affinity.get("scheme") or "http").lower(),
        host=str(affinity.get("host") or "").lower(),
        port=int(affinity.get("port") or 0),
    )
    if endpoint == "http://:0":
        return ""
    session_id = str(affinity.get("session_id") or "").strip()
    return f"{endpoint}|sid={session_id}" if session_id else endpoint


def sweep_expired_environment_leases(*, now: int = 0, runtime_config: ConfigInput = None) -> int:
    """Free every lease whose TTL has run out. Returns how many were freed.

    Runs on the acquire path, so an exit abandoned by a crashed task becomes
    available again without any external janitor.  Idempotent.
    """
    stamp = int(now or _now())
    conn = _ledger_connect(runtime_config)
    try:
        cursor = conn.execute(
            """
            UPDATE environment_ledger
            SET state=?, released_at=?, reason=?
            WHERE state=? AND expires_at <= ?
            """,
            (STATE_EXPIRED, stamp, REASON_TTL_EXPIRED, STATE_LEASED, stamp),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def _live_conflicts(conn, exit_key: str, fingerprint_key: str, now: int) -> tuple[dict, dict]:
    """The live leases already holding ``exit_key`` / ``fingerprint_key``."""
    exit_holder = {}
    if exit_key:
        row = conn.execute(
            "SELECT * FROM environment_ledger WHERE exit_key=? AND state=? AND expires_at > ?",
            (exit_key, STATE_LEASED, now),
        ).fetchone()
        exit_holder = dict(row) if row else {}
    fingerprint_holder = {}
    if fingerprint_key:
        row = conn.execute(
            "SELECT * FROM environment_ledger WHERE fingerprint_key=? AND state=? AND expires_at > ?",
            (fingerprint_key, STATE_LEASED, now),
        ).fetchone()
        fingerprint_holder = dict(row) if row else {}
    return exit_holder, fingerprint_holder


def _share_live_lease(conn, holder, *, expires_at: int) -> None:
    """Add one holder to a live lease and record why.

    ``expires_at`` is pushed out so the lease cannot lapse under the second
    holder and let a third task take the key meanwhile.

    Extracted because BOTH the pre-check path and the IntegrityError path need
    it.  Measured 2026-09-22 (full-suite run): under load two attempts raced
    through the pre-check, the loser reached the IntegrityError path, and that
    path used to ignore ``allow_reuse`` -- so the loser was handed no lease and
    the share went unrecorded, which is precisely the invisibility this ledger
    exists to remove.
    """
    conn.execute(
        """
        UPDATE environment_ledger
        SET active_holders = active_holders + 1,
            reuse_count = reuse_count + 1,
            expires_at = CASE WHEN expires_at < ? THEN ? ELSE expires_at END,
            reason = ?
        WHERE id = ?
        """,
        (expires_at, expires_at, REASON_POOL_SATURATED, int(holder["id"])),
    )
    conn.commit()


def acquire_environment_lease(
    *,
    exit_key: str,
    fingerprint_key: str = "",
    account_ref: str = "",
    batch_id: str = "",
    ttl_seconds: int = 0,
    allow_reuse: bool = False,
    runtime_config: ConfigInput = None,
) -> dict:
    """Try to hold ``exit_key`` / ``fingerprint_key`` for one task.

    Returns a dict, never raises, because the caller is mid-batch:

    ``ok``
        True when a lease is held -- freshly taken *or* deliberately shared.
    ``state``
        ``"leased"`` | ``"reused"`` | ``"conflict"`` | ``"unkeyed"``.
    ``conflict``
        ``""`` | ``"exit"`` | ``"fingerprint"`` | ``"unknown"`` -- which
        dimension refused.
    ``holder``
        The live lease that caused the conflict, so the caller can say *which*
        account is holding the exit instead of just "busy".
    ``reused``
        True when ``allow_reuse`` let two live tasks share one exit.  Recorded on
        the row; a silent share is the thing this is meant to prevent.
    """
    ttl = int(ttl_seconds or 0) or DEFAULT_LEASE_TTL_SECONDS
    exit_key = str(exit_key or "").strip()
    fingerprint_key = str(fingerprint_key or "").strip()
    account_ref = str(account_ref or "").strip()
    batch_id = str(batch_id or "").strip()
    if not exit_key and not fingerprint_key:
        return {
            "ok": False,
            "state": "unkeyed",
            "conflict": "unknown",
            "lease_id": 0,
            "holder": {},
            "reused": False,
            "active_holders": 0,
            "expires_at": 0,
            "swept": 0,
        }

    swept = sweep_expired_environment_leases(runtime_config=runtime_config)
    now = _now()
    expires_at = now + ttl
    conn = _ledger_connect(runtime_config)
    try:
        exit_holder, fingerprint_holder = _live_conflicts(conn, exit_key, fingerprint_key, now)
        holder = exit_holder or fingerprint_holder
        conflict = "exit" if exit_holder else ("fingerprint" if fingerprint_holder else "")

        if conflict and allow_reuse and exit_holder and not fingerprint_holder:
            # Saturated pool, not a profile clash: share the exit and say so.
            _share_live_lease(conn, exit_holder, expires_at=expires_at)
            return {
                "ok": True,
                "state": "reused",
                "conflict": "",
                "lease_id": int(exit_holder["id"]),
                "holder": exit_holder,
                "reused": True,
                "active_holders": int(exit_holder.get("active_holders") or 1) + 1,
                "expires_at": max(int(exit_holder.get("expires_at") or 0), expires_at),
                "swept": swept,
            }

        if conflict:
            return {
                "ok": False,
                "state": "conflict",
                "conflict": conflict,
                "lease_id": 0,
                "holder": holder,
                "reused": False,
                "active_holders": int(holder.get("active_holders") or 0),
                "expires_at": int(holder.get("expires_at") or 0),
                "swept": swept,
            }

        try:
            cursor = conn.execute(
                """
                INSERT INTO environment_ledger (
                    exit_key, exit_ip, fingerprint_key, account_ref, batch_id,
                    state, reason, leased_at, expires_at, released_at,
                    active_holders, reuse_count
                ) VALUES (?, '', ?, ?, ?, ?, '', ?, ?, 0, 1, 0)
                """,
                (exit_key, fingerprint_key, account_ref, batch_id, STATE_LEASED, now, expires_at),
            )
            conn.commit()
            return {
                "ok": True,
                "state": "leased",
                "conflict": "",
                "lease_id": int(cursor.lastrowid or 0),
                "holder": {},
                "reused": False,
                "active_holders": 1,
                "expires_at": expires_at,
                "swept": swept,
            }
        except sqlite3.IntegrityError:
            # The partial unique index is the arbiter: the pre-check above is UX
            # only, so a concurrent winner between the check and the insert lands
            # here. Re-read to name the dimension instead of parsing the message.
            conn.rollback()
            exit_holder, fingerprint_holder = _live_conflicts(conn, exit_key, fingerprint_key, now)
            # `allow_reuse` has to be honoured on this path too. It is not a
            # rare corner: under load the losing racer arrives here, and dropping
            # its lease would leave the share unrecorded -- the exact blind spot
            # the ledger exists to close.
            if allow_reuse and exit_holder and not fingerprint_holder:
                _share_live_lease(conn, exit_holder, expires_at=expires_at)
                return {
                    "ok": True,
                    "state": "reused",
                    "conflict": "",
                    "lease_id": int(exit_holder["id"]),
                    "holder": exit_holder,
                    "reused": True,
                    "active_holders": int(exit_holder.get("active_holders") or 1) + 1,
                    "expires_at": max(int(exit_holder.get("expires_at") or 0), expires_at),
                    "swept": swept,
                }
            holder = exit_holder or fingerprint_holder
            return {
                "ok": False,
                "state": "conflict",
                "conflict": "exit" if exit_holder else ("fingerprint" if fingerprint_holder else "unknown"),
                "lease_id": 0,
                "holder": holder,
                "reused": False,
                "active_holders": int(holder.get("active_holders") or 0),
                "expires_at": int(holder.get("expires_at") or 0),
                "swept": swept,
            }
    finally:
        conn.close()


def release_environment_lease(
    lease_id,
    *,
    state: str = STATE_RELEASED,
    reason: str = "",
    exit_ip: str = "",
    runtime_config: ConfigInput = None,
) -> bool:
    """Drop one holder. The lease ends when the last holder lets go.

    Shared leases are reference-counted, so the first task to finish must not
    free an exit another task is still using.

    The whole thing is **one statement**, not read-then-write.  The decrement and
    the "did that reach zero" decision have to be the same atomic act: reading
    ``active_holders``, subtracting in Python and writing back loses holders
    whenever two tasks finish together -- both read 2, both write 1, and the
    lease stays ``leased`` with nobody left to release it, squatting on its exit
    until the TTL expires.  Measured 2026-09-22: 2 failures in 30 runs of the
    two-worker batch test, which is how it was found.

    Every reference to a column in a ``SET`` clause sees the *pre-update* row, so
    ``active_holders - 1`` is the same value in all three CASE arms (verified
    against SQLite 3.43.1).  ``WHERE ... AND active_holders > 0`` keeps a
    duplicate release from driving the count negative.
    """
    try:
        lease = int(lease_id or 0)
    except (TypeError, ValueError):
        return False
    if lease <= 0:
        return False
    now = _now()
    measured_ip = str(exit_ip or "").strip()
    conn = _ledger_connect(runtime_config)
    try:
        cursor = conn.execute(
            """
            UPDATE environment_ledger
            SET active_holders = active_holders - 1,
                exit_ip = CASE WHEN ? <> '' THEN ? ELSE exit_ip END,
                state = CASE WHEN active_holders - 1 <= 0 THEN ? ELSE state END,
                reason = CASE WHEN active_holders - 1 <= 0 THEN ? ELSE reason END,
                released_at = CASE WHEN active_holders - 1 <= 0 THEN ? ELSE released_at END
            WHERE id=? AND active_holders > 0
            """,
            (
                measured_ip,
                measured_ip,
                str(state or STATE_RELEASED),
                str(reason or "")[:200],
                now,
                lease,
            ),
        )
        conn.commit()
        # False means "there was no holder left to drop": the lease is gone, or
        # somebody else already took the last one.
        return cursor.rowcount > 0
    finally:
        conn.close()


def record_environment_observations(
    lease_id,
    *,
    exit_ip: str = "",
    fingerprint_key: str = "",
    runtime_config: ConfigInput = None,
) -> dict:
    """Attach what the wire actually showed, and report any live collision.

    This is the post-hoc half of the ledger.  Both values are only knowable once
    the registration has run -- the exit IP because it is measured, the profile
    because the driver picks it deep inside the attempt -- so neither can gate
    allocation.  They answer the question the configured keys cannot: "did two
    live tasks really share an address, or really share a profile?"  A collision
    is reported rather than raised, because by the time we know, the request has
    already gone out and refusing the write would only lose the evidence.
    """
    ip = str(exit_ip or "").strip()
    fingerprint = str(fingerprint_key or "").strip()
    try:
        lease = int(lease_id or 0)
    except (TypeError, ValueError):
        return {"ok": False, "recorded": False, "exit_ip_collisions": [], "fingerprint_collisions": []}
    if lease <= 0 or (not ip and not fingerprint):
        return {"ok": False, "recorded": False, "exit_ip_collisions": [], "fingerprint_collisions": []}

    now = _now()
    conn = _ledger_connect(runtime_config)
    try:
        ip_collisions: list[str] = []
        if ip:
            rows = conn.execute(
                """
                SELECT account_ref, exit_key FROM environment_ledger
                WHERE exit_ip=? AND state=? AND id<>? AND expires_at > ?
                """,
                (ip, STATE_LEASED, lease, now),
            ).fetchall()
            ip_collisions = [str(row["account_ref"] or row["exit_key"] or "") for row in rows]
        fingerprint_collisions: list[str] = []
        if fingerprint:
            # Match either dimension: the other lease may have had its profile
            # pre-allocated (``fingerprint_key``) or only observed.
            rows = conn.execute(
                """
                SELECT account_ref, exit_key FROM environment_ledger
                WHERE (observed_fingerprint_key=? OR fingerprint_key=?)
                  AND state=? AND id<>? AND expires_at > ?
                """,
                (fingerprint, fingerprint, STATE_LEASED, lease, now),
            ).fetchall()
            fingerprint_collisions = [str(row["account_ref"] or row["exit_key"] or "") for row in rows]

        assignments = []
        params: list = []
        if ip:
            assignments.append("exit_ip=?")
            params.append(ip)
        if fingerprint:
            # Written to the OBSERVED column: the gating column is unique among
            # live leases, so recording a real collision there would raise.
            assignments.append("observed_fingerprint_key=?")
            params.append(fingerprint)
        params.append(lease)
        conn.execute(f"UPDATE environment_ledger SET {', '.join(assignments)} WHERE id=?", params)
        conn.commit()
        return {
            "ok": True,
            "recorded": True,
            "exit_ip_collisions": ip_collisions,
            "fingerprint_collisions": fingerprint_collisions,
        }
    finally:
        conn.close()


def list_environment_leases(
    *,
    state: str = "",
    limit: int = 200,
    runtime_config: ConfigInput = None,
) -> list[dict]:
    """Most recent leases first, for audits and the operator surface."""
    query = "SELECT * FROM environment_ledger"
    params: list = []
    if state:
        query += " WHERE state=?"
        params.append(str(state))
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit or 1)))
    conn = _ledger_connect(runtime_config)
    try:
        return [dict(row) for row in conn.execute(query, params)]
    finally:
        conn.close()


def environment_ledger_stats(*, runtime_config: ConfigInput = None) -> dict:
    """Counters an operator can read without guessing.

    ``reused_live`` is the one that matters: it is the number of live leases that
    two or more tasks are sharing, i.e. the amount of egress correlation we could
    not avoid because the pool is smaller than the batch.
    """
    now = _now()
    conn = _ledger_connect(runtime_config)
    try:
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN state='leased' AND expires_at > ? THEN 1 ELSE 0 END) AS live,
              SUM(CASE WHEN state='leased' AND expires_at <= ? THEN 1 ELSE 0 END) AS stale,
              SUM(CASE WHEN state='released' THEN 1 ELSE 0 END) AS released,
              SUM(CASE WHEN state='expired' THEN 1 ELSE 0 END) AS expired,
              SUM(CASE WHEN state='leased' AND expires_at > ? AND active_holders > 1 THEN 1 ELSE 0 END) AS reused_live,
              COUNT(DISTINCT CASE WHEN exit_ip <> '' THEN exit_ip END) AS measured_ips
            FROM environment_ledger
            """,
            (now, now, now),
        ).fetchone()
        return {
            "live": int(row["live"] or 0),
            "stale": int(row["stale"] or 0),
            "released": int(row["released"] or 0),
            "expired": int(row["expired"] or 0),
            "reused_live": int(row["reused_live"] or 0),
            "measured_ips": int(row["measured_ips"] or 0),
        }
    finally:
        conn.close()


__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "REASON_POOL_SATURATED",
    "REASON_TTL_EXPIRED",
    "STATE_EXPIRED",
    "STATE_LEASED",
    "STATE_RELEASED",
    "acquire_environment_lease",
    "environment_ledger_stats",
    "exit_key_from_affinity",
    "list_environment_leases",
    "record_environment_observations",
    "release_environment_lease",
    "sweep_expired_environment_leases",
]
