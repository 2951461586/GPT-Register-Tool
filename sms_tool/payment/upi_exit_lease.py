"""Exclusive per-exit leases so two lanes never dial the same exit at once.

Why this module exists
----------------------
``payment.batch`` runs up to four workers for UPI
(``payment/batch.py:136``), and the route planner hands each cell a proxy by
index (``payment/routing.py:431``: ``pool_offset % len(pool)``).  With a pool
smaller than the worker count -- and always across two *processes* -- two lanes
can be dialling the same residential credential at the same time.  Every request
then looks like two accounts behind one IP inside a few seconds, which is the
cheapest possible correlation to detect, and both lanes are slowed by the other's
session rotation.

The reference implementation holds a lease per task
(``upi_extract.py:1136 ProxySeedLease``, ``UPI_PROXY_LEASE*``).  This module is the
same idea on local primitives:

* the lease is an ``O_EXCL`` file per exit under ``runtime/upi_lease/``, so it is
  honoured across processes without a daemon;
* the holder refreshes ``mtime`` (heartbeat); a file older than the TTL is
  considered abandoned and may be taken over;
* **fail-open**: a pool whose candidates are all leased is still usable.  A
  collision is bad, but refusing to run is worse, and this must never be the
  reason a link is not generated.

Opt-in (``payment.proxy_lease.enabled`` / ``UPI_PROXY_LEASE``) because it adds a
filesystem round-trip per selection; the default keeps today's behaviour.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


LEASE_DIRNAME = "upi_lease"
DEFAULT_TTL_SECONDS = 1800
DEFAULT_WAIT_SECONDS = 0

_guard = threading.Lock()
_holdings: dict[str, str] = {}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _runtime_file(cfg: Mapping[str, Any] | None, name: str) -> Path:
    """``kernel.paths.runtime_file`` at call time (see ``identity_store``)."""
    from ..kernel.paths import runtime_file

    return runtime_file(cfg, name)


def lease_dir(cfg: Mapping[str, Any] | None = None) -> Path:
    raw = _text(os.environ.get("UPI_PROXY_LEASE_DIR"))
    path = (
        Path(raw).expanduser()
        if raw
        else _runtime_file(cfg if isinstance(cfg, Mapping) else {}, LEASE_DIRNAME) / LEASE_DIRNAME
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _section(cfg: Mapping[str, Any] | None) -> Mapping[str, Any]:
    section = cfg.get("payment") if isinstance(cfg, Mapping) else None
    nested = section.get("proxy_lease") if isinstance(section, Mapping) else None
    return nested if isinstance(nested, Mapping) else {}


def enabled(cfg: Mapping[str, Any] | None = None) -> bool:
    """``payment.proxy_lease.enabled`` / ``UPI_PROXY_LEASE`` (default **off**)."""
    section = _section(cfg)
    if "enabled" in section:
        return bool(section.get("enabled"))
    raw = _text(os.environ.get("UPI_PROXY_LEASE")).lower()
    return raw in {"1", "true", "yes", "on"}


def ttl_seconds(cfg: Mapping[str, Any] | None = None) -> int:
    return _int(_section(cfg).get("ttl") or os.environ.get("UPI_PROXY_LEASE_TTL"), DEFAULT_TTL_SECONDS)


def owner_token(explicit: Any = "") -> str:
    """Lease owner: an explicit id (batch cell) or this thread's identity."""
    text = _text(explicit)
    if text:
        return text
    return f"{os.getpid()}:{threading.get_ident()}"


def _key(proxy: Any) -> str:
    text = _text(proxy)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _lease_path(proxy: Any, cfg: Mapping[str, Any] | None = None) -> Path | None:
    key = _key(proxy)
    if not key:
        return None
    return lease_dir(cfg) / f"{key}.lease"


def _read_holder(path: Path) -> tuple[str, float]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        owner, _, stamp = raw.partition("\n")
        return owner, float(stamp or 0.0)
    except (OSError, ValueError):
        return "", 0.0


def _write_holder(path: Path, owner: str) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(f"{owner}\n{time.time()}", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def holder(proxy: Any, cfg: Mapping[str, Any] | None = None) -> str:
    """Who holds this exit right now (``""`` when free or expired)."""
    path = _lease_path(proxy, cfg)
    if path is None or not path.is_file():
        return ""
    owner, stamp = _read_holder(path)
    ttl = ttl_seconds(cfg)
    if ttl > 0 and stamp and (time.time() - stamp) > ttl:
        return ""
    return owner


def is_leased_by_other(proxy: Any, *, owner: Any = "", cfg: Mapping[str, Any] | None = None) -> bool:
    current = holder(proxy, cfg)
    if not current:
        return False
    return current != owner_token(owner)


def acquire(proxy: Any, *, owner: Any = "", cfg: Mapping[str, Any] | None = None) -> bool:
    """Try to take the lease; ``True`` when this owner now holds it."""
    if not enabled(cfg) or not _text(proxy):
        return True
    path = _lease_path(proxy, cfg)
    if path is None:
        return True
    token = owner_token(owner)
    with _guard:
        current = holder(proxy, cfg)
        if current and current != token:
            return False
        _write_holder(path, token)
        _holdings[str(path)] = token
        return True


def heartbeat(proxy: Any, *, owner: Any = "", cfg: Mapping[str, Any] | None = None) -> bool:
    """Refresh the lease timestamp (call between stages on a long run)."""
    if not enabled(cfg):
        return True
    path = _lease_path(proxy, cfg)
    if path is None:
        return True
    token = owner_token(owner)
    if holder(proxy, cfg) != token:
        return False
    with _guard:
        _write_holder(path, token)
    return True


def release(proxy: Any, *, owner: Any = "", cfg: Mapping[str, Any] | None = None) -> bool:
    """Drop the lease if this owner holds it."""
    if not _text(proxy):
        return False
    path = _lease_path(proxy, cfg)
    if path is None:
        return False
    token = owner_token(owner)
    with _guard:
        if holder(proxy, cfg) != token:
            return False
        try:
            path.unlink()
        except OSError:
            return False
        _holdings.pop(str(path), None)
        return True


def filter_available(
    proxies: Iterable[Any],
    *,
    owner: Any = "",
    cfg: Mapping[str, Any] | None = None,
) -> list[str]:
    """Candidates this owner may use, **fail-open** when everything is leased.

    Keeping the full list in that case is deliberate: a busy pool must degrade to
    today's behaviour (possible collision), never to "no exit at all".
    """
    entries = [_text(proxy) for proxy in proxies if _text(proxy)]
    if not enabled(cfg):
        return entries
    free = [proxy for proxy in entries if not is_leased_by_other(proxy, owner=owner, cfg=cfg)]
    return free or entries


def release_all(*, owner: Any = "", cfg: Mapping[str, Any] | None = None) -> int:
    """Release every lease this owner holds in this process (batch teardown)."""
    token = owner_token(owner)
    released = 0
    with _guard:
        for key, held in list(_holdings.items()):
            if held != token:
                continue
            try:
                Path(key).unlink()
                released += 1
            except OSError:
                continue
            _holdings.pop(key, None)
    return released


@contextmanager
def lease(proxy: Any, *, owner: Any = "", cfg: Mapping[str, Any] | None = None) -> Iterator[bool]:
    """``with lease(proxy) as ok:`` -- acquires, then always releases."""
    ok = acquire(proxy, owner=owner, cfg=cfg)
    try:
        yield ok
    finally:
        if ok:
            release(proxy, owner=owner, cfg=cfg)


def reset_holdings() -> int:
    """Forget the in-process holdings map (tests / a fresh batch in one process).

    Only the bookkeeping is cleared -- lease *files* stay, because they are the
    cross-process truth. Returns how many entries were dropped.
    """
    with _guard:
        dropped = len(_holdings)
        _holdings.clear()
    return dropped


def summary(cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Operator view: how many exits are leased right now."""
    directory = lease_dir(cfg)
    now = time.time()
    ttl = ttl_seconds(cfg)
    active = 0
    total = 0
    for path in directory.glob("*.lease"):
        total += 1
        _owner, stamp = _read_holder(path)
        if not ttl or not stamp or (now - stamp) <= ttl:
            active += 1
    return {"enabled": enabled(cfg), "dir": str(directory), "active": active, "files": total, "ttl": ttl}


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "LEASE_DIRNAME",
    "acquire",
    "enabled",
    "filter_available",
    "heartbeat",
    "holder",
    "is_leased_by_other",
    "lease",
    "lease_dir",
    "owner_token",
    "release",
    "release_all",
    "reset_holdings",
    "summary",
    "ttl_seconds",
]
