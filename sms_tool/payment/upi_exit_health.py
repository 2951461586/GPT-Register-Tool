"""Per-exit risk-block memory for the UPI lane.

Why this module exists
----------------------
A ``blocked`` verdict is a property of the **exit**, not of the attempt.  The
local stage already knows that: ``_stage_approve`` counts
``blocked_count`` and concludes "provider risk control" when every attempt was
blocked.  What it did not do was *remember it after the run*.  The next account
handed the same exit started from zero, spent a Checkout, a Sentinel mint and
five approve attempts, and learned the same thing again.

The reference implementation persists exactly this
(``upi_extract.py:1609 record_exit_risk_block`` /
``:1684 exit_health_block_reason`` / ``:1711 prune_exit_health``, with
``UPI_EXIT_HEALTH*`` knobs and a ``min_pool`` guard so one bad exit cannot empty a
small pool).

Policy
------
* A block is a **window**, not a ban: it expires, and the exit is usable again.
* ``min_pool`` guard: a pool at or below the floor is never fully poisoned --
  the caller is told the exit is blocked, but ``should_skip`` refuses to skip
  when that would leave nothing.  (Local pools are often 1-3 credentials, so
  this matters more here than upstream.)
* Storage is one JSON file under ``runtime/``, written through the same
  lock + atomic replace as the identity ledger (:mod:`identity_store`).
* Every read/write is best-effort: a locked or corrupt file must never stop a
  link from being generated.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any

from .upi_identity_store import env_flag, env_number
from .upi_identity_store import ledger_transaction, read_ledger, store_path

#: File name next to ``upi_identity.json``.
EXIT_HEALTH_FILENAME = "upi_exit_health.json"

#: Default block window (seconds) and how many blocks before it applies.
DEFAULT_BLOCK_WINDOW = 1800
DEFAULT_MAX_BLOCKS = 1

#: A pool this small is never allowed to be fully poisoned: with three or fewer
#: candidate exits, skipping "the blocked one" can mean skipping the only exit.
DEFAULT_MIN_POOL = 3


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def health_enabled(cfg: Mapping[str, Any] | None = None) -> bool:
    """``upi.exit_health`` / ``UPI_EXIT_HEALTH`` (default **on**).

    On by default because the feature is a pure write-then-read of local state
    and cannot change a successful run; the switch exists so an operator can
    compare a batch against the pre-P1-3 behaviour.
    """
    section = cfg.get("upi") if isinstance(cfg, Mapping) else None
    if isinstance(section, Mapping) and "exit_health" in section:
        return bool(section.get("exit_health"))
    return env_flag("UPI_EXIT_HEALTH", True)


def skip_enabled(cfg: Mapping[str, Any] | None = None) -> bool:
    """``upi.exit_health_skip`` / ``UPI_EXIT_HEALTH_SKIP`` (default **off**).

    When on, a run whose assigned exit is still cooling fails fast with a
    retryable ``exit_health_blocked`` instead of spending a Checkout on it.  Off
    by default because the UPI lane receives a single credential: only a caller
    that owns a pool (``payment.batch``) can turn the refusal into a different
    exit.
    """
    section = cfg.get("upi") if isinstance(cfg, Mapping) else None
    if isinstance(section, Mapping) and "exit_health_skip" in section:
        return bool(section.get("exit_health_skip"))
    return env_flag("UPI_EXIT_HEALTH_SKIP", False)


def block_window(cfg: Mapping[str, Any] | None = None) -> int:
    return env_number("UPI_EXIT_HEALTH_WINDOW", DEFAULT_BLOCK_WINDOW, minimum=0)


def max_blocks(cfg: Mapping[str, Any] | None = None) -> int:
    return env_number("UPI_EXIT_HEALTH_MAX_BLOCKS", DEFAULT_MAX_BLOCKS, minimum=1)


def min_pool(cfg: Mapping[str, Any] | None = None) -> int:
    return env_number("UPI_EXIT_HEALTH_MIN_POOL", DEFAULT_MIN_POOL, minimum=1)


def _path(cfg: Mapping[str, Any] | None = None):
    return store_path(cfg).with_name(EXIT_HEALTH_FILENAME)


def _key(proxy: Any) -> str:
    """Stable, non-reversible exit key.

    The raw credential is a secret and must not be written to disk; the ledger
    only needs to recognise the same exit again.
    """
    text = _text(proxy)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def read_health(cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return read_ledger(_path(cfg))


def _entry(ledger: dict[str, Any], key: str) -> dict[str, Any]:
    exits = ledger.setdefault("exits", {})
    entry = exits.get(key)
    if not isinstance(entry, dict):
        entry = {}
        exits[key] = entry
    return entry


def record_block(
    proxy: Any,
    reason: str = "",
    *,
    cfg: Mapping[str, Any] | None = None,
    window: int | None = None,
    fingerprint_id: str = "",
) -> dict[str, Any] | None:
    """Record one risk block on this exit; returns the updated record.

    Never raises: this is scheduling telemetry, and a failed write must not
    change the extraction result.
    """
    key = _key(proxy)
    if not key or not health_enabled(cfg):
        return None
    seconds = block_window(cfg) if window is None else max(0, _int(window, 0))
    now = time.time()
    try:
        with ledger_transaction(_path(cfg)) as ledger:
            entry = _entry(ledger, key)
            entry["blocks"] = _int(entry.get("blocks"), 0) + 1
            entry["last_reason"] = _text(reason)[:200]
            entry["last_block_at"] = now
            if entry["blocks"] >= max_blocks(cfg):
                entry["blocked_until"] = now + seconds
            if fingerprint_id:
                entry["fingerprint_id"] = fingerprint_id
            return dict(entry)
    except Exception:
        return None


def block_reason(proxy: Any, *, cfg: Mapping[str, Any] | None = None) -> str:
    """Why this exit is currently cooling (``""`` when it is clear)."""
    key = _key(proxy)
    if not key or not health_enabled(cfg):
        return ""
    try:
        ledger = read_health(cfg)
    except Exception:
        return ""
    exits = ledger.get("exits")
    entry = exits.get(key) if isinstance(exits, Mapping) else None
    if not isinstance(entry, Mapping):
        return ""
    until = entry.get("blocked_until")
    try:
        remaining = float(until or 0.0) - time.time()
    except (TypeError, ValueError):
        return ""
    if remaining <= 0:
        return ""
    reason = _text(entry.get("last_reason")) or "blocked"
    return f"{reason} ({remaining:.0f}s left)"


def should_skip(
    proxy: Any,
    *,
    pool_size: int | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    """True when this exit should be skipped **and** the pool can afford it."""
    if not block_reason(proxy, cfg=cfg):
        return False
    if pool_size is not None and _int(pool_size, 0) <= min_pool(cfg):
        # Never strand a small pool: report the block, but do not skip.
        return False
    return True


def clear(proxy: Any, *, cfg: Mapping[str, Any] | None = None) -> bool:
    """Forget this exit's blocks (used after a successful link)."""
    key = _key(proxy)
    if not key:
        return False
    try:
        with ledger_transaction(_path(cfg)) as ledger:
            exits = ledger.get("exits")
            if not isinstance(exits, dict) or key not in exits:
                return False
            del exits[key]
            return True
    except Exception:
        return False


def prune(*, cfg: Mapping[str, Any] | None = None) -> int:
    """Drop entries whose window has expired; returns how many were removed."""
    now = time.time()
    removed = 0
    try:
        with ledger_transaction(_path(cfg)) as ledger:
            exits = ledger.get("exits")
            if not isinstance(exits, dict):
                return 0
            for key in list(exits):
                entry = exits.get(key)
                try:
                    until = float(entry.get("blocked_until") or 0.0) if isinstance(entry, Mapping) else 0.0
                except (TypeError, ValueError):
                    until = 0.0
                if until <= now:
                    del exits[key]
                    removed += 1
    except Exception:
        return 0
    return removed


def summary(*, cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Operator-view: how many exits are cooling right now."""
    now = time.time()
    blocked = 0
    total = 0
    ledger = read_health(cfg)
    exits = ledger.get("exits")
    for entry in exits.values() if isinstance(exits, Mapping) else ():
        if not isinstance(entry, Mapping):
            continue
        total += 1
        try:
            if float(entry.get("blocked_until") or 0.0) > now:
                blocked += 1
        except (TypeError, ValueError):
            continue
    return {"total": total, "blocked": blocked, "window": block_window(cfg), "max_blocks": max_blocks(cfg)}


__all__ = [
    "EXIT_HEALTH_FILENAME",
    "block_reason",
    "block_window",
    "clear",
    "health_enabled",
    "max_blocks",
    "min_pool",
    "prune",
    "read_health",
    "record_block",
    "should_skip",
    "skip_enabled",
    "summary",
]
