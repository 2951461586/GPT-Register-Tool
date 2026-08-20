"""Strong egress-country gate for protocol-payment subprocess extractors.

Before a subprocess extractor (ideal/pix/kakao/blik/twint/direct_card/momo)
spawns, the stage proxies it will use are probed through the proxy itself and
the observed exit country must match the route plan's expected country.  This
keeps a mis-routed proxy — for example a Kookeey sticky session whose country
code was never rewritten — from starting a checkout/approve that would only
fail (or worse, run with the wrong geography) mid-protocol.  The gate runs
before any side effect, so a rejection costs one probe request, not a checkout.

Gate behavior is configured under ``protocol_payments.egress_check``:
``{"enabled": true, "timeout_seconds": 12, "cache_ttl_seconds": 600}``.
Only stages whose proxy *and* expected country are both present are asserted;
stages without an expectation are skipped so the gate never invents policy.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

# stage -> (options key holding the proxy, stage_proxy_countries keys, in order)
_STAGE_EXPECTATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("checkout", "checkout_proxy", ("checkout",)),
    ("approve", "approve_proxy", ("approve",)),
    ("promotion", "promotion_proxy", ("promotion", "update")),
)

_DEFAULT_TIMEOUT_SECONDS = 12.0
_DEFAULT_CACHE_TTL_SECONDS = 600.0

_cache_lock = threading.Lock()
_probe_cache: dict[tuple[str, str], tuple[float, tuple[bool, str, str]]] = {}


class EgressCheckError(Exception):
    """Raised when a stage proxy's observed exit country violates the route plan."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool,
        stage: str,
        expected_country: str,
        observed_country: str,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.stage = stage
        self.expected_country = expected_country
        self.observed_country = observed_country

    def to_result(self, payment_method: str) -> dict[str, Any]:
        return {
            "ok": False,
            "payment_method": payment_method,
            "status": "failed",
            "url": "",
            "error": str(self),
            "error_code": self.error_code,
            "failure_class": "proxy_country_mismatch" if self.error_code == "egress_country_mismatch" else "proxy_transport_failed",
            "error_stage": "preparing_proxy",
            "retryable": self.retryable,
            "egress": {
                "stage": self.stage,
                "expected_country": self.expected_country,
                "observed_country": self.observed_country,
            },
        }


def _gate_settings(runtime_config: Mapping[str, Any] | None) -> tuple[bool, float, float, float]:
    protocol = runtime_config.get("protocol_payments") if isinstance(runtime_config, Mapping) else None
    gate = protocol.get("egress_check") if isinstance(protocol, Mapping) else None
    if not isinstance(gate, Mapping):
        gate = {}
    enabled = gate.get("enabled", True)
    try:
        timeout = max(3.0, float(gate.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECONDS
    try:
        ttl = max(0.0, float(gate.get("cache_ttl_seconds") or _DEFAULT_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        ttl = _DEFAULT_CACHE_TTL_SECONDS
    try:
        failure_cooldown = max(0.0, float(gate.get("failure_cooldown_seconds") or 180))
    except (TypeError, ValueError):
        failure_cooldown = 180.0
    return bool(enabled), timeout, ttl, failure_cooldown


def _default_probe(proxy: str, expected_country: str, stage: str, timeout: float):
    from .paypal_proxy import probe_proxy

    return probe_proxy(proxy, expected_country=expected_country, stage=stage, timeout=timeout)


def _cached_probe(
    probe: Callable[..., Any],
    proxy: str,
    expected: str,
    stage: str,
    timeout: float,
    ttl: float,
    failure_cooldown: float,
) -> tuple[bool, str, str]:
    """Probe with a monotonic cache so batch runs do not re-probe healthy proxies."""
    from .paypal_proxy import proxy_key
    key = (proxy_key(proxy), expected)
    now = time.monotonic()
    with _cache_lock:
        cached = _probe_cache.get(key)
        if cached and now - cached[0] < (ttl if cached[1][0] else failure_cooldown):
            return cached[1]
    result = probe(proxy, expected, stage, timeout)
    observed = str(getattr(result, "country_code", "") or "")
    error = str(getattr(result, "error", "") or "")
    ok = bool(getattr(result, "ok", False))
    outcome = (ok, observed, error)
    with _cache_lock:
        _probe_cache[key] = (now, outcome)
    return outcome


def clear_cache() -> None:
    """Drop cached probe outcomes (tests re-run scenarios against fresh state)."""
    with _cache_lock:
        _probe_cache.clear()


def assert_egress_countries(
    options: Mapping[str, Any],
    runtime_config: Mapping[str, Any] | None = None,
    *,
    probe: Callable[..., Any] | None = None,
) -> None:
    """Assert every routed stage proxy egresses from its expected country.

    Raises :class:`EgressCheckError` on mismatch or probe failure; returns
    silently when the gate is disabled or a stage carries no expectation.
    """
    enabled, timeout, ttl, failure_cooldown = _gate_settings(runtime_config)
    if not enabled:
        return
    probe = probe or _default_probe
    countries = options.get("stage_proxy_countries")
    if not isinstance(countries, Mapping):
        countries = {}
    for stage, proxy_key, country_keys in _STAGE_EXPECTATIONS:
        proxy = str(options.get(proxy_key) or "").strip()
        if not proxy:
            continue
        expected = ""
        for country_key in country_keys:
            expected = str(countries.get(country_key) or "").strip().upper()
            if expected:
                break
        if not expected:
            continue
        ok, observed, error = _cached_probe(probe, proxy, expected, stage, timeout, ttl, failure_cooldown)
        if ok:
            continue
        if error.startswith("country_mismatch:"):
            observed = error.split(":", 1)[1].strip().upper() or observed
            raise EgressCheckError(
                f"egress_country_mismatch: stage={stage} expected={expected} observed={observed} "
                f"({proxy_key} egresses from the wrong country before any side effect)",
                error_code="egress_country_mismatch",
                retryable=True,
                stage=stage,
                expected_country=expected,
                observed_country=observed,
            )
        raise EgressCheckError(
            f"proxy_transport_failed: stage={stage} expected={expected} error={error}",
            error_code="egress_probe_failed",
            retryable=True,
            stage=stage,
            expected_country=expected,
            observed_country=observed,
        )
