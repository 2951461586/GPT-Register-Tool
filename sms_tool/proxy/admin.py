"""Proxy-pool administration: list, connectivity test, IP-intel screen.

Three read-only surfaces, one module, because all three answer questions about
the *pool* rather than about a registration:

* :func:`list_pool` — what is configured right now, credentials masked, plus the
  measured geo/health the process already has cached. No network.
* :func:`test_pool` — "does this exit reach the ChatGPT edge, and where does it
  exit from?" One edge probe + one geo pass per entry.
* :func:`screen_report` — the dual-pass residential screening from
  :mod:`sms_tool.proxy.screening` plus the ASN ranking.

Callers are the CLI flags (``--proxy-list`` / ``--proxy-test`` /
``--proxy-screen``), the ``--desktop-read proxy-pool`` read and the desktop
ProxyPool window. Every result is a JSON-safe dict; credentials never appear in
it (``mask_proxy``), and the raw pool is only echoed back when the caller asks
for the desktop read's editable text.

Why not put this on the SOCKS5 pool server (``start_proxy_pool.py``)? That
process owns *forwarding*, not operator diagnostics, and it deliberately has no
knowledge of registration lanes or screening policy.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

from .edge_probe import probe_openai_edge
from .health import ProxyHealthTracker
from .registry import resolve_registration
from .routing import parse_lane_proxy_pool
from .screening import (
    NETWORK_DATACENTER,
    ScreeningSample,
    aggregate_by_asn,
    mask_proxy,
    probe_once,
    screen_pool,
    summarize,
)

logger = logging.getLogger(__name__)

DEFAULT_LANE = "protocol_registration"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _entries(proxies: Iterable[str]) -> list[str]:
    return [str(item or "").strip() for item in proxies or [] if str(item or "").strip()]


def _cached_geo(proxy: str) -> dict[str, Any]:
    """Return the already-measured geo for an exit, or an empty dict.

    Imported inside the function: ``scripts/import_layer_ratchet.py`` has no
    ``proxy -> geo`` module-level edge and this is a read-only convenience, not
    a reason to widen it. Never probes — a list must not pay for N round-trips.
    """
    try:
        from ..geo import shared_geo_resolver

        geo = shared_geo_resolver().cached(proxy)
    except Exception:
        return {}
    if geo is None:
        return {}
    return {
        "ip": geo.ip,
        "country": geo.country,
        "asn": geo.asn,
        "isp": geo.isp,
        "network_type": geo.network_type,
        "source": geo.source,
    }


def _health_rank(config: Mapping[str, Any] | None) -> dict[str, int]:
    """Map ``host:port`` -> rank position using the existing health journal."""
    try:
        tracker = ProxyHealthTracker(config or {})
        pool = _entries(resolve_registration(config or {}, lane=DEFAULT_LANE))
    except Exception:
        return {}
    try:
        ranked = tracker.rank(pool)
    except Exception:
        return {}
    return {proxy: index for index, proxy in enumerate(ranked)}


def _row(proxy: str, *, rank: int | None = None) -> dict[str, Any]:
    parsed = urlsplit(proxy)
    geo = _cached_geo(proxy)
    # A hint-only cache entry (credential template said ``region-VN``) carries a
    # country but no IP/ASN/timezone. Reporting it as "measured" made the list
    # look like a real probe had run; keep the country visible but mark the
    # evidence for what it is.
    geo_source = str(geo.get("source") or "")
    measured = bool(geo) and geo_source != "hint"
    row = {
        "proxy": mask_proxy(proxy),
        "host": str(parsed.hostname or ""),
        "port": _as_int(parsed.port, 0),
        "scheme": str(parsed.scheme or "http").lower(),
        "has_auth": bool(parsed.username or parsed.password),
        "country": str(geo.get("country") or ""),
        "asn": str(geo.get("asn") or ""),
        "isp": str(geo.get("isp") or ""),
        "network_type": str(geo.get("network_type") or "unknown"),
        "source": geo_source,
        "measured": measured,
        "health_rank": rank if rank is not None else -1,
    }
    # The raw URL is included only when it is credential-free, so the desktop can
    # offer "copy" for entries that have no secret in them. A masked value is
    # never a copyable proxy, so it must not masquerade as one.
    if "-***" not in row["proxy"] and "***:***@" not in row["proxy"]:
        row["proxy_raw"] = proxy
    return row


def list_pool(config: Mapping[str, Any] | None = None, *, lane: str = DEFAULT_LANE) -> dict[str, Any]:
    """Describe the configured pool for the given lane (no network)."""
    proxies = _entries(resolve_registration(config or {}, lane=lane))
    rank = _health_rank(config or {})
    rows = [_row(proxy, rank=rank.get(proxy)) for proxy in proxies]
    return {
        "ok": True,
        "lane": lane,
        "count": len(rows),
        "measured": sum(1 for row in rows if row["measured"]),
        "proxies": rows,
        "asns": _asn_rows(rows),
    }


def _asn_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        asn = str(row.get("asn") or "")
        if asn:
            groups.setdefault(asn, []).append(row)
    return [
        {
            "asn": asn,
            "isp": str(items[0].get("isp") or ""),
            "count": len(items),
            "network_type": str(items[0].get("network_type") or "unknown"),
            "countries": sorted({str(item.get("country") or "") for item in items if item.get("country")}),
        }
        for asn, items in sorted(groups.items())
    ]


def test_pool(
    proxies: Iterable[str],
    *,
    country: str = "",
    workers: int = 8,
    timeout: float = 6.0,
    edge_probe: Callable[..., Any] | None = None,
    geo_probe: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Probe every entry against the edge **and** measure where it exits from.

    Two independent questions on purpose: an exit can be edge-clean and still be
    a datacenter line, or residential and still be Cloudflare-blocked. Collapsing
    them into one "ok" loses the reason an operator needs.
    """
    entries = _entries(proxies)
    if not entries:
        return {"ok": True, "total": 0, "results": []}
    if edge_probe is None:
        edge_probe = probe_openai_edge
    worker_count = max(1, min(_as_int(workers, 8), 32, len(entries)))

    def run(proxy: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            verdict = edge_probe(proxy, timeout=timeout)
            edge_status = str(getattr(verdict, "status", "") or "unknown")
            http_status = _as_int(getattr(verdict, "http_status", 0), 0)
            edge_error = str(getattr(verdict, "error", "") or "")
        except Exception as exc:
            edge_status, http_status, edge_error = "dead", 0, f"{type(exc).__name__}: {exc}"
        geo = probe_once(proxy, timeout=timeout, probe=geo_probe)
        target = str(country or "").strip().upper()
        return {
            "proxy": mask_proxy(proxy),
            "edge": edge_status,
            "http_status": http_status,
            "edge_error": edge_error,
            "country": geo.country,
            "asn": geo.asn,
            "isp": geo.isp,
            "network_type": geo.network_type,
            "ip": geo.ip,
            "latency_ms": _as_int((time.perf_counter() - started) * 1000, 0),
            "country_match": bool(target) and geo.country == target,
            "reachable": edge_status not in {"dead"},
            "residential": geo.network_type in {"residential", "mobile"},
        }

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(run, entries))
    return {
        "ok": True,
        "total": len(results),
        "reachable": sum(1 for row in results if row["reachable"]),
        "residential": sum(1 for row in results if row["residential"]),
        "country": str(country or "").strip().upper(),
        "results": results,
    }


def screen_report(
    proxies: Iterable[str],
    *,
    country: str = "",
    workers: int = 8,
    quality_min: int = 0,
    timeout: float = 6.0,
    probe: Callable[..., Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the dual-pass screening and return samples + the ASN ranking.

    When ``config`` is supplied (the CLI/desktop path), the residential samples
    are also written into the ASN ledger's ``proxy_asn`` mapping. That is what
    lets a later registration batch use the measurement: the in-memory geo cache
    is process-local, so without this the screening result disappeared on
    restart. Tests call without ``config`` and therefore never touch the
    operator's runtime database.
    """
    entries = _entries(proxies)
    samples: list[ScreeningSample] = screen_pool(
        entries,
        country=country,
        workers=workers,
        quality_min=quality_min,
        timeout=timeout,
        probe=probe,
    )
    if config is not None:
        try:
            from .asn_registry import AsnRegistry

            AsnRegistry.from_config(config).configure(
                [
                    {
                        "proxy": sample.proxy,
                        "asn": sample.asn,
                        "isp": sample.isp,
                        "network_type": sample.network_type,
                        "country": sample.country,
                    }
                    for sample in samples
                ]
            )
        except Exception as exc:  # a ledger write must not lose the report
            logger.warning("ASN ledger update failed: %s", type(exc).__name__)
    return {
        "ok": True,
        "country": str(country or "").strip().upper(),
        "quality_min": _as_int(quality_min, 0),
        "counts": summarize(samples),
        "asns": aggregate_by_asn(samples),
        "samples": [sample.to_dict() for sample in samples],
        # Raw (credential-bearing) eligible entries are deliberately NOT part of
        # this payload: it is written into task results and logs. The CLI
        # ``--out`` file is the only place a usable pool is materialised.
        "datacenter": sum(1 for sample in samples if sample.network_type == NETWORK_DATACENTER),
    }


def resolve_pool(
    config: Mapping[str, Any] | None = None,
    *,
    lane: str = DEFAULT_LANE,
    explicit: Any = None,
) -> list[str]:
    """Resolve the raw (credential-bearing) entries to probe.

    ``explicit`` wins over the lane, matching ``proxy_pool_probe.load_pool``: an
    operator pasting a pool into the UI means *that* pool, not the configured
    one. Credentials stay inside this list and never reach a payload.
    """
    parsed = _entries(parse_lane_proxy_pool(explicit))
    if parsed:
        return parsed
    return _entries(resolve_registration(config or {}, lane=lane))


__all__ = [
    "DEFAULT_LANE",
    "list_pool",
    "resolve_pool",
    "screen_report",
    "test_pool",
]
