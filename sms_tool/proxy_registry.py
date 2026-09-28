"""Unified proxy-pool registry: one surface to resolve and inspect every lane.

Why this exists
---------------
The three egress lanes grew independent resolution paths:

============  ==========================================================
lane          resolver
============  ==========================================================
registration  ``proxy_routing.proxy_pool_for`` (lanes + one-way fallbacks)
mailbox       ``mailbox.mailbox_proxy_candidates``
payment       ``payment_routing.payment_proxy_pools`` / ``PaymentRoutePlanner``
============  ==========================================================

Their differences are deliberate and pinned by tests (Rule 19), so this module
does **not** replace them -- it *fronts* them: one vocabulary, one resolve call,
one census.  Operators get a single place to see and reason about every pool;
the call sites keep working unchanged.

Optional single declaration point
---------------------------------
When ``proxy.lanes`` is present it is authoritative for the matching lane; when
absent (the default) every resolver uses its legacy keys unchanged.  The reader
lives in :mod:`proxy_routing` so both this facade and
``proxy_routing.proxy_pool_for`` share it::

    "proxy": {
      "lanes": {
        "protocol_registration": ["http://...", ...],
        "mailbox": ["http://...", ...],
        "payment": {"pools": {"US": [...], "JP": [...]}, "default": [...]}
      }
    }

Rule 10/13: host-side only; proxy strings are parsed by ``proxy_entry`` through
the existing resolvers, never re-implemented here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .proxy_entry import parse_proxy, proxy_to_url
from .proxy_routing import canonical_lane_pool, canonical_payment_pools, parse_lane_proxy_pool, proxy_pool_for

__all__ = [
    "LANES",
    "MAILBOX",
    "PAYMENT",
    "REGISTRATION",
    "census",
    "endpoints_of",
    "format_census",
    "resolve",
    "resolve_mailbox",
    "resolve_payment",
    "resolve_registration",
]

REGISTRATION = "registration"
MAILBOX = "mailbox"
PAYMENT = "payment"
LANES = (REGISTRATION, MAILBOX, PAYMENT)

#: Registration resolution order: the protocol lane is the canonical one; the
#: browser lane is offered for callers that explicitly register with a browser
#: driver.  Both consult ``proxy.lanes.<lane>`` first (inside ``proxy_pool_for``).
_REGISTRATION_LANES = ("protocol_registration", "browser_registration")


def endpoints_of(pool: Sequence[Any]) -> list[str]:
    """Distinct ``host:port`` endpoints of a pool, sorted, credential-free."""
    endpoints: set[str] = set()
    for item in pool or ():
        entry = parse_proxy(str(item or "").strip())
        if entry and entry.host and entry.port:
            endpoints.add(f"{entry.host.lower()}:{entry.port}")
        elif str(item or "").strip():
            # Not a parseable proxy (e.g. a bare local listener): keep the raw
            # host:port so the census never silently drops an entry.
            endpoints.add(str(item).strip())
    return sorted(endpoints)


def _credentials(pool: Sequence[Any]) -> set[str]:
    """Full normalized credential set -- one sticky session each. Never printed.

    The lane-isolation rule is about the *exit*, and two different sticky
    sessions on one endpoint are different exits, so this compares full
    credentials rather than bare ``host:port`` endpoints.
    """
    out: set[str] = set()
    for item in pool or ():
        entry = parse_proxy(str(item or "").strip())
        if entry and entry.host and entry.port:
            out.add(proxy_to_url(entry))
    return out


def _canonical(config: Mapping[str, Any] | None, lane: str) -> list[str] | None:
    return canonical_lane_pool(config, lane)


def resolve_registration(config: Mapping[str, Any] | None, *, lane: str = "protocol_registration") -> list[str]:
    """Registration pool for ``lane`` (``proxy.lanes`` wins when declared)."""
    if lane not in _REGISTRATION_LANES:
        lane = "protocol_registration"
    return proxy_pool_for(config, lane)


def resolve_mailbox(config: Mapping[str, Any] | None, *, operation_proxy: str | None = None) -> list[str]:
    """Mailbox/OTP pool; canonical ``proxy.lanes.mailbox`` wins when declared."""
    canonical = _canonical(config, MAILBOX)
    if canonical:
        values = list(canonical)
        operation = str(operation_proxy or "").strip()
        if operation and operation not in values:
            values.append(operation)
        return values
    # Legacy path validates the whole config (``mailbox._config_data``); a
    # census must not fail on a partial config, so a resolution error degrades
    # to an empty pool rather than raising.  Live polling still uses mailbox's
    # own resolver, not this facade.
    try:
        from .mailbox import mailbox_proxy_candidates  # lazy: pulls provider adapters

        return parse_lane_proxy_pool(mailbox_proxy_candidates(operation_proxy, config))
    except Exception:
        return []


def _method_local_pools(cfg: Mapping[str, Any], method: str) -> dict[str, list[str]]:
    """Pools declared inside the method's own section (``upi.stage_proxies`` …).

    These are read by the extractors directly, not through ``payment_routing``,
    so the census would otherwise miss them (e.g. the UPI bringyour exit).
    """
    protocol_raw = cfg.get("protocol_payments")
    protocol: Mapping[str, Any] = protocol_raw if isinstance(protocol_raw, Mapping) else {}
    methods_raw = protocol.get("methods")
    methods: Mapping[str, Any] = methods_raw if isinstance(methods_raw, Mapping) else {}
    out: dict[str, list[str]] = {}
    for section_name, section in (
        (method, cfg.get(method)),
        (f"protocol_payments.methods.{method}", methods.get(method)),
    ):
        if not isinstance(section, Mapping):
            continue
        for key in ("stage_proxies", "stage_proxy_pools", "proxies", "proxy_pool", "proxy"):
            value = section.get(key)
            if isinstance(value, Mapping):
                for name, pool in value.items():
                    parsed = parse_lane_proxy_pool(pool)
                    if parsed:
                        out[f"{section_name}.{key}.{name}"] = parsed
            else:
                parsed = parse_lane_proxy_pool(value)
                if parsed:
                    out[f"{section_name}.{key}"] = parsed
    return out


def resolve_payment(config: Mapping[str, Any] | None, *, method: str = "paypal") -> dict[str, Any]:
    """Payment pools: named regions plus this method's per-stage pools.

    Canonical ``proxy.lanes.payment`` supplies the region map when declared;
    otherwise the legacy ``protocol_payments.proxy_pools`` names are used and
    the method's stage pools are resolved through ``payment_routing``.
    """
    protocol = config.get("protocol_payments") if isinstance(config, Mapping) else None
    protocol = protocol if isinstance(protocol, Mapping) else {}
    cfg: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
    regions: dict[str, list[str]] = (
        {
            str(name): parse_lane_proxy_pool(value)
            for name, value in (protocol.get("proxy_pools") or {}).items()
            if parse_lane_proxy_pool(value)
        }
        if isinstance(protocol.get("proxy_pools"), Mapping)
        else {}
    )

    canonical = canonical_payment_pools(cfg)
    regions_source = "protocol_payments.proxy_pools"
    if canonical:
        regions = dict(canonical)
        regions_source = "proxy.lanes.payment"

    stages: dict[str, list[str]] = {}
    from .payment_routing import payment_proxy_pools  # lazy: pulls the payment catalog

    try:
        stages = {str(name): pool for name, pool in payment_proxy_pools(cfg, method).items() if pool}
    except Exception:
        # A method may be unknown/disabled; the census must not fail on that.
        stages = {}
    # Method-local pools (``upi.stage_proxies`` …) are read by the extractors
    # directly; surface them under their own names so the census is complete.
    for name, pool in _method_local_pools(cfg, method).items():
        stages.setdefault(name, pool)
    return {"regions": regions, "stages": stages, "regions_source": regions_source}


def resolve(config: Mapping[str, Any] | None, lane: str, **kwargs: Any) -> Any:
    """Resolve ``lane`` (``registration`` / ``mailbox`` / ``payment``)."""
    key = str(lane or "").strip().lower()
    if key == REGISTRATION:
        return resolve_registration(config, **kwargs)
    if key == MAILBOX:
        return resolve_mailbox(config, **kwargs)
    if key == PAYMENT:
        return resolve_payment(config, **kwargs)
    raise ValueError(f"unknown proxy lane: {lane!r} (expected one of {LANES})")


def census(config: Mapping[str, Any] | None, *, methods: Sequence[str] = ("paypal", "upi")) -> dict[str, Any]:
    """One structured view of every pool, for operators and health dashboards."""
    registration = resolve_registration(config)
    mailbox = resolve_mailbox(config)
    report: dict[str, Any] = {
        REGISTRATION: {
            "count": len(registration),
            "endpoints": endpoints_of(registration),
            "source": "proxy.lanes" if _canonical(config, "protocol_registration") else "proxy.registration/pool",
        },
        MAILBOX: {
            "count": len(mailbox),
            "endpoints": endpoints_of(mailbox),
            "source": "proxy.lanes" if _canonical(config, MAILBOX) else "mailbox_proxy(_pool)",
        },
    }
    payment: dict[str, Any] = {"methods": {}, "endpoints": set()}
    payment_pools: list[list[str]] = []
    union: set[str] = set()
    union.update(report[REGISTRATION]["endpoints"])
    union.update(report[MAILBOX]["endpoints"])
    for method in methods:
        resolved = resolve_payment(config, method=method)
        method_view: dict[str, Any] = {"regions_source": resolved["regions_source"], "regions": {}, "stages": {}}
        for name, pool in resolved["regions"].items():
            eps = endpoints_of(pool)
            method_view["regions"][name] = {"count": len(pool), "endpoints": eps}
            payment["endpoints"].update(eps)
            payment_pools.append(list(pool))
        for name, pool in resolved["stages"].items():
            eps = endpoints_of(pool)
            method_view["stages"][name] = {"count": len(pool), "endpoints": eps}
            payment["endpoints"].update(eps)
            payment_pools.append(list(pool))
        payment["methods"][method] = method_view
    union.update(payment["endpoints"])
    report[PAYMENT] = {
        "regions": (resolve_payment(config, method=methods[0]).get("regions", {}) if methods else {}),
        "endpoints": sorted(payment["endpoints"]),
        "source": resolve_payment(config, method=methods[0])["regions_source"] if methods else "n/a",
        "methods": payment["methods"],
    }
    report["union_endpoints"] = sorted(union)
    report["local_endpoints"] = sorted(ep for ep in union if ep.split(":")[0] in {"127.0.0.1", "localhost", "::1"})
    # Lane isolation: payment/mailbox must not reuse a registration exit. Compare
    # full credentials (distinct sticky sessions on one endpoint are distinct
    # exits); report endpoints, never the credential itself.
    registration_creds = _credentials(registration)
    mailbox_shared = registration_creds & _credentials(mailbox)
    payment_shared: set[str] = set()
    for pool in payment_pools:
        payment_shared |= registration_creds & _credentials(pool)
    report["cross_lane_overlaps"] = {
        MAILBOX: endpoints_of(sorted(mailbox_shared)),
        PAYMENT: endpoints_of(sorted(payment_shared)),
    }
    report["lane_isolation_ok"] = not mailbox_shared and not payment_shared
    return report


def format_census(report: Mapping[str, Any]) -> str:
    """Human-readable, credential-free rendering of :func:`census`."""
    lines: list[str] = []
    for lane in (REGISTRATION, MAILBOX):
        section = report.get(lane) or {}
        lines.append(f"{lane:13s} count={section.get('count', 0):3d} source={section.get('source', '')}")
        for endpoint in section.get("endpoints", []):
            lines.append(f"    {endpoint}")
    payment = report.get(PAYMENT) or {}
    lines.append(f"{'payment':13s} endpoints={len(payment.get('endpoints', []))} source={payment.get('source', '')}")
    for method, view in (payment.get("methods") or {}).items():
        lines.append(f"  [{method}] regions_source={view.get('regions_source', '')}")
        for name, info in view.get("regions", {}).items():
            lines.append(f"    region {name:16s} count={info['count']:3d} endpoints={info['endpoints']}")
        for name, info in view.get("stages", {}).items():
            lines.append(f"    stage  {name:16s} count={info['count']:3d} endpoints={info['endpoints']}")
    overlaps = report.get("cross_lane_overlaps") or {}
    if overlaps.get(MAILBOX) or overlaps.get(PAYMENT):
        lines.append("CROSS-LANE OVERLAP (registration credential reused):")
        for lane in (MAILBOX, PAYMENT):
            for endpoint in overlaps.get(lane, []):
                lines.append(f"    {lane}: {endpoint}")
    else:
        lines.append("lane isolation: OK (no credential shared with registration)")
    lines.append(
        f"union endpoints = {len(report.get('union_endpoints', []))} (local={len(report.get('local_endpoints', []))})"
    )
    return "\n".join(lines)
