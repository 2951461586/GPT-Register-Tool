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
from typing import Any
from urllib.parse import urlsplit

from .phone_proxy import normalize_proxy_url


def parse_proxy_pool(value: Any) -> list[str]:
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

    The compatibility fallbacks are intentionally one-way: browser
    registration may fall back to the legacy registration pool, while health
    checks may fall back to the legacy default.  Health never falls back to a
    saved account affinity unless the caller explicitly asks for it.
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
        values = parse_proxy_pool(health_proxies.get(key))
        if values:
            return values
        values = parse_proxy_pool(proxy.get(key))
        if values:
            return values

    if lane == "browser_registration":
        values = parse_proxy_pool(proxy.get("registration"))
        values.extend(item for item in parse_proxy_pool(proxy.get("pool")) if item not in values)
        return values or parse_proxy_pool(proxy.get("default"))
    if lane == "protocol_registration":
        values = parse_proxy_pool(proxy.get("protocol")) or parse_proxy_pool(proxy.get("registration"))
        values.extend(item for item in parse_proxy_pool(proxy.get("pool")) if item not in values)
        return values or parse_proxy_pool(proxy.get("default"))
    if lane in {"liveness", "promotion", "health_browser"}:
        values = parse_proxy_pool(health.get("proxy_pool"))
        if values:
            return values
        # Operator decision 2026-08-29: drop the separate 127.0.0.1:7897 lane
        # so post-registration checks reuse the signup egress. Fall back to the
        # registration pool; the isolated-health-lane behaviour is kept only if
        # an explicit health/account_health proxy list is configured.
        return (
            parse_proxy_pool(proxy.get("health"))
            or parse_proxy_pool(proxy.get("registration"))
            or parse_proxy_pool(proxy.get("default"))
        )
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


def select_operation_proxy(
    account: Mapping[str, Any] | None,
    *,
    operation: str,
    explicit: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """Choose a stable proxy for a health operation without stale affinity."""
    # Opt-in: reuse the account's registration proxy for probes so the
    # fingerprint/proxy pair stays identical to signup time.
    if _use_registration_affinity(config) and isinstance(account, Mapping) and account.get("identity_context"):
        try:
            from .account_identity import resolve_account_proxy

            saved = resolve_account_proxy(account, config=config)
            if saved:
                return saved
        except Exception:
            pass
    explicit_pool = parse_proxy_pool(explicit)
    configured_pool = proxy_pool_for(config, operation)
    # For persisted accounts, a dedicated lane is authoritative.  Callers
    # often pass the signup proxy as a generic fallback; allowing it to
    # override the health lane would reintroduce the contaminated-exit
    # problem this router prevents.  Stateless callers retain the explicit
    # proxy for backwards compatibility (and for one-off diagnostics).
    has_identity = isinstance(account, Mapping) and bool(account.get("identity_context"))
    pool = configured_pool if (configured_pool and has_identity) else (explicit_pool or configured_pool)
    # A freshly-created browser account is especially sensitive to exit reuse:
    # the registration proxy may be rate-limited or challenged immediately
    # after signup.  Prefer a different health exit when the configured lane
    # offers one, but retain a single-entry pool as a last resort.
    if configured_pool and has_identity and len(configured_pool) > 1:
        affinity = (account.get("identity_context") or {}).get("proxy_affinity")
        reg_host = str((affinity or {}).get("host") or "").strip().lower()
        try:
            reg_port = int((affinity or {}).get("port") or 0)
        except (TypeError, ValueError):
            reg_port = 0
        if reg_host:
            alternatives = []
            for candidate in configured_pool:
                parsed = urlsplit(candidate)
                if parsed.hostname and parsed.hostname.lower() == reg_host and int(parsed.port or 0) == reg_port:
                    continue
                alternatives.append(candidate)
            if alternatives:
                pool = alternatives
    if not configured_pool and explicit_pool and isinstance(account, Mapping) and account.get("identity_context"):
        # Legacy callers passed a generic fallback proxy while expecting the
        # saved account affinity to remain authoritative.  Preserve that
        # behavior only when no dedicated health lane is configured; once a
        # health pool exists it always wins and prevents stale signup exits.
        try:
            from .account_identity import resolve_account_proxy

            saved = resolve_account_proxy(account, fallback_proxy=explicit, config=config)
            if saved:
                return saved
        except Exception:
            pass
    if not pool:
        return None
    email = str((account or {}).get("email") or "").strip().lower()
    digest = hashlib.sha256(email.encode("utf-8")).digest() if email else b"\x00"
    return pool[int.from_bytes(digest[:4], "big") % len(pool)]


__all__ = ["parse_proxy_pool", "proxy_pool_for", "select_operation_proxy"]
