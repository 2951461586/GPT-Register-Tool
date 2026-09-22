"""Explicit proxy lanes for registration, protocol work, and account health.

Registration and post-registration account checks have different failure
profiles.  In particular, reusing a stale registration exit for repeated
quota/promotion probes can invalidate a freshly-created account.  This module
keeps lane selection in one small, testable boundary.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .phone_proxy import normalize_proxy_url


def parse_lane_proxy_pool(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[\r\n,;]+", value)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        proxy = normalize_proxy_url(str(item or "").strip())
        if proxy and proxy not in result:
            result.append(proxy)
    return result


def _section(config: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    value = config.get(name) if isinstance(config, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def proxy_pool_for(config: Mapping[str, Any] | None, lane: str) -> list[str]:
    """Return only the pool owned by ``lane``.

    The compatibility fallbacks are intentionally one-way: browser/protocol
    registration own their corresponding pools, while health operations use
    the registration pool as a legacy fallback. Persisted accounts can further
    restore their exact registration affinity in ``select_operation_proxy``.
    """
    proxy = _section(config, "proxy")
    health = _section(config, "account_health")
    health_proxies = _section(health, "proxies")

    aliases = {
        "browser_registration": ("browser_pool", "browser_registration_pool"),
        "protocol_registration": ("protocol_pool", "protocol_registration_pool"),
        "liveness": ("liveness_pool", "quota_pool", "liveness"),
        "promotion": ("promotion_pool", "promotion"),
        "health_browser": ("browser_pool", "browser", "browser_verification_pool"),
    }
    keys = aliases.get(lane, (lane,))
    for key in keys:
        values = parse_lane_proxy_pool(health_proxies.get(key))
        if values:
            return values
        values = parse_lane_proxy_pool(proxy.get(key))
        if values:
            return values

    if lane == "browser_registration":
        values = parse_lane_proxy_pool(proxy.get("registration"))
        values.extend(item for item in parse_lane_proxy_pool(proxy.get("pool")) if item not in values)
        return values or parse_lane_proxy_pool(proxy.get("default"))
    if lane == "protocol_registration":
        values = parse_lane_proxy_pool(proxy.get("protocol")) or parse_lane_proxy_pool(proxy.get("registration"))
        values.extend(item for item in parse_lane_proxy_pool(proxy.get("pool")) if item not in values)
        return values or parse_lane_proxy_pool(proxy.get("default"))
    if lane in {"liveness", "promotion", "health_browser"}:
        values = parse_lane_proxy_pool(health.get("proxy_pool"))
        if values:
            return values
        # Operator decision 2026-08-29: drop the separate 127.0.0.1:7897 lane
        # so post-registration checks reuse the signup egress. Fall back to the
        # registration pool; the isolated-health-lane behaviour is kept only if
        # an explicit health/account_health proxy list is configured.
        values = parse_lane_proxy_pool(proxy.get("health"))
        if values:
            return values
        # Keep the registration endpoint first for deterministic affinity, but
        # include the complete configured pool. Previously the scalar
        # ``proxy.registration`` short-circuited this fallback and silently
        # collapsed all health probes onto one exit, making transient TLS
        # failures look like account failures.
        values = parse_lane_proxy_pool(proxy.get("registration"))
        for item in parse_lane_proxy_pool(proxy.get("pool")):
            if item not in values:
                values.append(item)
        return values or parse_lane_proxy_pool(proxy.get("default"))
    return []


def _use_registration_affinity(config: Mapping[str, Any] | None) -> bool:
    """Read ``account_health.use_registration_affinity`` (default false).

    When enabled, health/promotion probes restore the account's saved
    registration proxy instead of the dedicated health lane, so the probe
    exit matches the signup exit.  Opt-in only: the default keeps the
    isolated health lane that avoids stale signup exits.
    """
    health = _section(config, "account_health")
    value = health.get("use_registration_affinity", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass(frozen=True)
class OperationProxyCandidate:
    """One ordered egress candidate and its non-sensitive provenance."""

    proxy: str
    source: str


def _saved_registration_proxy(
    account: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None,
) -> str:
    if not (
        _use_registration_affinity(config)
        and isinstance(account, Mapping)
        and account.get("identity_context")
    ):
        return ""
    try:
        from .accounts.account_identity import resolve_account_proxy

        return normalize_proxy_url(resolve_account_proxy(account, config=config) or "") or ""
    except Exception:
        return ""


def operation_proxy_candidates(
    account: Mapping[str, Any] | None,
    *,
    operation: str,
    explicit: str | None = None,
    pool: Any = None,
    config: Mapping[str, Any] | None = None,
) -> tuple[OperationProxyCandidate, ...]:
    """Resolve the complete operation egress order.

    Precedence is explicit command input, saved registration affinity when the
    operator enables it, then the operation pool (including its documented
    one-way compatibility fallback).  The source label is safe to persist in
    diagnostics; proxy credentials are not.
    """
    ordered: list[OperationProxyCandidate] = []

    def append(values: Sequence[str], source: str) -> None:
        existing = {item.proxy for item in ordered}
        for value in values:
            normalized = normalize_proxy_url(str(value or "").strip())
            if normalized and normalized not in existing:
                ordered.append(OperationProxyCandidate(normalized, source))
                existing.add(normalized)

    append(parse_lane_proxy_pool(explicit), "explicit")
    saved = _saved_registration_proxy(account, config)
    if saved:
        append([saved], "registration_affinity")

    configured = parse_lane_proxy_pool(pool) if pool is not None else proxy_pool_for(config, operation)
    # When affinity is disabled, avoid immediately reusing the registration
    # endpoint if the operation pool contains a clean alternative. Explicit
    # input and enabled affinity have already been placed ahead of this pool
    # and are never filtered.
    if configured and isinstance(account, Mapping) and account.get("identity_context") and not saved:
        affinity = (account.get("identity_context") or {}).get("proxy_affinity")
        reg_host = str((affinity or {}).get("host") or "").strip().lower()
        try:
            reg_port = int((affinity or {}).get("port") or 0)
        except (TypeError, ValueError):
            reg_port = 0
        alternatives = []
        if reg_host and len(configured) > 1:
            for candidate in configured:
                parsed = urlsplit(candidate)
                if parsed.hostname and parsed.hostname.lower() == reg_host and int(parsed.port or 0) == reg_port:
                    continue
                alternatives.append(candidate)
        if alternatives:
            configured = alternatives
    if len(configured) > 1:
        seed = str((account or {}).get("email") or operation)
        start = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % len(configured)
        configured = configured[start:] + configured[:start]
    append(configured, "operation_pool")
    return tuple(ordered)


def select_operation_proxy_candidate(
    account: Mapping[str, Any] | None,
    *,
    operation: str,
    explicit: str | None = None,
    pool: Any = None,
    config: Mapping[str, Any] | None = None,
) -> OperationProxyCandidate | None:
    candidates = operation_proxy_candidates(
        account,
        operation=operation,
        explicit=explicit,
        pool=pool,
        config=config,
    )
    return candidates[0] if candidates else None


def select_operation_proxy(
    account: Mapping[str, Any] | None,
    *,
    operation: str,
    explicit: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """Choose the first candidate from the canonical operation proxy order."""
    selected = select_operation_proxy_candidate(
        account,
        operation=operation,
        explicit=explicit,
        config=config,
    )
    if selected is None:
        return None
    # Preserve deterministic distribution inside the operation pool while
    # never moving it ahead of explicit input or registration affinity.
    candidates = operation_proxy_candidates(
        account,
        operation=operation,
        explicit=explicit,
        config=config,
    )
    same_source = [item for item in candidates if item.source == selected.source]
    if len(same_source) <= 1:
        return selected.proxy
    seed = str((account or {}).get("email") or operation)
    index = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % len(same_source)
    return same_source[index].proxy


__all__ = [
    "OperationProxyCandidate",
    "operation_proxy_candidates",
    "parse_lane_proxy_pool",
    "proxy_pool_for",
    "select_operation_proxy",
    "select_operation_proxy_candidate",
]


__all__ = ["parse_lane_proxy_pool", "proxy_pool_for", "select_operation_proxy"]
