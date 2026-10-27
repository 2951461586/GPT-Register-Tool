"""Persistent ASN ledger: rotation, hit rate and transport health per supplier.

Why an ASN-level ledger
-----------------------
A residential provider sells *pools* keyed by ASN: every exit inside one ASN
shares a reputation neighbourhood, so a run that spends 20 accounts on a
burned ASN learns 20 times the same lesson. The pool already tracks health per
endpoint (``proxy/health.py``) and quality per proxy (``proxy/screening.py``);
neither can answer "which ASNs have we not tried this round?".

This module owns that third question, and only that:

* **round-robin allocation** across enabled ASNs, with the cursor persisted so
  a restart does not re-start at ASN #1 (the batch-after-batch skew the
  reference project hit),
* **hit rate** per ASN (registrations that succeeded / valid attempts),
* **transport health** (proxy failures/successes, consecutive failure streak),
* **manual enable/disable** — the only way an ASN is turned off.

Deliberate non-goals
--------------------
* No automatic blacklisting. The reference project's ``finalize_round`` keeps
  every ASN enabled after a round on purpose, so hit-rate comparison stays
  available; disabling is an operator decision (or ``batch_circuit_breaker``
  for a whole-batch abort). Auto-blacklisting one supplier on a bad afternoon
  is how a pool silently empties.
* No proxy strings. Rows carry ASN/ISP/country metadata only — the ledger is
  safe to ship in a support bundle.

Wiring
------
``order_candidates()`` is the bridge to the registration batch. It is opt-in
(``proxy.asn_allocation.enabled``, default **off**) so enabling the ledger
never changes an existing pool's ordering by accident. It only reads ASNs the
geo cache already measured; it never probes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: ASNs accepted as residential candidates. ``mobile`` is a distinct risk
#: profile (CGNAT) and is kept because it is often *better*, not worse.
RESIDENTIAL_NETWORK_TYPES = frozenset({"residential", "mobile"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS asn_performance (
    asn TEXT PRIMARY KEY,
    isp TEXT NOT NULL DEFAULT '',
    network_type TEXT NOT NULL DEFAULT 'unknown',
    country_code TEXT NOT NULL DEFAULT '',
    valid_samples INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    proxy_failures INTEGER NOT NULL DEFAULT 0,
    proxy_successes INTEGER NOT NULL DEFAULT 0,
    consecutive_proxy_failures INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    disabled_reason TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS asn_performance_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proxy_asn (
    proxy_key TEXT PRIMARY KEY,
    asn TEXT NOT NULL,
    network_type TEXT NOT NULL DEFAULT 'unknown',
    country_code TEXT NOT NULL DEFAULT '',
    isp TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_proxy_asn_asn ON proxy_asn(asn);
"""

# Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` does not
# add them to an existing database, and the upgrade must be idempotent because
# several processes (CLI + desktop) open the same file.
#
# Each ALTER is written out at its call site rather than assembled from a
# (column, declaration) table: a DDL string chosen at runtime is a shape a
# reviewer (and a scanner) cannot check at a glance.


def _proxy_key(proxy: str | None) -> str:
    """Credential-free identity for a configured exit (same key as health).

    The ledger stores ``proxy_key -> asn`` so a screening run can teach the
    allocator which ASN each configured sticky session leaves through. Reusing
    ``ProxyHealthTracker.key`` keeps the two journals aligned: the registration
    health record and the ASN mapping then key on exactly the same endpoint +
    session hash, and no proxy credential is ever written to the database.
    """
    value = str(proxy or "").strip()
    if not value or "***" in value:
        # A masked/probe-report value must never become a ledger key; it would
        # map a fake credential to a real ASN.
        return ""
    try:
        from .entry import parse_proxy
        from .health import ProxyHealthTracker

        entry = parse_proxy(value)
        key = ProxyHealthTracker.key(entry.url if entry is not None else value)
        return "" if key == ":0" else key
    except Exception:
        return ""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_dict(value: Any) -> dict[str, Any]:
    """Decode a JSON object from untrusted SQLite text without raising."""
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _default_path(config: Mapping[str, Any] | None = None) -> Path:
    """Resolve ``runtime/asn_performance.sqlite3``.

    Imported inside the function on purpose: ``scripts/import_layer_ratchet.py``
    freezes ``proxy -> kernel`` at module scope, and ``proxy/health.py`` already
    pays for its own copy of this edge. The same deferral is what
    ``mailbox/reclaim.py`` does for ``runtime_file``.
    """
    from ..kernel.paths import runtime_file

    return runtime_file(dict(config or {}), "asn_performance.sqlite3")


class AsnRegistry:
    """SQLite-backed ASN allocation policy. One instance per process is enough;

    every method takes the instance lock and its own short-lived connection, so
    the CLI and the desktop can share the file without a long-lived handle.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path or _default_path())
        self._lock = threading.RLock()
        self._initialize()

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> "AsnRegistry":
        return cls(_default_path(config))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _open(self):
        """One transaction-scoped connection that is always closed.

        ``with sqlite3.connect(...) as connection`` commits but does **not**
        close; on Windows the still-open handle keeps the database file locked
        and broke temp-directory cleanup in the tests. This wrapper does both.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._open() as connection:
            connection.executescript(_SCHEMA)
            existing = {row[1] for row in connection.execute("PRAGMA table_info(asn_performance)").fetchall()}
            if "country_code" not in existing:
                connection.execute("ALTER TABLE asn_performance ADD COLUMN country_code TEXT NOT NULL DEFAULT ''")
            if "proxy_failures" not in existing:
                connection.execute("ALTER TABLE asn_performance ADD COLUMN proxy_failures INTEGER NOT NULL DEFAULT 0")
            if "proxy_successes" not in existing:
                connection.execute("ALTER TABLE asn_performance ADD COLUMN proxy_successes INTEGER NOT NULL DEFAULT 0")
            if "consecutive_proxy_failures" not in existing:
                connection.execute(
                    "ALTER TABLE asn_performance ADD COLUMN consecutive_proxy_failures INTEGER NOT NULL DEFAULT 0"
                )

    # ── configuration ────────────────────────────────────────────────────────
    def configure(self, candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Upsert ASN metadata from screening rows.

        Only residential/mobile candidates are recorded: a datacenter ASN is a
        screening *rejection*, and keeping it in the allocation table would let
        it win a round-robin slot it can never justify.
        """
        with self._lock, self._open() as connection:
            for candidate in candidates or []:
                asn = str(candidate.get("asn") or "").strip().upper()
                network = str(candidate.get("network_type") or "unknown").strip().lower()
                if not asn or network not in RESIDENTIAL_NETWORK_TYPES:
                    continue
                row = connection.execute("SELECT metadata_json FROM asn_performance WHERE asn=?", (asn,)).fetchone()
                metadata = _json_dict(row["metadata_json"]) if row else {}
                # Never copy a proxy URL (or any credential-shaped field) into
                # metadata_json; the ledger is meant to be safe to ship. The
                # credential-free mapping lives in ``proxy_asn`` only.
                metadata.update(
                    {
                        key: value
                        for key, value in dict(candidate).items()
                        if key not in {"proxy", "username", "password"} and value not in (None, "")
                    }
                )
                connection.execute(
                    """
                    INSERT INTO asn_performance(asn, isp, network_type, country_code, metadata_json)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(asn) DO UPDATE SET
                        isp=excluded.isp,
                        network_type=excluded.network_type,
                        country_code=excluded.country_code,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        asn,
                        str(candidate.get("isp") or metadata.get("isp") or ""),
                        network,
                        str(candidate.get("country_code") or candidate.get("country") or "").upper(),
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )
                proxy_key = _proxy_key(str(candidate.get("proxy") or ""))
                if proxy_key:
                    connection.execute(
                        """
                        INSERT INTO proxy_asn(proxy_key, asn, network_type, country_code, isp, updated_at)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(proxy_key) DO UPDATE SET
                            asn=excluded.asn,
                            network_type=excluded.network_type,
                            country_code=excluded.country_code,
                            isp=excluded.isp,
                            updated_at=excluded.updated_at
                        """,
                        (
                            proxy_key,
                            asn,
                            network,
                            str(candidate.get("country_code") or candidate.get("country") or "").upper(),
                            str(candidate.get("isp") or metadata.get("isp") or ""),
                            _as_int(time.time(), 0),
                        ),
                    )
        return self.rows()

    def rows(self) -> list[dict[str, Any]]:
        with self._open() as connection:
            rows = connection.execute("SELECT * FROM asn_performance ORDER BY asn").fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            metadata = _json_dict(item.pop("metadata_json"))
            for key in ("country_code", "session_minutes", "max_attempts"):
                if key in metadata and not item.get(key):
                    item[key] = metadata[key]
            item["enabled"] = bool(item["enabled"])
            samples = _as_int(item["valid_samples"])
            item["hit_rate"] = round(_as_int(item["hits"]) / samples * 100, 1) if samples else 0.0
            transport = _as_int(item["proxy_failures"]) + _as_int(item["proxy_successes"])
            item["transport_success_rate"] = (
                round(_as_int(item["proxy_successes"]) / transport * 100, 1) if transport else 0.0
            )
            result.append(item)
        return result

    def get(self, asn: str) -> dict[str, Any] | None:
        key = str(asn or "").strip().upper()
        return next((row for row in self.rows() if row["asn"] == key), None)

    def internal_config(self, asn: str) -> dict[str, Any]:
        with self._open() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM asn_performance WHERE asn=?", (str(asn or "").upper(),)
            ).fetchone()
        return _json_dict(row["metadata_json"]) if row else {}

    def asn_of(self, proxy: str | None) -> str:
        """Persisted ASN for a configured exit, or ``""`` when never screened."""
        key = _proxy_key(proxy)
        if not key:
            return ""
        with self._open() as connection:
            row = connection.execute("SELECT asn FROM proxy_asn WHERE proxy_key=?", (key,)).fetchone()
        return str(row["asn"] or "").upper() if row else ""

    # ── allocation ───────────────────────────────────────────────────────────
    def allocate(self, count: int) -> list[str]:
        """Return ``count`` ASNs round-robin, advancing the persisted cursor.

        The cursor lives in a separate meta table rather than in memory: a
        restart mid-batch used to reset the rotation and hammer the same ASN
        for the first N accounts of every process.
        """
        wanted = max(0, _as_int(count, 0))
        if not wanted:
            return []
        with self._lock, self._open() as connection:
            enabled = [
                str(row["asn"])
                for row in connection.execute("SELECT asn FROM asn_performance WHERE enabled=1 ORDER BY asn").fetchall()
            ]
            if not enabled:
                return []
            cursor_row = connection.execute("SELECT value FROM asn_performance_meta WHERE key='cursor'").fetchone()
            cursor = (_as_int(cursor_row["value"], 0) if cursor_row else 0) % len(enabled)
            result = [enabled[(cursor + index) % len(enabled)] for index in range(wanted)]
            connection.execute(
                "INSERT INTO asn_performance_meta(key, value) VALUES('cursor', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str((cursor + wanted) % len(enabled)),),
            )
        return result

    # ── outcomes ─────────────────────────────────────────────────────────────
    def record(self, asn: str, *, hit: bool, valid: bool = True) -> None:
        """Record one registration attempt against an ASN's hit rate.

        ``valid=False`` means "this attempt says nothing about the ASN" (the
        failure was a mailbox/OTP problem), so it must not move the denominator.
        """
        key = str(asn or "").strip().upper()
        if not key or not valid:
            return
        with self._lock, self._open() as connection:
            row = connection.execute("SELECT valid_samples, hits FROM asn_performance WHERE asn=?", (key,)).fetchone()
            if not row:
                return
            connection.execute(
                "UPDATE asn_performance SET valid_samples=?, hits=? WHERE asn=?",
                (_as_int(row["valid_samples"]) + 1, _as_int(row["hits"]) + (1 if hit else 0), key),
            )

    def record_proxy_outcome(self, asn: str, *, ok: bool) -> None:
        """Track transport health without ever disabling an ASN.

        A dead proxy on Tuesday is not evidence about the ASN's registration
        reputation; it is evidence about the transport. The streak is what a
        UI shows next to "手动停用", not a trigger.
        """
        key = str(asn or "").strip().upper()
        if not key:
            return
        with self._lock, self._open() as connection:
            row = connection.execute(
                "SELECT proxy_failures, proxy_successes, consecutive_proxy_failures FROM asn_performance WHERE asn=?",
                (key,),
            ).fetchone()
            if not row:
                return
            failures = _as_int(row["proxy_failures"])
            successes = _as_int(row["proxy_successes"])
            streak = _as_int(row["consecutive_proxy_failures"])
            if ok:
                successes += 1
                streak = 0
            else:
                failures += 1
                streak += 1
            connection.execute(
                "UPDATE asn_performance SET proxy_failures=?, proxy_successes=?, "
                "consecutive_proxy_failures=? WHERE asn=?",
                (failures, successes, streak, key),
            )

    def set_enabled(self, asn: str, enabled: bool, *, reason: str = "") -> bool:
        key = str(asn or "").strip().upper()
        if not key:
            return False
        with self._lock, self._open() as connection:
            updated = connection.execute(
                "UPDATE asn_performance SET enabled=?, disabled_reason=? WHERE asn=?",
                (1 if enabled else 0, "" if enabled else (reason or "手动停用"), key),
            ).rowcount
        return bool(updated)


# ── registry ↔ batch wiring ──────────────────────────────────────────────────
def _allocation_enabled(config: Mapping[str, Any] | None) -> bool:
    section = (config or {}).get("proxy")
    if not isinstance(section, Mapping):
        return False
    allocation = section.get("asn_allocation")
    if not isinstance(allocation, Mapping):
        return False
    return bool(allocation.get("enabled"))


def cached_asn_of(proxy: str | None, *, registry: "AsnRegistry | None" = None) -> str:
    """Return the ASN measured for this proxy, if any.

    The shared geo cache is read first (never a network round-trip). When the
    caller supplies a ledger, a persisted ``proxy_asn`` row is the fallback:
    that is what makes a screening run useful to the *next* process instead of
    letting the in-memory cache disappear on restart.
    """
    if not proxy:
        return ""
    try:
        from ..geo import shared_geo_resolver

        geo = shared_geo_resolver().cached(proxy)
    except Exception:
        geo = None
    asn = str(getattr(geo, "asn", "") or "").upper()
    if asn:
        return asn
    return registry.asn_of(proxy) if registry is not None else ""


def order_candidates(
    candidates: Sequence[str],
    *,
    config: Mapping[str, Any] | None = None,
    registry: AsnRegistry | None = None,
    rank: Any = None,
) -> list[str]:
    """Reorder registration candidates by the ASN round-robin allocation.

    Ordering is two-level: the allocated ASN order first, then each group keeps
    whatever order the caller's health ranking produced. Proxies whose ASN is
    unknown keep that ranking and go last — an unmeasured exit must not outrank
    a measured one, but it must still be *reachable* (the pool may be brand new
    and have no measurements yet).

    Returns ``candidates`` unchanged when the feature is off, when no ASN is
    registered, or when nothing resolves to a measured ASN. That fail-open
    behaviour is what makes the feature safe to enable on an existing pool.
    """
    entries = [str(item or "") for item in candidates or [] if str(item or "").strip()]
    if len(entries) <= 1 or not _allocation_enabled(config):
        return entries
    try:
        ledger = registry or AsnRegistry.from_config(config)
    except Exception as exc:  # a broken ledger must not break registration
        logger.warning("asn allocation unavailable (%s); keeping pool order", type(exc).__name__)
        return entries
    allocation = ledger.allocate(len(entries))
    if not allocation:
        return entries
    grouped: dict[str, list[str]] = {}
    unmeasured: list[str] = []
    for entry in entries:
        asn = cached_asn_of(entry, registry=ledger)
        if asn:
            grouped.setdefault(asn, []).append(entry)
        else:
            unmeasured.append(entry)
    ordered: list[str] = []
    for asn in allocation:
        ordered.extend(grouped.pop(asn, []))
    # Remaining measured groups (ASNs the ledger does not know yet) keep the
    # caller's ranking, then the unmeasured tail.
    for asn in grouped:
        ordered.extend(grouped[asn])
    ordered.extend(unmeasured)
    if rank is not None:
        try:
            ordered = list(rank(ordered))
        except Exception:
            pass
    return ordered


def record_registration_outcome(
    proxy: str | None,
    *,
    ok: bool,
    valid: bool = True,
    config: Mapping[str, Any] | None = None,
    registry: AsnRegistry | None = None,
) -> str:
    """Record one registration result against the proxy's measured ASN.

    Returns the ASN it recorded (empty when unknown), so callers can log the
    attribution without re-reading the cache.
    """
    try:
        ledger = registry or AsnRegistry.from_config(config)
    except Exception:
        return ""
    asn = cached_asn_of(proxy, registry=ledger)
    if not asn:
        return ""
    ledger.record(asn, hit=ok, valid=valid)
    ledger.record_proxy_outcome(asn, ok=ok)
    return asn


__all__ = [
    "AsnRegistry",
    "RESIDENTIAL_NETWORK_TYPES",
    "cached_asn_of",
    "order_candidates",
    "record_registration_outcome",
]
